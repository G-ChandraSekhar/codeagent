# ADR 0006: Git safety policy for filters, hooks, and content fidelity

Status: Accepted (2026-09-16)

Implementation status: **not implemented.** No mechanism in this ADR
exists in production code yet. Acceptance of this design is
independent of implementation and of Linux validation — see
Consequences.

## Context

Threat-model entry T-M3 ("Repository-configured Git hooks execute host
code during CodeAgent's own Git operations") is only partially closed.
`src/codeagent/checkpoint_ref.py` (Milestone 2 slice A) strips `GIT_*`
environment variables and passes `-c core.hooksPath=/dev/null` for its
own ref-transaction invocations, but `src/codeagent/workspace.py` and
`src/codeagent/patch.py` are unprotected, and the threat model itself
flags `core.fsmonitor`, `core.pager`, `core.editor`, diff/merge
drivers, and `filter.*` as unaddressed.

This ADR is driven by real, positive-controlled probes (throwaway
fixture repositories, real hostile filters/hooks, real markers) run
this session, not by documentation review alone. Key findings:

1. **`git worktree add` performs a real checkout.** Against a fixture
   with `filter.hostile.smudge`/`.process` and a `post-checkout` hook,
   plain `git worktree add --detach <path> <commit>` executed the
   configured smudge/process filter (process takes precedence over
   smudge) and the hook. `workspace.py`'s exact invocation shape was
   reproduced, not a hypothetical.
2. **`git worktree add --no-checkout --detach <path> <commit>`
   executes nothing** (no filter, no hook markers fired) but leaves the
   linked worktree's **index empty**, not populated to the target
   commit (`git ls-files -z` is empty; `git diff --cached --stat` shows
   every tracked file "deleted"; `git status --porcelain=v1 -uno` shows
   `D` for each).
3. **`git read-tree <commit>` populates only the index**, exactly, with
   no filter/hook execution and no working-tree materialization
   (confirmed via absent markers, `git ls-files -z` now listing every
   tracked path, `git diff --cached --stat` now empty, and an
   unchanged empty working tree). This is one documented Git plumbing
   command, not a custom filesystem populator.
4. **`git ls-files -z` piped into `git check-attr --cached -z --stdin`**,
   run against that populated index, correctly reports `filter`,
   `text`, `eol`, `ident`, `working-tree-encoding`, and legacy `crlf`
   for every tracked path, reading from the **index** (not the working
   tree or any file that hasn't been staged) — all before a single
   byte is written to disk and with zero code execution.
5. **The four canonical `check-attr` states** are `unspecified` (no
   rule), `unset` (explicit negation), `set` (bare boolean-true), and
   an explicit `value`. For `text`/`eol`/`ident`/
   `working-tree-encoding`, only `unspecified`/`unset` are safe. For
   `filter`, `unspecified`/`unset` are safe **only conditionally** —
   see finding 16, which corrects the unconditional reading this
   finding originally carried: the reported string alone is not
   sufficient, because a configured driver may be *named* `unset` or
   `unspecified`.
6. **This inspection is not an absolute guarantee.** It fixes the
   `.gitattributes` state recorded in the *inspected index* only.
   `.git/info/attributes`, `core.attributesFile`/global attributes, and
   Git configuration itself are external mutable inputs that
   `check-attr` merges in at query time — a change to any of them
   between inspection and the subsequent checkout/add/commit is a real,
   unclosed timing gap. Per threat-model assumption A4 (host OS/local-
   process compromise is out of scope), this is recorded as an
   **explicit A4-scoped residual race**, not eliminated and not claimed
   to be eliminated.
7. **`core.autocrlf=false` does not override `.gitattributes`
   `eol=crlf`.** A fixture with `crlf.txt text eol=crlf` staged CRLF
   working-tree bytes as LF in the blob regardless of the global
   autocrlf setting (verified by hexdump). No single global `-c`
   override can close the content-fidelity gap; only per-path
   discovery and refusal can.
8. **`check-attr --cached` reads the staged index, not the working
   tree.** An unstaged edit to `.gitattributes` does not change the
   result until that edit is staged. This fixes the required scan
   ordering for any patch that touches `.gitattributes`.
9. **Byte-for-byte identity was proven** for a path with no active
   unsafe attributes: SHA-256 of the bytes CodeAgent wrote, before
   staging, equalled SHA-256 of the retrieved committed blob.
10. **Enumeration of `filter.*` config has real, independently-found
    correctness bugs**, distinct from the execution-safety question:
    - `-c filter.<name>.process=` (empty string) when `.process` was
      **never configured** for that driver causes `git add`/checkout to
      hard-fail (`fatal: clean filter '<name>' failed`) rather than
      disabling anything — the override must only be emitted for
      subkeys that actually exist in the enumerated config, mirroring
      the earlier-discovered `core.fsmonitor=` empty-string bug.
    - `/bin/cat` **cannot** serve as a `.process` filter — `.process`
      is a long-running pkt-line protocol
      (`git-filter-client`/`git-filter-server` handshake), and
      `/bin/cat` doesn't speak it (`error: Unexpected line
      'git-filter-client', expected git-filter-server`). `/bin/cat` is
      only a valid passthrough for one-shot `clean`/`smudge`; `.process`
      must be cleared outright when configured, never redirected.
    - `filter.<name>.required=false` was tested with a clean
      positive/negative/control triple:
      - *Positive* (motivates the question): a blind, incorrect
        override of `.process=` on a driver that never defined
        `.process` causes `git add` to fail hard.
      - *Negative*: adding `required=false` on top of that same bug
        makes `git add` succeed, silently falling back to unfiltered
        content.
      - *Control*: with the override corrected (only overriding
        subkeys that actually exist — `.process` left untouched
        because it was never configured), `git add` succeeds normally
        under `required=true` with no special-casing needed.
      - Conclusion: `required=false` only masked the enumerator's own
        bug. With correct per-key-existence overrides, it is
        unnecessary, and it is excluded from this design — a policy
        that fails closed should not silently paper over its own
        construction bugs.
    - Driver names may legitimately contain dots (`filter.weird.name.clean`
      resolves to driver name `weird.name`, since Git's config
      subsection matches everything between the first and last dot
      verbatim). Enumeration must not naively split on the first two
      dots.
    - Enumeration and the subsequent protected Git invocation are two
      separate process executions that each independently reread
      configuration from disk — this is not an atomic snapshot, and the
      gap between them is the same class of A4-scoped residual race as
      finding 6, not "the same resolved state."
11. **`ident` keyword expansion was independently forced and reproduced.**
    A file with a bare `$Id$` line and `ident` set stages as literal
    `$Id$` (collapsed) but expands on checkout to
    `$Id: c66d3f4e824b4af6b1f0c68e21369a843cbde027 $` (verified: expand
    on smudge, collapse on clean, real content hash shown). `check-attr
    --cached ident` reports `id.txt: ident: set` — an explicit `set`
    state, unsafe under the section-2 policy, refused before staging.
12. **`working-tree-encoding` conversion was independently forced and
    reproduced.** A file attributed `working-tree-encoding=UTF-16`,
    written on disk as real UTF-16LE-with-BOM bytes (`ff fe 68 00 65 00
    6c 00 6c 00 6f 00`, i.e. "hello"), staged as UTF-8 bytes with no BOM
    (`68 65 6c 6c 6f`) — a genuine, silent byte-level re-encoding with
    no external process involved. `check-attr --cached
    working-tree-encoding` reports the explicit value `UTF-16` — unsafe,
    refused before staging.
13. **`filter` state `unset` was independently proven safe** in an
    isolated fixture: a driver `hostile` with real `clean`/`smudge`
    commands that append to a marker file, one path exempted via
    `-filter` in `.gitattributes`. `check-attr --cached filter` reports
    `unset` for the exempted path; `git add` of that path alone produced
    no marker output, while the driver was independently confirmed live
    (it does fire for non-exempted paths in the same repo). This
    confirms `unset`, not only `unspecified`, is a safe state for
    `filter` **in that fixture** — where no driver was named `unset`.
    Finding 16 shows why that qualifier is load-bearing.
14. **Attribute inheritance from three external sources was confirmed**,
    all without any local `.gitattributes` in the affected directory:
    a parent-directory `.gitattributes` (`sub/*.txt filter=hostile`
    correctly applied to `sub/child.txt`, which has no `.gitattributes`
    of its own); `.git/info/attributes`; and global `core.attributesFile`
    pointed at a file outside the repository. `check-attr --cached`
    correctly merged all three without any repo-local `.gitattributes`
    entry for the affected path in two of the three cases.
15. **Local Git operations can silently perform a lazy fetch from a
    partial clone merely to answer a read-only existence/inspection
    query, and this executes the repository's configured remote
    transport — proven with a local fake remote-helper fixture, not a
    real network service.** A genuine `--filter=blob:none` clone was
    built via the `ext::` transport pointed at a local wrapper script
    (`fakehelper.sh`) that appends a timestamped line to a marker file
    and then execs `git upload-pack <origin>` — so it behaves as a real
    remote helper while leaving unambiguous, positive-control evidence
    of invocation, without any credentials, real network service, or
    network nondeterminism. The clone was confirmed genuinely partial
    via `git count-objects -v` (4 objects — 2 commits, 2 trees — both
    blobs absent) before every test, and the marker file was cleared
    after clone/`read-tree` setup so only the *command under test*'s
    invocation of the helper would appear.

    Every command CodeAgent's design relies on for pre-materialization
    inspection was tested individually against this fixture — object
    existence, exit status, object-count change, and marker presence
    were recorded for each, not inferred from `cat-file` alone:

    | Command | Needs the missing blob? | Default env (helper allowed) | `GIT_NO_LAZY_FETCH=1` (helper allowed) |
    |---|---|---|---|
    | `read-tree <commit>` | No | exit 0, objects unchanged (4→4), marker absent | not separately needed — already safe |
    | `ls-files -z` | No | exit 0, objects unchanged (4→4), marker absent | not separately needed — already safe |
    | `check-attr --cached -z --stdin` | No | exit 0, objects unchanged (4→4), marker absent | not separately needed — already safe |
    | `checkout -- <path>` | Yes | exit 0, objects **4→5**, marker **PRESENT** (helper invoked) | exit 255 (`error: unable to read sha1 file of <path> (<sha>)`), objects unchanged (4→4), marker absent |
    | `cat-file -e <sha>` | Yes | exit 0, objects **4→5**, marker **PRESENT** (helper invoked) | exit 1, objects unchanged (4→4), marker absent |

    This confirms two distinct facts that must not be conflated: (a)
    `read-tree`/`ls-files`/`check-attr --cached` are safe by
    construction — they operate on tree/index metadata already present
    in a `blob:none` partial clone and never touch the remote helper
    regardless of `GIT_NO_LAZY_FETCH`, matching finding 3's original
    claim; and (b) `checkout` and any blob-content command (`cat-file`,
    and by the same mechanism `add`/`diff` against blob content) do
    invoke the configured transport by default, and `GIT_NO_LAZY_FETCH=1`
    is both necessary (default env silently invokes the helper) and
    fully effective (it prevents helper invocation and produces a hard
    failure instead) at blocking that invocation — for the same reason
    it worked in this fixture as it would for a real `ext::`/SSH/HTTPS
    remote helper, since the interception point is `GIT_NO_LAZY_FETCH`
    disabling Git's own decision to fetch at all, before any transport
    is selected.
    - **Verified this round**: Git 2.54.0 on macOS, `ext::` transport
      with a local fake helper (no real network, no credentials).
    - **Not verified**: Linux, or a real SSH/HTTPS remote. Per the
      author's decision, Linux validation is required before T-M3 is
      marked mitigated, but does not block accepting this ADR's design;
      real SSH/HTTPS testing is not required at all, because the
      `GIT_NO_LAZY_FETCH` prohibition is enforced by Git before
      transport/helper selection — it is a property of Git's own
      fetch-decision logic, not of which transport would have been
      used, so the `ext::` fixture exercises the same enforcement point
      a real remote would.
16. **`check-attr` output alone cannot classify `filter` safety: the
    reported state strings collide with legal driver names.** Found
    during implementation review of the foundation module, and
    reproduced with real positive controls in one fixture repository
    containing `a.txt -filter`, `b.txt filter=unset`, `c.txt` (no
    filter rule), `d.txt filter=unspecified`, plus two live drivers
    deliberately *named* `unset` and `unspecified`, each writing a
    marker:

    | Path | `.gitattributes` | `check-attr --cached -z` reports | Driver executed on `git add`? |
    |---|---|---|---|
    | `a.txt` | `-filter` | `unset` | No (marker absent) |
    | `b.txt` | `filter=unset` | `unset` (**identical**) | **Yes** — `DRIVER-RAN:unset-driver` |
    | `c.txt` | no filter rule | `unspecified` | No (marker absent) |
    | `d.txt` | `filter=unspecified` | `unspecified` (**identical**) | **Yes** — `DRIVER-RAN:unspecified-driver` |

    Git resolves the `filter` attribute's *value* as a configured
    driver name, and `unset`/`unspecified` are legal driver names, so
    the literal strings `check-attr` prints for "genuinely negated" and
    "explicitly assigned to a driver of that name" are
    indistinguishable. A context-free rule that treated those two
    strings as safe would therefore permit host code execution during
    exactly the real checkout that §1's primary control exists to
    protect — and `worktree add` has no enumerate-and-neutralize
    backstop (§5), so nothing else would catch it.

    This does **not** affect `text`/`eol`/`ident`/
    `working-tree-encoding`/`crlf`: Git does not resolve those values
    as driver names, so their state classification is unchanged.

## Decision

### 1. Defense layers for filter/hook safety

- **Primary control — inspect resolved attributes, refuse before
  execution.** Before any operation that would let Git run a
  filter/hook/textconv against content, resolve every affected path's
  attributes read-only (`check-attr --cached -z --stdin` against a
  populated index, or `check-attr -z --stdin` against the working tree
  where no index step applies — see per-command table below) and
  refuse with a structured error, causing **whole-run refusal before
  materialization**, if any tracked path has an active external
  `filter` attribute (state `set` or any named value). `unspecified`
  and `unset` are safe **only when that exact string is not also a
  configured filter driver name** — per finding 16, a driver may
  legally be named `unset` or `unspecified`, and `check-attr` reports
  the same literal string for it as for a genuine negation. The
  inspection therefore requires the enumerated driver-name set (§1's
  secondary control supplies it, read-only, even where its overrides
  are not applied) and **refuses conservatively when the reported
  string collides with a configured driver name**, accepting refusal
  of some genuinely-safe paths rather than guessing. This is the sole guarantee
  for `git worktree add`, using `--no-checkout` +
  `git read-tree <commit>` + attribute inspection, then either
  refusing or performing the real checkout under the hardened policy
  once the inspection has cleared every path. Per finding 6, this
  inspection is not an absolute guarantee: it is refusal based on the
  attribute state observed at inspection time, and a configuration
  change between inspection and the checkout/mutation that follows is
  an explicit A4-scoped residual race, not a claim of eliminated risk.
- **Secondary control — enumerate configured drivers and neutralize,
  as defense in depth only.** For commands where no pre-materialization
  inspection point exists (`status`, `add`, `commit` against an
  already-checked-out worktree — see finding 6/10 and the per-operation
  sections below), additionally enumerate `filter.*` configuration
  (`git config -z --get-regexp '^filter\.'`, NUL-safe parsing, in
  bounded chunks per section 1a) and neutralize only the subkeys
  actually present per driver:
  - `clean`/`smudge`, if present, are overridden to the fixed, verified
    absolute path `/bin/cat` (never `shutil.which`, never a
    PATH-resolved name).
  - `process`, if present, is **cleared** (set to empty), never pointed
    at `/bin/cat` (protocol-incompatible per finding 10).
  - `filter.<name>.required` is left at its configured value.
    `required=false` is explicitly **omitted** from v1 (see finding 10):
    the probe proved it only masks the enumerator's own incorrect blind
    `process=` override; with correct per-key-existence overrides it
    was unnecessary, and a policy that fails closed must not silently
    hide its own construction bugs.
  - Driver names are deduplicated by exact string equality after
    NUL-safe parsing (no prefix matching), including names containing
    dots (finding 10/14 — Git subsections match verbatim between the
    first and last dot).
  - Fixed bounds are enforced: at most **128 distinct drivers**, at
    most **256 bytes per driver name**, and at most **65,536 bytes**
    total generated safety `-c` argv payload (including encoded bytes
    and argument terminators). Malformed, NUL-invalid, or undecodable
    `git config -z` output, or any exceeded bound, is a **structured
    refusal**, not a best-effort partial neutralization.
  - This is explicitly *risk reduction*, not a guarantee: enumeration
    and the protected command are separate process invocations, each
    independently rereading configuration, so a configuration change
    between them is an explicit A4-scoped residual race — the same
    class as the primary control's residual race above, not "the same
    resolved state."

#### 1a. Bounded, NUL-safe processing for large repositories

Path enumeration (`git ls-files -z`) and attribute inspection
(`check-attr --cached -z --stdin`) are streamed and NUL-delimited end
to end, never collected as a single in-memory newline-joined string:
paths are read and written in fixed-size chunks so a repository with an
arbitrarily large tracked-file count cannot force unbounded memory
growth or an oversized single `check-attr --stdin` payload. The same
128-driver/256-byte/65,536-byte bounds from section 1 apply uniformly
to filter enumeration regardless of repository size; a repository
whose filter configuration exceeds them is refused, not silently
truncated.

### 2. Exact-byte content-fidelity invariant

- Refusal scope is **local to the affected patch destination, not
  global to the repository**: an active external `filter` on *any*
  tracked path causes whole-run refusal before materialization (since
  a single unsafe path anywhere in the tree can execute host code
  during `worktree add`'s real checkout). Built-in `text`/`eol`/`ident`/
  `working-tree-encoding` transforms do **not** reject the repository
  as a whole — they are refused only when a specific patch destination
  path is unsafe, since these are silent byte-fidelity risks (not
  code-execution risks) and the rest of the repository is unaffected.
- `core.autocrlf=false` is forced on every relevant invocation (it does
  not override `.gitattributes`, per finding 7, but is set anyway to
  remove one variable and document intent).
- Every affected path's `filter`, `text`, `eol`, `ident`,
  `working-tree-encoding`, and legacy `crlf` attributes are inspected.
  For `filter`, `set` and any named value are unsafe; `unspecified` and
  `unset` are safe only when that exact string is not also a configured
  driver name, and are refused conservatively when it is (findings 13
  and 16). For `text`/`eol`/`ident`/
  `working-tree-encoding`/`crlf`, only `unspecified`/`unset` are safe;
  `set` or any explicit value is unsafe (findings 7, 11, 12 — both
  `ident` and `working-tree-encoding` were independently forced and
  reproduced with real positive controls, not merely documented).
- Any path with an active/`set`/valued byte-transforming attribute is
  **refused** for that destination, never silently normalized.
- The invariant to be enforced: the committed blob must be byte-for-
  byte identical to the bytes CodeAgent validated and wrote — not
  merely "some Git-filtered representation of them."

### 3. Handling a patch that changes `.gitattributes`

1. Identify every `.gitattributes` file the patch changes or adds
   (including nested ones), and every path in the patch whose applicable
   attributes could change as a result — this must include new/untracked
   paths introduced by the same patch, not only previously-tracked ones.
2. `.gitattributes` itself is a tracked file and is not exempt from this
   policy: before staging a changed `.gitattributes` path, validate it
   under the attributes **currently effective for that path** — the
   parent-directory, `.git/info/attributes`, and global
   `core.attributesFile` layers that apply to it independently of its
   own content (finding 14 confirms all three are real, live inputs to
   attribute resolution, so a `.gitattributes` file is itself a
   candidate for an inherited unsafe attribute from an outer layer, the
   same as any other path). Then stage it under the hardened policy.
3. Re-run the attribute inspection against the **resulting staged
   index** (`check-attr --cached`, per finding 8 — this only reflects
   staged state, which is why the `.gitattributes` file must be staged
   first).
4. If any destination path — old or newly-added — is now unsafe, fail
   the patch transaction before staging the remaining files, and rely
   on ADR 0003's discard-and-recreate-worktree recovery rather than
   attempting per-file rollback.
5. Only after this inspection clears may the ordinary changed files in
   the patch be staged.
6. Revalidate (re-run the same inspection against the staged index)
   immediately before the commit step, closing the window between
   staging and commit as tightly as a single additional check can.
7. Renames and deletes: a rename is treated as the new path being a
   "new/added" destination for step 1 purposes (its attributes must be
   checked at the new path, since attribute resolution is path-based
   and may differ from the old path). A delete requires no attribute
   check (nothing is being written). Any other patch shape not covered
   by add/modify/rename/delete against a single `.gitattributes` layer
   (e.g. a patch that deletes a `.gitattributes` file itself, changing
   inherited attributes for sibling paths in the same commit) is
   explicitly **unsupported in v1** and must be a structured refusal,
   not a best-effort guess.

### 4. Source-repository status (`snapshot_source()`)

`workspace.py`'s `snapshot_source()` runs `git status` against the
**source** repository before any disposable worktree exists, and may
observe an unstaged `.gitattributes` edit already present in the
source working tree. Because there is no "pre-checkout" moment here
(the source tree is already materialized), the same two-layer defense
applies in the order defense-in-depth requires:
1. Enumerate and neutralize configured filter drivers (secondary
   control, per section 1) for the `git status` invocation itself,
   since `status` can trigger a clean filter during Git's racy-stat
   comparison (confirmed this session, both immediately after checkout
   and with forced-matching mtimes).
2. Additionally inspect attributes with `check-attr -z --stdin` against
   the **working tree** (not `--cached`, since nothing is staged yet)
   for **every tracked path currently in the index, plus every path
   the run's proposed patch will add or modify** (not merely "paths
   status happens to touch" — the source repository's status must be
   trusted as a baseline before any patch is even proposed, so the
   check must cover the full tracked set, not a subset inferred from
   `status`'s own output). Refuse if any has an active unsafe
   attribute before trusting the snapshot as a baseline.

### 5. Hardened baseline and command-specific additions

No single config list is safe for every command, but a fixed baseline
applies to **every** Git invocation this ADR governs, and each command
adds only what its own risk profile requires on top of it. The baseline
is explicit, not implicit:

**Baseline (every invocation):**
- `-c core.hooksPath=/dev/null`
- `-c core.fsmonitor=false` (explicit boolean; an empty string does not
  suppress it — finding from prior-round probing)
- `-c core.autocrlf=false`
- `-c submodule.recurse=false`
- `--no-pager` (explicit flag, in addition to the existing non-tty
  `capture_output=True` discipline, so pager suppression is not solely
  an artifact of how the subprocess happens to be invoked)
- fully sanitized `GIT_*` environment (every inherited `GIT_*` variable
  stripped, then `GIT_NO_LAZY_FETCH=1` deliberately set) plus the
  global `--no-lazy-fetch` flag — the two are deliberately redundant,
  never alternatives (section 6)
- bounded `subprocess` timeout (`GIT_TIMEOUT_SECONDS`, matching
  `checkpoint_ref.py`'s existing constant)
- `subprocess.TimeoutExpired` handled alongside
  `CalledProcessError`/`OSError` at every call site (closing the
  confirmed gap in `workspace.py`/`patch.py`, neither of which
  currently catches it)
- sanitized failure messages — no raw stderr or filesystem path
  embedded in a persisted error (closing the confirmed raw-stderr leak
  in `workspace.py.__enter__`'s `GitWorktreeError` path)

**Per-command additions on top of the baseline:**

| Command | Additions beyond baseline |
|---|---|
| `worktree add --no-checkout` | none (no checkout occurs; baseline's `hooksPath`/`fsmonitor`/`submodule.recurse` are precautionary since nothing should fire) |
| `read-tree` | none (index-only; no filter/hook execution point exists) |
| `ls-files` / `check-attr` | none (read-only) |
| real `checkout`/materialization | primary control (section 1) must have already cleared every path; `post-checkout` hook additionally covered by baseline `hooksPath` |
| `status` (source, pre-worktree) | secondary control (section 1) enumerate-and-neutralize + working-tree `check-attr` refusal (section 4) |
| `add` | primary control (section 2/3 attribute refusal) + secondary control (section 1) enumerate-and-neutralize as backstop |
| `diff --cached` | `--no-textconv --no-ext-diff` (no filters run regardless — pure blob comparison — but external diff/textconv are separate execution paths and must be independently suppressed) |
| `commit` | secondary control (section 1) enumerate-and-neutralize as backstop — **probes this round confirmed `commit` can itself re-run clean/process filters** (e.g. re-staging or index-refresh behavior around the commit), so `commit` is not exempt from the same filter backstop `add` uses; re-validate attributes per section 3 step 6; `-c commit.gpgSign=false`; `-m` always passed (no editor invoked) |
| checkpoint-ref transactions | none beyond baseline (already implements the equivalent hardening; see below) |

`checkpoint_ref.py`'s existing hardening is treated as already
satisfying the baseline for its own ref-only invocations (it strips
`GIT_*` and passes `-c core.hooksPath=/dev/null` today); adopting the
shared module only needs to add the remaining baseline elements
(`fsmonitor`, `autocrlf`, `submodule.recurse`, `--no-pager`,
`GIT_NO_LAZY_FETCH=1`) that are inapplicable no-ops for a ref-only
operation but are included for uniformity, without touching its
transaction or cleanup semantics.

`checkpoint_ref.py`'s existing `_RefTransaction`
(`start`/`prepare`/`commit`/`abort` over `git update-ref --stdin`,
re-observing the ref while `prepare` holds its lock) and its cleanup
behavior are preserved exactly — this ADR extends the same environment-
sanitization and `-c` hardening pattern to `workspace.py`/`patch.py`
and the new no-checkout/read-tree/inspect sequence, and does not modify
`checkpoint_ref.py`'s transaction or cleanup semantics.

### 6. Partial-clone / lazy-fetch safety

Finding 15 established, with a local fake remote-helper fixture (no
real network, no credentials), that `checkout` and any command needing
blob content silently invoke the repository's configured transport to
lazily fetch a missing object from a partial clone, while
`read-tree`/`ls-files`/`check-attr --cached` never do (they don't need
blob content). This is in scope because the entire primary-control
design in section 1 depends on inspection being read-only and
network-free, and finding 15's matrix confirms that dependency holds
for the specific commands section 1 actually uses, while identifying
exactly which later commands (`checkout`, `add`, `diff` against blob
content) need the explicit guard.

**Git >= 2.45 is the v1 minimum supported version.** Git's global
`--no-lazy-fetch` option was introduced in Git 2.45; this ADR's
lazy-fetch design depends on it being a recognized option, so v1
establishes Git 2.45 as CodeAgent's minimum supported Git version,
not merely a preference.

**Preflight, not per-call.** A capability/platform preflight check runs
once, before any repository access (before source-repository status,
worktree creation, or any mutation), and has two parts:

1. Parse `git --version` and require the reported version to be
   `>= 2.45`. A version below 2.45, or output that cannot be parsed as
   a Git version string, is **unsupported substrate**.
2. Additionally prove the global option is actually recognized by
   running the sanitized equivalent of:

   ```
   git --no-pager --no-lazy-fetch --version
   ```

   An unrecognized-option error or any nonzero exit from this call is
   also **unsupported substrate** — independent of the version-number
   check, since a version string alone does not prove the installed
   binary actually behaves as documented for that version (a distro
   patch, a broken build, or a version-string spoof could all pass (1)
   while still failing (2), and step 2 exists precisely so that a
   passing version number is never treated as sufficient proof).

Either failure aborts the run as unsupported substrate before any
repository operation begins; the installed Git cannot be trusted to
honor the lazy-fetch prohibition at all, so no further Git invocation
is attempted. This is a one-time capability check per run, not
re-verified on every Git invocation. **A passing version check is not
by itself evidence of correct behavior** — the macOS/Linux fake-helper
positive/negative controls (finding 15, and the acceptance tests below)
remain the actual proof that lazy fetches are blocked; version-gating
only screens out installations known to lack the option at all.

Once preflight passes, every governed Git invocation for the rest of
the run passes **both** the global `--no-lazy-fetch` flag **and**
`GIT_NO_LAZY_FETCH=1` in its sanitized environment — the flag and the
environment variable are deliberately redundant, not alternatives, so
that a call site which forgets to plumb one still has the other. No
governed invocation is ever made without both.

**Runtime outcomes**, precisely distinguished:
- **Unsupported `GIT_NO_LAZY_FETCH` capability** (the `--no-lazy-fetch`
  preflight call above returns an unrecognized-option error or any
  nonzero exit) — **aborts the whole run** as an unsupported substrate.
  This is a startup-time refusal, before any repository operation
  begins.
- **Supported substrate, but a required object is genuinely missing
  locally** (the normal, expected outcome of finding 15's negative
  control — e.g. `checkout`/`cat-file`/`add` fails closed under
  `GIT_NO_LAZY_FETCH=1`) — **aborts the whole run** as repository
  objects unavailable. This is a distinct outcome from the preflight
  failure above: the capability is confirmed present and working, but
  the specific repository cannot be operated on as given (e.g. it is a
  partial clone missing objects CodeAgent needs).
- **Any other Git failure** (hook/filter refusal, timeout, malformed
  config, ordinary `CalledProcessError`) retains its existing,
  already-categorized operational failure classification from
  elsewhere in this ADR and the surrounding error taxonomy — it is not
  reclassified as a lazy-fetch issue.
- **No retry and no fallback ever occurs without both `--no-lazy-fetch`
  and `GIT_NO_LAZY_FETCH=1` set.** There is no code path that
  re-attempts an operation with lazy fetch allowed, or with only one of
  the two protections in place, after it fails with both disabled —
  that would silently reintroduce the exact network/helper-execution
  risk this section exists to close.
- Exact `ErrorCode`/`ErrorDomain` names for these three outcomes are
  **not invented here**; they are chosen during implementation and
  pinned in `errors.py`/`events.py` and their tests, per this ADR's
  Implementation status and CLAUDE.md's existing errors-taxonomy
  discipline.

**Acceptance scope.** Per the author's decision, Linux validation of
this preflight and both runtime outcomes is mandatory before T-M3 is
marked mitigated, but does not block accepting this ADR's design.
Real SSH/HTTPS transport testing is not required for acceptance: finding
15 established that `GIT_NO_LAZY_FETCH` is enforced by Git's own
fetch-decision logic before any transport or remote helper is selected,
so a local fake-helper fixture exercises the same enforcement point a
real remote would.

### 7. Shared module

A shared `_git_safety.py` (or equivalent) module owns: the sanitized
`GIT_*` environment (including `GIT_NO_LAZY_FETCH=1`), the
`--no-lazy-fetch` global flag applied alongside it on every governed
invocation, the baseline and per-command `-c`/flag sets from section 5,
the timeout constant and
`TimeoutExpired` handling, sanitized error construction, the
bounded/chunked attribute-inspection helpers (section 1a; `check-attr
--cached`/working-tree variants with NUL-safe parsing and the
safe/unsafe state classification from finding 5), and the bounded
filter-enumeration helper from section 1 (128/256/65,536 limits).
`checkpoint_ref.py` may adopt this shared module for its existing
environment/`-c` construction without changing its transaction or
cleanup behavior or its existing tests' observable outcomes.

## Consequences

- **Git LFS and filter-managed repositories are refused in v1**, not
  silently exposed as raw pointer files or partially filtered content.
  This is a deliberate scope limit, not an oversight: no v1 mechanism
  exists to safely execute a repository-configured filter, and doing so
  unsafely was the exact risk this ADR closes.
- **Repositories relying on `text`/`eol`/`ident`/`working-tree-encoding`
  normalization may be refused** wherever CodeAgent needs to
  stage/checkout/commit an affected path, in favor of exact-byte
  fidelity. This is a real, author-visible behavior change from "Git's
  normal filtered representation" to "refuse rather than transform,"
  and is expected to be visible primarily on repositories that
  currently rely on CRLF normalization or keyword expansion.
- **An isolated preparation environment is the identified future route**
  to supporting trusted filters/LFS repositories without relaxing this
  ADR's host-execution guarantee — explicitly deferred, not designed
  here.
- **No production mitigation exists until this ADR is implemented and
  the acceptance tests below pass on both macOS and Linux.** Until
  then, T-M3 remains open for `workspace.py`/`patch.py` exactly as
  currently documented in the threat model and in CLAUDE.md's status
  section.
- **This ADR's acceptance and T-M3's resulting implementation precede
  checkpoint-ref's integration with patch application and the
  controller.** Milestone 2 slice A (`checkpoint_ref.py`) remains
  correctly hardened and unintegrated until this ordering is satisfied
  — integrating it against still-unprotected `workspace.py`/`patch.py`
  would extend a hardened primitive's guarantees to call sites that do
  not yet have them.
- The A4-scoped residual races identified in findings 6 and 10 remain
  after implementation — they are accepted as out of scope per the
  existing A4 assumption (host OS/local-process compromise), not
  newly introduced by this ADR, and must be stated as such rather than
  described as closed.
- **A platform/Git-version combination that cannot support
  `GIT_NO_LAZY_FETCH=1`'s guarantee (section 6, finding 15) is refused
  as an unsupported substrate**, not silently allowed to perform
  network fetches during what is meant to be read-only inspection.
- **CodeAgent v1 requires Git >= 2.45.** An older Git installation is
  refused outright at preflight, as unsupported substrate, rather than
  operated in some weaker mode with lazy-fetch protection disabled or
  best-effort. There is no degraded fallback path for pre-2.45 Git —
  the version floor exists specifically because `--no-lazy-fetch` does
  not exist before 2.45, so there is no equivalent protection to fall
  back to.
- **Accepting this ADR's design is independent of Linux validation.**
  This ADR may be Accepted on the strength of the macOS evidence in
  finding 15 and the rest of this document; T-M3 itself is not marked
  mitigated, and the Linux acceptance tests above must still pass,
  before any claim of an implemented, cross-platform mitigation is
  made.

## Alternatives considered

- **Enumerate-and-neutralize as the primary (sole) mechanism** —
  rejected for `worktree add`: it has no pre-materialization inspection
  point, gives no absolute guarantee, and this session found multiple
  independent correctness bugs in it (empty-string `.process=`,
  `/bin/cat` protocol incompatibility, the `required=false` masking
  effect). Retained only as secondary defense-in-depth where no
  no-checkout-style primary control is possible.
- **Accept residual filter/hook execution risk** — rejected against
  threat-model assumption A2 (all repository content/config is
  untrusted); "hooks/config aren't copied by ordinary clone" alone was
  explicitly rejected as sufficient justification.
- **Isolated preparation environment for all filter-capable
  operations** — not rejected outright, but deferred: it is the
  identified future route for supporting trusted filters/LFS, not
  adopted now because the no-checkout/read-tree/inspect design already
  gives a stronger guarantee for `worktree add` specifically without
  the added implementation/isolation cost.
- **`filter.<name>.required=false` as part of the neutralization
  policy** — rejected; the positive/negative/control test (finding 10)
  showed it only masks the enumerator's own construction bugs and is
  unnecessary once overrides are emitted correctly.

## Required acceptance tests

- No-checkout + `read-tree` + attribute inspection correctly refuses a
  fixture with an active `filter` attribute, and correctly proceeds
  (with filters/hooks proven not to execute via positive-control
  markers) for a fixture with no active unsafe attributes.
- Positive-control hostile `clean`/`smudge`/`process` filters and a
  hostile `post-checkout`/`pre-commit`/`commit-msg`/`post-commit` hook
  each independently prove non-execution under the hardened policy, and
  independently prove execution when the hardening is removed (so the
  test itself is known to be capable of detecting a regression).
- CRLF/`text`/`eol`, `ident`, and `working-tree-encoding` each
  independently force a refusal when in an unsafe (`set`/valued) state,
  and permit the operation when `unspecified`/`unset` — with `ident`
  and `working-tree-encoding` both reproduced this round via real
  positive controls (findings 11–12: real `$Id$` expansion/collapse,
  real UTF-16↔UTF-8 re-encoding), not merely documented.
- `filter` state `unset` is proven safe/non-executing, distinct from
  `unspecified`, using an isolated fixture where the same driver is
  confirmed live for a non-exempted path (finding 13).
- **A driver named `unset` is refused, with a positive control proving
  it executes unprotected** (finding 16): a fixture with `a.txt
  -filter`, `b.txt filter=unset` and a live `filter.unset.clean`
  driver must show `check-attr` reporting the identical string `unset`
  for both paths, the driver actually firing for `b.txt` and not for
  `a.txt`, and the driver-set-dependent classification refusing both.
- **A driver named `unspecified` is refused, with the same positive
  control** (finding 16): `c.txt` (no filter rule) and `d.txt
  filter=unspecified` against a live `filter.unspecified.clean` driver,
  proving the identical-string collision, real execution for `d.txt`
  only, and conservative refusal of both. Both tests must also confirm
  the classification stays permissive for the same attribute states
  once no driver claims those names, so the refusal is attributable to
  the collision rather than to the states themselves.
- Attributes inherited from a parent-directory `.gitattributes`,
  `.git/info/attributes`, and global `core.attributesFile` are all
  correctly detected by the inspection helper, including for a
  `.gitattributes` file that is itself affected by an outer layer
  (section 3 step 2; finding 14).
- Dotted filter driver names (`filter.a.b.clean`) are enumerated
  correctly; malformed or over-limit `filter.*` configuration
  (exceeding 128 drivers, 256 bytes per name, or 65,536 bytes total
  argv, or non-NUL-safe/unparseable output) produces a structured
  refusal, not a partial or silently-degraded neutralization.
- Bounded/chunked path and attribute processing (section 1a) does not
  regress correctness on a repository large enough to require multiple
  chunks, and does not unboundedly grow memory or a single subprocess
  argv/stdin payload.
- A patch that adds new/untracked files and a patch that modifies
  `.gitattributes` both correctly trigger re-inspection against the
  staged index per section 3, including refusal when a newly-added
  destination path becomes unsafe, and including the `.gitattributes`
  file itself being checked against its own currently-effective outer
  attributes before being staged.
- Exact blob-byte equality is verified end-to-end (SHA-256 of bytes
  CodeAgent wrote equals SHA-256 of the retrieved committed blob) for a
  representative safe-state path.
- `GIT_NO_LAZY_FETCH=1` positive and negative controls (finding 15),
  using an equivalent local fake remote-helper fixture (no real
  network/credentials), are reproduced on **both macOS and Linux**, for
  each command in finding 15's matrix (`read-tree`, `ls-files`,
  `check-attr --cached`, `checkout`, `cat-file`/object-existence) —
  not inferred from one command alone. T-M3 remains open until both
  platforms pass.
- The capability preflight correctly aborts the whole run as an
  unsupported substrate when `GIT_NO_LAZY_FETCH=1` cannot be confirmed
  to hold, and correctly aborts as repository-objects-unavailable
  (a distinct outcome) when the capability is confirmed but a required
  object is genuinely missing locally — verified as two separate test
  cases, not one. Unrelated Git failures (hook/filter refusal, timeout,
  malformed config) are confirmed to retain their normal categorized
  operational failure and are not misclassified as either lazy-fetch
  outcome. No code path retries or falls back without
  `GIT_NO_LAZY_FETCH=1` set.
- Version-floor preflight is independently tested with four cases: (a)
  a simulated/actual Git 2.44 (or lower) reports its version parses
  below 2.45 and preflight aborts as unsupported substrate without
  attempting any repository operation; (b) a Git 2.45 (or higher, e.g.
  this session's 2.54.0) passes the version check and proceeds to the
  `--no-lazy-fetch` recognized-option check; (c) malformed or
  unparseable `git --version` output (not matching the expected version
  string shape) aborts as unsupported substrate rather than being
  treated as passing or crashing uncontrolled; (d) a Git report that
  parses as `>= 2.45` but whose `--no-lazy-fetch` invocation still
  fails (unrecognized-option error or nonzero exit) independently
  aborts as unsupported substrate, proving the two preflight checks are
  both enforced and neither is treated as sufficient on its own. None
  of these four cases substitutes for the macOS/Linux fake-helper
  behavioral controls above — a passing preflight only establishes that
  the option is recognized, not that it is honored correctly.
- Exact `ErrorCode`/`ErrorDomain` values for the preflight and
  repository-objects-unavailable outcomes are pinned by
  `errors.py`/`events.py` tests at implementation time (not specified
  in this ADR).
- `subprocess.TimeoutExpired` is handled at every hardened call site,
  and every failure path produces a sanitized message with no raw
  stderr or filesystem path leakage.
- `commit` is independently proven, via a positive-control hostile
  filter, to no longer re-run a clean/process filter once the
  section-5 backstop is applied (closing the gap the probes found in
  plain `commit`).
- `checkpoint_ref.py`'s existing 90 tests, including its transaction
  and cleanup behavior, show no regression after any shared-module
  adoption.
