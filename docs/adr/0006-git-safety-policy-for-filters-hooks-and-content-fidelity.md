# ADR 0006: Git safety policy for filters, hooks, and content fidelity

Status: Accepted (2026-09-16)

Amendment 5 (mandatory pre-D5-C hotfix: refuse unrenderable filter-driver
names; neutralize the workspace entry gate): **Accepted (2026-11-09)**;
committed as `067fca0` and Linux-CI-confirmed in run 42908777385. (Historical:
before its commit this read "implemented and locally verified, uncommitted and
not Linux-CI-confirmed", which was true then.) See "Amendment 5" after
Amendment 4.

Implementation status: **partially implemented.** The shared
foundation (`src/codeagent/_git_safety.py`) and `workspace.py`'s
`worktree add` inspect-then-refuse-or-checkout mechanism are
implemented, committed, and CI-validated on Linux (see
`ENGINEERING_LOG.md` and CLAUDE.md's "Current status"). **Amendments
1–3** below record the `patch.py`-hardening slice, including two
correction passes that found and fixed real gaps — most notably that
`.gitattributes` patch targets (top-level or nested) are **refused**,
not safely supported: a nested `.gitattributes` file's own attribute
classification can be masked by that file's own staged content, and
there is currently no trusted way to validate parent/
`.git/info/attributes`/global attribute layers independently of the
target's own content (Amendment 3). The slice is implemented and
passing its own tests locally (macOS, Git 2.54.0); Linux CI validation
is pending — see Amendment 3 for exactly what evidence that implies
and what it does not. **Amendment 4** (2026-09-17, implemented,
locally macOS-verified, and confirmed on Linux CI — see Amendment 4's
own implementation-status note for the run) extends this policy to
`git diff` against working-tree content, found during Milestone 2
Slice 2B-2's evidence-capture design review; see the end of this
document. Acceptance of this ADR's design
is independent of implementation and of Linux validation — see
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

## Amendment 1: patch.py-hardening slice (implemented; Linux CI validation pending)

Recorded at implementation time, on macOS, Git 2.54.0
(Apple Git-157), arm64. Every finding and control below is what was
actually reproduced and verified by this session's tests
(`tests/unit/test_git_safety.py`, `tests/unit/test_patch.py`,
`tests/unit/test_checkpoint_ref.py`, `tests/unit/test_errors.py`,
`tests/unit/test_events.py`, `tests/integration/test_controller.py`,
`tests/integration/test_slice_c.py`) — not a general or cross-platform
claim, and not yet exercised by CI.

### New findings, with real reproduced evidence

- **Replacement refs (`refs/replace/*`) silently subvert object-content
  resolution.** `git rev-parse <oid>^{tree}`, `<oid>:path`, `<oid>^`,
  `git ls-tree <oid>`, and `git cat-file -p <oid>` all resolve through
  an active replace ref instead of the literal named object.
  `GIT_REPLACE_REF_BASE` stripping alone does **not** help (verified
  by a real negative-control probe still exhibiting the substitution).
  `git write-tree` and `checkpoint_ref.py`'s `for-each-ref`/
  `update-ref` are **unaffected** (verified by direct real-repository
  probes in both `test_git_safety.py` and permanent regression tests
  added to `test_checkpoint_ref.py`). The accepted defense is the
  redundant pair `--no-replace-objects` (global flag) +
  `GIT_NO_REPLACE_OBJECTS=1` (env var) on every governed invocation —
  never either alone. `core.useReplaceRefs` is the real Git config key
  for this behavior (an earlier draft of this amendment incorrectly
  named a nonexistent `core.replaceRefs`); it is not relied on.

- **Pathspec magic survives `--`.** `--` separates paths from
  revisions but does not disable glob (`*`), single-character (`?`),
  bracket-class, or magic-keyword-prefix (`:(glob)`, leading `:`)
  pathspec interpretation. A literal filename containing these
  characters can match unrelated files during `git add` even with
  `--`. Verified with real decoy-file probes for all four forms.
  Defense: the redundant pair `--literal-pathspecs` (global flag) +
  `GIT_LITERAL_PATHSPECS=1` (env var). With this pair active, a
  glob-shaped literal path that matches nothing is correctly reported
  as missing (not ambiguously multi-matched) — see
  `test_lookup_staged_entry_rejects_multi_match_from_pathspec_magic`'s
  corrected expectation.

- **`Path.read_text()`/`Path.write_text()` perform universal-newline
  translation independent of any Git attribute**, on this session's
  Python (3.12, no `newline=` parameter on `read_text`). Fixed by using
  binary-safe `open(..., "rb"/"wb")` throughout `patch.py`'s I/O.

- A real submodule (gitlink, mode `160000`) does **not** need its
  referenced commit to exist locally for `git write-tree` to succeed
  (verified via a fresh clone that never fetched the submodule's own
  commit). This is the sole justification for excluding gitlinks from
  `check_index_blob_availability`.

- `git write-tree` (and therefore `git commit`, which calls it
  internally) re-invokes a `clean` filter even on already-staged
  content with no further working-tree change — confirmed via an
  isolated `write-tree` call, not only `commit`. This is why filter
  enumeration and the cached-attribute recheck are re-run immediately
  before each of `add`, `write-tree`, and `commit` independently in
  `patch.py`, never as one shared snapshot.

- `git cat-file --batch-check` requires stdin, which risks a pipe-buffer
  deadlock if written in full before its output is read (both ends can
  fill their OS pipe buffers simultaneously). `_git_safety._read_bounded`
  was extended to write via a concurrent daemon thread rather than a
  blocking pre-read write; `test_read_bounded_with_stdin_survives_pipe_buffer_pressure`
  reproduces real pipe-buffer pressure (300 objects) against this path.

### What is implemented

- `src/codeagent/_git_safety.py`: `--no-replace-objects` +
  `GIT_NO_REPLACE_OBJECTS=1` and `--literal-pathspecs` +
  `GIT_LITERAL_PATHSPECS=1` added to the shared hardened baseline (so
  `workspace.py` inherits both automatically); `ObjectFormat`/
  `detect_object_format`/`validate_oid`; a bounded, deadline-controlled
  binary subprocess seam (`run_git_bounded`/`_read_bounded`) with
  selector-based non-blocking reads, `limit+1` oversize detection,
  confirmed kill/reap with a `PROCESS_CLEANUP_UNCONFIRMED` outcome, and
  an optional concurrent stdin writer thread; path-safe
  `lookup_staged_entry`/`lookup_tree_entry` (never `:<path>` revision
  syntax); `check_index_blob_availability` (streamed `ls-files --stage
  -z`, deduplicated, gitlink-excluded, chunked `cat-file --batch-check`);
  `read_blob_bytes`/`read_commit_header` (structured pre-check, bounded
  retrieval, header-only parsing); a `cached` parameter on
  `check_filter_attribute_for_paths` for the working-tree attribute
  view.
- `src/codeagent/patch.py`: binary-safe bounded I/O
  (`MAX_PATCH_BLOB_BYTES = 65536`); a clean-staged-index precondition
  (`git diff --cached --quiet HEAD --`, three-way exit interpretation);
  object-format detection and OID validation on every reused OID;
  cached-and-working-tree filter/attribute safety checks before write
  and again before `add`; independent filter re-enumeration before
  each of `add`/`write-tree`/`commit`; a pre-mutation
  `check_index_blob_availability` gate; strict commit acceptance
  (capture expected parent/tree/staged-entry before `commit`, execute
  once, observe HEAD exactly once, structurally verify single parent,
  matching tree, matching target mode/OID, and exact-byte + SHA-256
  blob match before accepting — an unchanged HEAD or a
  structurally-mismatched HEAD is never accepted as success, and a
  reported `commit` failure with an exact structural match is accepted
  as success).
- `src/codeagent/errors.py` / `src/codeagent/events.py`: two new
  `ErrorCode`s, `PATCH_UNSUPPORTED_GIT_SUBSTRATE` and
  `PATCH_REPOSITORY_OBJECTS_UNAVAILABLE`, both `ErrorDomain.PATCH`,
  added to `ToolCompleted`'s enforced patch-error-code allowlist.
- `tests/integration/test_controller.py`: a test proving
  `_dispatch_apply_patch`'s returned `OperationalError` is the exact
  same object (`is`, not just equal `error_id`) forwarded into both
  `ToolCompleted.error` and `RunFinished.error`, for both new codes.
- `tests/unit/test_checkpoint_ref.py`: three permanent regression tests
  proving `observe()`/`advance()` are unaffected by a real replacement
  ref on the checkpointed commit — `checkpoint_ref.py` itself required
  no code change; no test failed against the existing implementation.

### Explicitly not covered by this amendment

- Linux validation. Everything above is macOS/Git 2.54.0 evidence only,
  consistent with this ADR's existing "acceptance is independent of ...
  Linux validation" framing.
- **Superseded by Amendment 2** (below): the `patch.py` apply path is
  now exercised end-to-end against a real `--object-format=sha256`
  repository, and `.gitattributes` targets now trigger a real
  repository-wide re-validation of every other tracked path's
  attribute safety.
- A live interrupted-commit (SIGKILL mid-`commit`) reproduction — as
  before, this remains explicitly inferred from S3's evidence and the
  worktree-replacement recovery model (ADR 0003), not independently
  reproduced for the hardened `patch.py` path.
- CI/Linux validation of any of the above; this amendment's changes are
  implemented and locally tested only — Linux CI validation pending.

## Amendment 2: correction pass on the patch.py-hardening slice

Recorded at implementation time, same environment as Amendment 1
(macOS, Git 2.54.0, arm64). This corrects several real gaps a review
pass found in Amendment 1's implementation before anything was
committed.

### Real defects found and fixed

1. **Only the `filter` attribute was ever checked.** `patch.py`'s
   attribute-safety check called `check_filter_attribute_for_paths`
   (filter only), never the other five ADR 0006 attributes
   (`text`/`eol`/`ident`/`working-tree-encoding`/legacy `crlf`) — a
   live `eol=crlf` or `ident` transformation on a patch target was not
   refused. Fixed by generalizing `_git_safety.check_filter_attribute_for_paths`
   into `check_attributes_for_paths` (queries all six attributes in one
   `check-attr` invocation, positionally verified path-major, per-path
   duplicate detection preserved) and wiring `patch.py`'s attribute
   check to use it. Real positive-control tests (`test_git_safety.py`
   and `test_patch.py`) reproduce live `eol`/`ident`/
   `working-tree-encoding` transformations and confirm refusal.
2. **The `.gitattributes` flow was documented as an accepted
   limitation instead of implemented.** A patch to `.gitattributes`
   itself never re-validated its effect on *other* tracked paths.
   Fixed (at the time): after staging a `.gitattributes` change
   (top-level or nested), `patch.py` enumerated every tracked path
   under `list_tracked_paths`'s existing fixed bounds and re-checked
   all six attributes for all of them against the current staged
   index, repeated again immediately before `write-tree` and before
   `commit`.
   **Superseded by Amendment 3 below**: a further probe found this
   repository-wide re-check itself relies on an untrustworthy premise
   — a `.gitattributes` target's own attribute classification can be
   masked by that same file's own staged content — so `.gitattributes`
   patch targets are now refused categorically before any mutation,
   and the repository-wide helper described here is preserved but not
   invoked. This paragraph is kept for the historical record of what
   was believed and tested at the time; it is not the current behavior.
3. **Several real Git-invocation call sites in `patch.py` could let a
   raw `_git_safety.GitSafetyError` escape `apply()` entirely**,
   becoming an unhandled exception instead of a structured
   `PatchResult` — found by systematically auditing every call into
   `_git_safety` from `patch.py`. Confirmed gaps: the attribute-safety
   helper's `enumerate_filter_neutralization`/`check_attributes_for_paths`
   calls, `lookup_staged_entry`, and essentially every `run_git` call
   in the write/stage/commit sequence (`add`, the staged-scope diff,
   the diff-bytes computation, `write-tree`, `commit` itself, and both
   `rev-parse HEAD` observations) — `run_git` itself raises
   `GitSafetyError` for a timeout or launch failure, not just a
   nonzero exit, and none of these were wrapped. All are now wrapped
   and mapped to the most precise available `ErrorCode`/failure type
   for their pre- vs. post-mutation position (an injected timeout at
   the pre-write clean-baseline check maps to
   `PATCH_UNSUPPORTED_GIT_SUBSTRATE`; everywhere else, post-write,
   maps to `PATCH_APPLICATION_FAILED`; a raised error during the
   `commit` invocation itself is deliberately swallowed and not
   branched on, matching the existing documented policy that the
   commit's own reported exit code is not dispositive — HEAD
   observation and structural verification decide the outcome either
   way). Six real injected-failure tests (timeout, a second timeout at
   a pre-write site, malformed attribute output, filter-enumeration
   failure, a staged-entry lookup failure, and a commit-verification
   `read_commit_header` failure) prove no `GitSafetyError` escapes
   `apply()` for any of these paths.
4. **`_git_safety.run_git_bounded` returned a nonzero-exit
   `BoundedProcessResult` silently** rather than failing categorically,
   which every current call site (`check_index_blob_availability`,
   `read_blob_bytes`, `read_commit_header`) treated as a footgun-prone
   pattern requiring its own returncode check. Fixed: the public
   `run_git_bounded` wrapper now raises
   `GitSafetyFailure.BOUNDED_COMMAND_FAILED` on a nonzero exit; the
   private `_read_bounded` primitive keeps its original neutral
   contract (never raises on nonzero exit) since only the public
   wrapper's callers uniformly wanted the stricter behavior. Each call
   site now catches `BOUNDED_COMMAND_FAILED` and re-raises its own more
   specific reason (`INDEX_STAGE_LISTING_UNAVAILABLE`,
   `BLOB_UNAVAILABLE`, `COMMIT_OBJECT_UNAVAILABLE`).
5. **`_read_bounded`'s cleanup path was not unified**, and its
   EOF-then-hang case was not classified as a timeout. Rewritten around
   a single `_terminate_and_confirm` abort path used for every failure
   after `Popen` succeeds (a read/oversize/timeout failure, an EOF
   followed by a `wait` timeout — now explicitly classified as
   `GIT_COMMAND_TIMEOUT` rather than silently accepted as success, a
   monitoring/selector setup failure — a new
   `GitSafetyFailure.PROCESS_SETUP_FAILED` — or any other unexpected
   exception). **A real deadlock was found and fixed while doing this**:
   the initial correction attempt closed the child's stdin from the
   main thread as part of abort, but a concurrent writer thread could
   already be blocked inside a `write()` call on that same file object,
   and Python's buffered-IO `close()` contends for the same internal
   lock as an in-flight `write()` — closing it concurrently deadlocked
   the abort path against the writer instead of unblocking it. Fixed by
   never closing stdin from the abort path; killing the child (which
   makes its read end disappear, delivering `BrokenPipeError` to the
   blocked writer) is what actually unblocks it, and the writer's own
   `finally` clause closes stdin once its write call returns. This is
   real, reproduced evidence for a documented deviation from this
   session's own correction instruction to "close stdin when
   aborting" — doing so unconditionally is unsafe. Five new tests cover
   a selector-setup failure, EOF-then-hang classified as timeout, a
   writer blocked on a full stdin pipe recovering cleanly once the
   child is killed, a writer that genuinely cannot be confirmed
   stopped (`PROCESS_CLEANUP_UNCONFIRMED`), and the pre-existing
   hung-child/launch-failure/cleanup-unconfirmed tests continuing to
   pass unchanged.
6. **`Path.resolve()`/`is_symlink()`/`is_file()` failures and a
   `UnicodeEncodeError` from caller-provided replacement text were
   unhandled** in `patch.py`'s path validation and content-preparation
   logic. Both are real, plausible inputs (a permission failure or
   symlink cycle; a model-proposed replacement containing an unpaired
   UTF-16 surrogate) neither previously caught. Fixed: both are now
   caught and converted to sanitized `_ValidationFailure`s via
   `from None` (so the original exception — which can embed an
   absolute path — is suppressed from the exception chain, verified via
   `__suppress_context__` and a full `traceback.format_exception` render
   in a white-box test, not just the persisted message string).

### Corrected documentation

- The docstring claim that `check_index_blob_availability` "streams"
  `git ls-files --stage -z` was imprecise: `_read_bounded` performs a
  bounded whole-output capture (`limit + 1` oversize detection), not
  incremental record-by-record parsing. The docstring now says this
  explicitly.

### What is additionally implemented (beyond Amendment 1)

- `_git_safety.check_attributes_for_paths` (all six ADR 0006
  attributes, generalizing the former filter-only function, which is
  now a thin backward-compatible wrapper `workspace.py` still uses
  unchanged), `GitSafetyFailure.PROCESS_SETUP_FAILED` and
  `BOUNDED_COMMAND_FAILED`, and a concurrent-writer-safe unified
  cleanup path in `_read_bounded`.
- `patch.py`'s `_check_repository_wide_attribute_safety` and
  `_is_gitattributes_path`.
- 6 new tests in `test_git_safety.py` (full-attribute defaults, three
  live-transformation positive controls, duplicate-path rejection,
  empty-attribute-names rejection) plus 2 new tests replacing a
  previously-inverted `run_git_bounded` nonzero-exit expectation, plus
  4 new bounded-reader cleanup tests — 181 total in that file (up from
  170).
- 22 new tests in `test_patch.py` (2 real full-attribute live-control
  end-to-end refusals, 3 `.gitattributes` repository-wide-revalidation
  tests, 6 injected-`GitSafetyError` mapping tests, 2 sanitization
  tests, 5 negative-commit-acceptance tests including the SHA-256
  end-to-end run, and a literal-pathspec end-to-end effectiveness
  test) — 42 total in that file (up from 29).
- Full suite: 1296 passed (up from 1267); the 3 real Docker
  `test_slice_c.py` tests passed with `CODEAGENT_REQUIRE_DOCKER=1` set.

### Explicitly not covered by this amendment

- Linux validation — unchanged from Amendment 1: macOS/Git 2.54.0 only.
- Linux CI validation of this amendment's changes.

## Amendment 3: nested `.gitattributes` self-masking — narrowed scope, not a new design decision

Recorded at implementation time, same environment as Amendments 1–2
(macOS, Git 2.54.0, arm64). This is a new empirical finding from a
further probe, not a reversal of ADR 0006's accepted decision (section
1's inspect-then-refuse-or-checkout control for `worktree add`, and the
shared attribute/filter-safety machinery in `_git_safety.py`, remain as
accepted). It narrows what `patch.py` claims to support for one
specific target shape.

### The finding

Amendment 2's `.gitattributes` repository-wide re-validation assumed
that querying `check-attr --cached` for the `.gitattributes` file's own
path, using the attribute state that file's own staged content
produces, was a valid way to classify that target's safety. A direct
probe disproves this:

```
# top-level .gitattributes:
sub/.gitattributes filter=parent

# sub/.gitattributes (the nested file's own content):
.gitattributes -filter
```

Querying `git check-attr --cached filter` for the path
`sub/.gitattributes` reports **`unset`** (a safe state) — the parent
directory's `filter=parent` assignment for that exact path is silently
shadowed by the nested file's own self-referential rule, because Git's
`.gitattributes` precedence gives the deeper directory's rules priority
for paths within it, and `sub/.gitattributes` is itself a path "within"
`sub/`. The unsafe parent assignment never surfaces in the query.

This means: **a `.gitattributes` target's own attribute classification
is not trustworthy when it is derived, even in part, from that same
file's own staged content** — which is exactly what both the
single-target check (existing since Amendment 1) and the
repository-wide re-check (Amendment 2) depend on for the
`.gitattributes` file's own path specifically. Neither check is sound
for this one target shape; both remain sound and unaffected for every
*other* tracked path (this masking is specific to a file governing its
own attributes, not a general flaw in `check-attr` or in
`check_attributes_for_paths`).

There is currently no trusted mechanism in this codebase to query a
path's governing parent-directory, `.git/info/attributes`, or global/
system attribute layers independently of the target file's own staged
content. Building one (e.g., synthesizing a `.gitattributes`-file-free
view of the same commit, or parsing the attribute file precedence
chain directly) is future work, not attempted here.

### The decision (scoped, not a reversal)

`GitPatchApplier.apply()` now refuses **any** `.gitattributes` patch
target — top-level or nested — categorically, before any read, write,
stage, or commit, via `PATCH_UNSUPPORTED_GIT_SUBSTRATE`. This is
narrower than Amendment 2's claim, not a new capability: Amendment 2's
repository-wide helper (`_check_repository_wide_attribute_safety`) is
preserved in `patch.py` for potential future reuse once a trusted
independent-layer inspection mechanism exists, but it is **not called**
from `apply()` and must not be described as a complete
`.gitattributes` safety control until that gap is closed. Every other
ADR 0006 control (the six-attribute check for non-`.gitattributes`
targets, replace-ref/literal-pathspec defenses, object-format
validation, bounded I/O, commit-acceptance verification) is unaffected.

### Evidence

- `test_nested_gitattributes_can_really_mask_a_parent_rule_for_its_own_path`
  (`test_patch.py`) reproduces the probe above directly against a real
  repository, independent of `patch.py`, confirming `check-attr`
  reports `unset` despite the parent's `filter=parent`.
- `test_apply_refuses_a_top_level_gitattributes_patch_target_before_any_mutation`
  and `test_apply_refuses_a_nested_gitattributes_patch_target`
  (`test_patch.py`) confirm `apply()`'s categorical refusal for both
  shapes, before any mutation (HEAD never moves, nothing is staged).
- Full suite: 1296 passed (unchanged count — 3 `.gitattributes` tests
  were replaced/removed, 3 added, net even); `test_patch.py` at 43
  tests (net even for the same reason).

### Explicitly not covered by this amendment

- A trusted, independent-of-target-content mechanism for inspecting
  parent/`.git/info/attributes`/global attribute layers — this remains
  unbuilt; `.gitattributes` patch support stays refused until it
  exists.
- Linux validation — unchanged: macOS/Git 2.54.0 only.
- Linux CI validation of this amendment's changes.

## Amendment 4 (Accepted 2026-09-17): `git diff` is a filter/external-diff/textconv execution point

Implementation status: **implemented (2026-09-17), locally verified
on macOS, and confirmed on Linux CI (commit `fafefbe`, run
[35306212472](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35306212472),
success: 1863 passed, 1 intentional Darwin-only skip, all 3 real-Docker
tests passed, no leftover verification containers).** `src/codeagent/
_git_safety.py` gained the planned `run_git_bounded_preview` primitive
(a bounded-preview counterpart to `run_git_bounded`: on overflow it
stops draining, terminates and confirms reaping of the child, and
returns a `complete=False` truncated result instead of raising and
discarding it — `run_git_bounded`'s own contract is unchanged), and
`src/codeagent/evidence.py`'s `FilesystemEvidenceSink` calls it for
both the governed `status` and `diff` commands, sharing one
`enumerate_filter_neutralization()` result across both per this
amendment's decision. Verified by `tests/unit/test_git_safety.py`'s 12
new `run_git_bounded_preview` tests (normal completion, exact boundary,
overflow, confirmed child termination, timeout, launch/setup failure,
cleanup-unconfirmed, binary bytes, baseline/argv ordering) and
`tests/unit/test_evidence.py`'s real positive-control hostile
external-diff, textconv, clean-filter, and process-filter tests proving
the production capture path suppresses all four — reproducing this
amendment's own findings against the real implementation, not only in
scratch probes. This status note does not revise anything in this
amendment's decision text below, which remains the accepted design as
written.

Original planning-time status, preserved for the historical record (now
superseded by the implementation status above): found during the
Milestone 2 Slice 2B-2 planning review (evidence-capture design), not
during any patch.py/workspace.py implementation pass. Real,
positive-controlled probes were run against scratch fixture
repositories outside this codebase. This amendment extends the ADR's
existing filter/hook-safety model to a command class no prior amendment
addressed; it does not revise or narrow anything in Amendments 1–3.

### New finding

`git diff` against working-tree content is a filter/external-diff/
textconv execution point, exactly as `git status` already is per this
ADR's existing racy-stat reasoning (section 4) — a previously
unaddressed gap, since no prior slice needed to run `git diff` against
live working-tree content.

- **`--no-ext-diff` and `--no-textconv` independently and correctly
  suppress `diff.external` and per-path `textconv` execution
  respectively** — proven with real positive controls: each driver
  fires without its corresponding flag, and is silent with it. (A
  per-path `textconv` probe must be run in a fixture isolated from
  `diff.external`, since an active `diff.external` driver shadows
  per-file `textconv` entirely — a real interaction found while
  isolating the two controls.)
- **Neither flag suppresses a `clean` or `.process` filter driver.** A
  hostile `clean` filter, and independently a hostile `.process`
  filter, both executed during a bare `git diff <commit> --` call with
  all four of `--binary --full-index --find-renames --no-ext-diff
  --no-textconv` already present — none of those flags touch filter
  execution.
- **`enumerate_filter_neutralization`'s existing overrides, already
  used for `git status`, suppress both** when applied to the same
  `git diff` invocation: the hostile `clean`-filter marker disappears,
  and the hostile `.process`-filter marker disappears (with the call
  then legitimately failing, exit 128, because a `required=true`
  driver with only `.process` configured and now-cleared has no
  `.clean` fallback — correct fail-closed behavior, already covered by
  "nonzero exit is a capture failure," not a new failure mode needing
  separate handling).
- **Purely observational `git diff` is completely blind to untracked
  files** under every flag combination tested; only read-only
  `git status --porcelain=v1` detects them. This is not a filter/hook
  finding but is recorded here because it shapes the same governed
  command's safe usage (ADR 0003 Amendment 2 section 2's untracked-path
  rule).

### Decision

- **Every governed `git diff` invocation against working-tree content
  is a governed command under section 1's secondary control**: filter
  drivers are enumerated and neutralized for it exactly as for `status`/
  `add`/`commit`, using `enumerate_filter_neutralization`. `--no-ext-diff`
  and `--no-textconv` remain independently required on the same
  invocation — the two defenses are additive, not substitutes for each
  other.
- **One logical operation that issues both a `status` and a `diff` call
  (e.g. evidence capture) enumerates filter configuration exactly
  once** and reuses that single bounded, validated neutralization
  snapshot, unchanged, for both calls. This prevents the two commands
  from ever being governed by independently-derived override sets (one
  call neutralized against a driver set the other never saw). **It does
  not claim protection against a concurrent local actor modifying Git
  configuration after enumeration** — such concurrent direct
  repository/configuration manipulation is a local-process-level attack
  already out of scope under threat-model assumption A4. Without A4,
  neither reusing one enumeration nor re-enumerating before each call
  would provide a race-free guarantee on its own; single-enumeration
  reuse is adopted here for consistency between the two calls, not as a
  race-closing control.
- **Both the governed `status` and `diff` calls are binary/NUL-safe and
  bounded.** `status` output is parsed NUL-delimited
  (`--porcelain=v1 -z`); `diff` output is treated as opaque binary
  bytes end to end, never decoded or re-encoded.
- **A new shared primitive, `run_git_bounded_preview`, is planned** in
  `_git_safety.py` for exactly this class of call (a bounded, binary,
  bulk-capture read where "stop early and report incomplete" is
  correct, as distinct from `run_git_bounded`'s existing "fail on
  overflow" contract):
  - Applies the section 5 hardened baseline exactly once per call, plus
    the caller-supplied filter-neutralization overrides.
  - Caps captured bytes at a caller-supplied bound (1 MiB for the
    current evidence-capture use).
  - On overflow (the bound-plus-one-th byte), terminates and reaps the
    child using the existing bounded-subprocess kill/reap machinery
    (`_read_bounded`'s unified `_terminate_and_confirm` abort path) and
    reports `complete=False` with the preview bytes captured so far —
    never continuing to drain the remainder for an exact total or a
    full-stream hash.
  - An unconfirmed child-process cleanup after overflow **fails
    closed** — reported as a structured process-cleanup failure, not
    silently treated as a successful truncation.
  - `run_git_bounded`'s existing semantics (whole-output capture,
    categorical failure on nonzero exit) are **unchanged** — this is a
    new, additional primitive for a different contract, not a
    modification of the existing one.

### Consequences

- ADR 0003 Amendment 2's evidence-capture design (section 3 there)
  depends on this amendment's neutralization requirement and planned
  `run_git_bounded_preview` primitive; neither exists in production
  code yet.
- This amendment does not change T-M3's status: `git diff` was never
  previously a governed command in production code, so this is new
  scope being brought under the existing policy, not a regression being
  fixed.

### Evidence

Real, positive-controlled probes in scratch fixture repositories
(outside this codebase): single-endpoint `git diff <commit> --`
combining committed/staged/unstaged changes; isolated `diff.external`
and `textconv` positive/negative controls; a hostile `clean` filter and
a hostile `.process` filter both firing during unneutralized `git diff`
and both suppressed by `enumerate_filter_neutralization`'s existing
overrides; and untracked-file blindness under every flag combination
tested. No implementation or test exists yet in this repository for any
of the above; `run_git_bounded_preview` and its callers are the
implementation obligation this amendment creates.

## Amendment 5 (Accepted 2026-11-09): refuse unrenderable filter-driver names; neutralize the workspace entry gate (mandatory pre-D5-C hotfix)

**Status.** Accepted 2026-11-09, after joint review. **Implemented, locally
verified on macOS (2026-11-09), committed as
`067fca0767f140a0e1f4a20afbe4b3eeae0a80e8` (an ordinary fast-forward over
`5422046`), and Linux-CI-confirmed in run 42908777385** (A5.9 records the
local evidence, A5.10 the Linux CI evidence). The acceptance decision is
unchanged. (Historical: before its commit this read "Implemented and locally
verified on macOS (2026-11-09); unstaged, uncommitted and not
Linux-CI-confirmed", and at insertion it read "Accepted 2026-11-09, after joint
review; not implemented." Both were true then.) The
accepted-content SHA-256 of the external draft (revision 3) is
`ab57fbf9f54fc8a06e23d448e418c8d621eddc07b6fcaae2c09e165c2e583722`.
(Historical: the heading read "Proposed 2026-11-09", and this status read
"Proposed. Not accepted and not implemented.") This is the mandatory
pre-D5-C hotfix (D-23, D-25; D5-C plan revision 9 §5). D5-C plan revision 12
governs D5-C and excludes this hotfix's effort, estimated separately at
10–18 h (low–medium confidence). That estimate is not entered in the ledger.

### A5.1 Finding

**Override parsing.** Section 1's secondary control renders one override per
configured `clean`, `smudge` and `process` subkey, as
`-c filter.<name>.<subkey>=<value>`. Git splits a `-c` argument at its
**first** `=`. For a driver named `a=b`, the override sets key `filter.a`,
and the configured helper still runs. This was reproduced against this
module on Git 2.54.0, with a positive control. Nothing refuses the name, so
the control fails open.

**Entry gate.** `GitWorktree.entry_gate()` runs `diff --quiet HEAD --` and
`status --porcelain=v1 --untracked-files=all` without neutralization.
- Each one ran a configured clean filter on a stat-dirty tracked file,
  independently of the other.
- `diff --cached --quiet HEAD --` did not.

### A5.2 Threat and security boundary

**Threat.** A filter helper in repository, global or system configuration,
or one attributed through `info/attributes`, executes on the host with the
invoking user's privileges during an observation this ADR promises is
filter-free. Committed `.gitattributes` alone cannot configure a driver.

**Established:**
1. No governed `status` or `diff` in `workspace.py` or `evidence.py` runs
   while a driver name that cannot be neutralized is configured.
2. The entry gate's two filter-capable observations are neutralized.

**Not established:**
- protection against configuration rewritten between enumeration and launch;
- any filter-capable call site not listed in A5.4.

### A5.3 Neutralization contract

1. **Renderability.** A driver name is renderable if and only if it contains
   no `=`. Renderable names (including `a.b`, `x.clean`, `é` and `A`) keep
   today's byte-identical rendering.
2. **Rejection.** If any configured `filter.*` driver name is unrenderable,
   `enumerate_filter_neutralization` raises
   `GitSafetyError(FILTER_DRIVER_NAME_UNRENDERABLE, <fixed message>)`.
   - It does so whichever subkeys the driver configures, including none.
   - Order: parse configuration; enforce the existing bounds; test every
     name with the private predicate `_driver_name_renderable`; on any
     `False`, raise the refusal directly; only then verify the passthrough
     and render the overrides.
   - There is no environment-variable fallback and no partial
     neutralization.
3. **Confidentiality.**
   - **Where the refusal is raised.** The refusal is raised directly, in
     straight-line code, outside every exception handler. The path has no
     internal exception class, handler or `from`.
   - **What the refusal carries:**
     - its message is one constant;
     - `args` is that message alone;
     - `__cause__ is None` and `__context__ is None`;
     - its attributes are only `reason` and `message`.
   - **What is never reachable.** No driver name, configured value, helper,
     host or repository path, Git stderr, or other exception is reachable
     from it.
   - **Consumers.** Consumer failures chain `from` the refusal, which
     retains diagnostics, but they add none of those values. The entry
     gate's fixed `GitWorktreeError` has exactly one cause, the sanitized
     `GitSafetyError`, exposing only `reason` and its fixed message. The one exception is `patch.py`'s existing
     sanitized repository-relative approved target fragment, which is
     retained.
4. **Safe execution.** When every name is renderable, each governed
   filter-capable command runs with the rendered overrides placed before
   its subcommand.
5. **Interrupts.** The only new handler is the entry gate's
   `except GitSafetyError`. `KeyboardInterrupt`, `SystemExit` and other
   non-`Exception` `BaseException`s propagate unchanged, by identity, under
   ADR 0005 Amendment 1.

### A5.4 Call sites

**Refusal propagates, with no consumer code change:**

| Consumer | Location | Failure |
|---|---|---|
| evidence `status` and `diff` | `evidence.py` | `EvidenceCaptured`, capture-failed |
| `snapshot_source()` `status` | `workspace.py` | `GitWorktreeError` |
| creation-time attribute inspection, before `checkout` | `workspace.py`, via `evaluate_tracked_filter_safety` | `GitWorktreeError`, then the existing exact cleanup |
| target-attribute checks and their re-checks | `patch.py` `_check_attribute_safety` | validation or application failure |
| repository-wide re-validation | `patch.py` `_check_repository_wide_attribute_safety` | application failure |

**Newly neutralized: the entry gate.**
- One enumeration runs immediately before `diff --quiet HEAD --`. Its
  immutable overrides are applied to that command and to the following
  `status`.
- An enumeration failure becomes a fixed `GitWorktreeError`, raised `from`
  the sanitized `GitSafetyError`, consistent with `snapshot_source()`. It
  maps to `WORKSPACE_ENTRY_GATE_FAILED`.
- The `HEAD` and `diff --cached --quiet` checks are unchanged.

**Consequence.**
- One successful worktree recreation launches **22** Git processes, not 21.
- The recreation wait-bound statement is owned by **ADR 0004**. Its §21.3a
  and §21.17 are updated in the same change:
  - the pin moves to 22;
  - the configured subprocess component grows by at most one 30 s timeout;
  - the approximate configured range moves from 576–728 s to **606–758 s**;
  - filesystem and `fsync` time, and the direct-child timeout residual,
    stay unbounded.
- The launch-site inventory (L1–L7) is unchanged.

### A5.5 Test obligations

All tests use real Git. Outcomes are captured as values and asserted with
plain `assert`.

**Fixture.** A hostile `clean` or `process` helper that touches a sentinel,
attributed through common-directory `info/attributes`, with a stat-dirty
tracked file.

**Names:**
- refused: `a=b`, `=a`, `a=`, `x.y=z` and `a=SENTINEL-<uuid>`;
- also refused: a name configuring only `required`.

**Controls:**
- renderable names `ab`, `a.b`, `x.clean`, `é`;
- fixture sanity (the helper runs with no override);
- a **positive control**: with `_driver_name_renderable` patched to return
  `True`, the helper runs.

**Per consumer:**
- the fixed failure;
- a cycle-safe walk that reaches no hostile value and no exception outside
  the expected chain;
- exact Git launch counts: one `config` and zero downstream filter-capable
  launches;
- sentinel absent;
- unchanged `HEAD`, refs, index bytes, worktree bytes, worktree
  registrations, configuration and admin entries.

**Confidentiality tests:**
- **No retained failure:** with a sentinel-bearing driver name, the surfaced
  low-level refusal has `__cause__ is None` and `__context__ is None`, and
  its cause, context, args and attributes contain neither the sentinel nor
  any other exception.
- **Fixed message:** an exact fixed-message assertion plus the reachability
  walk.

**Entry gate:**
- passes with a hostile helper for a renderable name, with no sentinel,
  the exact 5-launch sequence, and identical override tokens on both
  commands;
- refuses `a=b` after 3 launches, with the exact safe chain:
  - `GitWorktreeError(fixed)`, whose cause is `GitSafetyError(reason, fixed)`;
  - that inner error has no cause and no context;
  - nothing else is reachable;
- an enumeration failure fails closed;
- `KeyboardInterrupt`, `SystemExit` and direct-`BaseException` instances
  propagate by identity, with no later launch.

**Recreation pin:** 22, differing from the previous launch list by exactly
one `config` entry.

### A5.6 Mutation obligations

Six categories, six concrete runs, under the strict assertion-only rule:
- a kill counts only when the catching test fails with an `AssertionError`
  in a pytest session that ends normally with exit code 1;
- sources are restored and SHA-256-verified.

| ID | Mutation | Caught by |
|---|---|---|
| H-1 | `_driver_name_renderable` always returns `True` | refusal test: the outcome is the fixed refusal |
| H-2 | the entry gate's `diff --quiet HEAD --` un-neutralized | entry-gate test: sentinel absent |
| H-3 | the entry gate's `status` un-neutralized | the same test: sentinel absent |
| H-4 | the new entry-gate handler widened to `BaseException` | interrupt test: exact identity |
| H-5 | the direct refusal chained from a synthetic name-bearing failure (`from ValueError(name)`); production has no such failure | retention test: `__cause__ is None`, and an empty walk |
| H-6 | the refusal message includes the driver name or another hostile value | fixed-message test: exact message, and an empty walk |

### A5.7 Landing and Linux-CI gate

**Before commit:**
- the affected test files, forward and reverse;
- the full suite with `CODEAGENT_REQUIRE_DOCKER=1`;
- all six mutations caught;
- `git diff --check` and `git diff --cached --check`;
- no leftover containers, worktrees, `refs/codeagent` refs, processes or
  state roots.

**Commit contents:**
- the hotfix code and tests;
- this amendment;
- the ADR 0004 bound note;
- `CLAUDE.md`;
- an append-only `ENGINEERING_LOG.md` entry;
- optionally, a ledger status/dependency note with no hours.

**Index discipline:**
- Every Proposed D5-C draft stays unstaged.
- The ADR 0004 and ADR 0006 index blobs are built from `HEAD` plus only the
  hotfix content. Patches are generated against `HEAD`, checked with
  `git apply --cached --check`, and applied.
- The intended blob hashes are recorded and verified, and the worktree is
  verified to still hold the renumbered D5-C drafts.
- Every other path is staged explicitly. `uv.lock`, `git add -A`,
  `git add .` and wildcards are never used.

**Gate.**
- The commit is an ordinary fast-forward, followed by a successful
  push-triggered Linux CI run on that exact commit. Its evidence is recorded
  here afterwards.
- **D5-C** (ADR 0006 Amendment 6, the D5-C amendments to ADRs 0003, 0004
  and 0007, and any D5-C implementation) **cannot begin** until this hotfix
  is committed and Linux-CI-confirmed.
- D5-C reuses `FILTER_DRIVER_NAME_UNRENDERABLE`.

### A5.8 Non-claims

This amendment does not add:
- typed Git capabilities, a capability registry, a filter-facts snapshot,
  a private index, a freshness guard or the four-operation protocol;
- a change to `FilterNeutralization`, attribute classification, the hardened
  baseline, the executor, the controller, the events, the error codes or the
  workflows;
- operator reachability, Gate α or v1.

T-E1, T-F1 and T-F2 are unchanged. D5 stays In Progress.

### A5.9 Implementation record (2026-11-09; local, macOS)

(Historical: this heading read "(2026-11-09; local, macOS; uncommitted)"
before the commit.)

**Production.**
- `_git_safety.py`:
  - `GitSafetyFailure.FILTER_DRIVER_NAME_UNRENDERABLE`;
  - `_UNRENDERABLE_MESSAGE`;
  - `_driver_name_renderable(name)` (`"=" not in name`);
  - in `enumerate_filter_neutralization`, after parsing and the existing
    bounds, a direct refusal for any unrenderable name, before
    `_verify_fixed_cat_path()` and rendering.

  No handler or internal exception was added.
- `workspace.py`: `entry_gate` enumerates once, immediately before
  `diff --quiet HEAD --`. Only `GitSafetyError` is translated, to the fixed
  `GitWorktreeError` raised `from exc`. The same `neutralization.args` are
  applied to the tree diff and to `status`. The `HEAD` and cached-diff checks
  are unchanged.

**Tests.**
- New tests in `test_git_safety.py`, `test_workspace.py`, `test_evidence.py`
  and `test_patch.py`, plus the shared support module
  `tests/support/hostile_filter.py` (a hostile helper and sentinel, a
  `Popen` launch spy, and a cycle-safe reachability walk).
  - The support module is a disclosed deviation: the plan listed only test
    files.
- `test_worktree_recreation.py`: `RECREATION_GIT_LAUNCHES = 22`. The pinned
  launch list now also asserts the gate's `diff --cached`, `config`,
  `diff --quiet`, `status` sequence.
- The refused-creation launch list is pinned for both the tempdir and the
  reservation variants: `rev-parse`, `worktree add`, `read-tree`, `config`,
  `worktree remove`, `worktree list`.
  - This is a measured pin. Its prefix up to `config` equals that of a
    successful creation, and `checkout` is never launched.

**Local evidence** (macOS, Git 2.54.0, Python 3.12.16, real Docker,
`CODEAGENT_REQUIRE_DOCKER=1`):
- **Affected set** (`test_git_safety`, `test_workspace`, `test_evidence`,
  `test_patch`, `test_worktree_recreation`, `test_controller_recreation`,
  `test_patch_failure_continuation`, `test_subprocess_inventory`,
  `test_errors`, `test_events`): 3,437 passed, forward and reverse.
- **Full suite:** 7,116 passed, 2 skipped. The skips are the two known
  non-UTF-8-filename tests in `test_repo_tools.py`, which macOS refuses.
- **Mutations:** H-1 to H-6, one canonical run each, all **killed**. Each
  kill was an assertion failure in a normally completed pytest session
  (exit 1, no errors). Sources were restored and verified by SHA-256 after
  every run.

  | ID | Failing tests | Designated assertion |
  |---|---|---|
  | H-1 | 8 | the refusal outcome |
  | H-2 | 1 | sentinel absent |
  | H-3 | 1 | sentinel absent |
  | H-4 | 3 | `got is inst` |
  | H-5 | 1 | `__cause__ is None` |
  | H-6 | 1 | the exact fixed message |
- `git diff --check` is clean.
- **Cleanup checks:**
  - no `codeagent-(verify-|baseline-|verification-)` containers;
  - one registered worktree, the main checkout;
  - no `refs/codeagent` refs;
  - no leftover test processes;
  - no default state root.
  - Two `codeagent-fixture-*` temporary directories dated 2026-11-08
    predate this pass and were left untouched.

**Prior mutation-harness invalidation audit (2026-11-09/10).**

**Method.**
- Membership was computed mechanically, not from memory:
  - every string literal naming a `src/codeagent/*.py` or `tests/*.py` path
    was read from each preserved harness script;
  - for D4b, the per-mutation entries were read from
    `d4b_mutations_strict.py`, which `run_d4b_inv.py` loads;
  - the transitive `tests.*` import closure of every selected test was
    computed, including `from tests.support import <module>` and imports
    nested inside functions.
- There is no `conftest.py`.
- The changed set (checked against `git diff --name-only` plus untracked
  files) is `_git_safety.py`, `workspace.py`, the five modified test files
  and `tests/support/hostile_filter.py`.
- A harness is invalidated if it mutates a changed source, selects a changed
  test, or imports `hostile_filter.py` directly or transitively.

| Harness (script) | Mutates | Selects | Intersection | Status |
|---|---|---|---|---|
| D5-B (`d5b/mut_d5b.py`) | `controller.py`, `_lifecycle_run.py`, `patch.py`, `executor.py` | `test_controller_recreation`, `test_worktree_recreation`, `test_patch_failure_continuation`, `test_patch` | `test_patch`, `test_worktree_recreation`, `hostile_filter` | **invalidated** |
| Step 7 (`d5b/mut7_d5b.py`) | `_lifecycle_run.py` | `test_worktree_recreation`, `test_controller_recreation` | `test_worktree_recreation`, `hostile_filter` | **invalidated** |
| Step 8 (`mut8.py`) | `_lifecycle_run.py`, `reconciliation.py`, `worktree_absence.py` | `test_reconciliation`, `test_worktree_absence`, `test_reconciliation_s9`, `test_worktree_recreation` | `test_worktree_recreation` (direct, and imported by `test_reconciliation_s9`), `hostile_filter` | **invalidated** |
| Step 6 (`d5b/mut6b_d5b.py`) | `controller.py` | `test_controller`, `test_controller_recreation` | none (support: `fakes`, `scripted_model`) | not invalidated |
| D5-A (`d5b/mut_d5a_d5b.py`) | `controller.py`, `model_protocol.py` | `test_patch_failure_continuation`, `test_model_protocol`, `test_domain` | none | not invalidated |
| D4b (`d5a/d4b/d4b_mutations_strict.py`) | `controller.py`, `model_protocol.py`, `tool_schema.py`, `_lifecycle_run.py`, `domain.py`, `cli.py`, `_read_adapter.py`, `trace_assertions.py`, a fixture test | `test_d4b_model_loop`, `test_d4a_read_path`, `test_d4b_e2e`, `test_tool_schema`, `test_lifecycle_run` | none (0 of 82 mutations) | not invalidated |
| Steps 1–5 | — | — | — | No separate harness is recorded in ADR 0004 Amendment 20 §14, §21.17 or `ENGINEERING_LOG.md`. The Steps 1–8 mutation evidence is the Step 6, 7 and 8 harnesses. |

**Reruns of the invalidated harnesses** (macOS, `CODEAGENT_REQUIRE_DOCKER=1`,
`PYTHONDONTWRITEBYTECODE=1`):
- **Anchors and scripts:** every anchor applied exactly once. No harness was
  missing or rebuilt, and no mutation was re-anchored, retired or
  superseded.
- **Sequence:** each harness ran clean baseline, then every mutation, then
  clean baseline.
- **Kill rule:** a kill counts only for exit 1 with at least one
  `AssertionError`/`assert` failure and no other failure, error or
  interruption.
- **Restoration:** each harness restores the mutated source and asserts its
  SHA-256 after every run, and a whole-tree SHA-256 over tracked and
  untracked files was compared before and after.

| Harness | Baseline 1 | Mutations | Baseline 2 | Tree before = after |
|---|---|---|---|---|
| D5-B | 1,266 passed | 44/44 caught by assertion (43 categories, 44 runs) | 1,266 passed | `ea5efa3f…74f0cc7`, yes |
| Step 7 | 1,162 passed | 33/33 | 1,162 passed | yes |
| Step 8 | 743 passed | 24/24 | 743 passed | yes |

- D5-B's baseline is 1,266 rather than §21.17's 1,264 because of the hotfix's
  two new tests in `test_patch.py`.
- **System sleep.** The Mac entered clamshell and maintenance sleep during
  the Step 8 run.
  - S8-M16, S8-M20 and S8-M21 overlapped a sleep, in the harness run and
    again in a first targeted rerun. Each time they were caught by
    assertion only, with the tree unchanged.
  - They were rerun a third time under `caffeinate -s -i` in a window with no
    sleep or wake events (2026-11-10 15:37:54–15:48:24). Results: S8-M16 10
    assertion failures, S8-M20 18, S8-M21 24; zero other failures or errors;
    the tree unchanged.
  - D5-B, Step 7, both Step 8 baselines and S8-M01–M15, M17–M19 and M22–M24
    ran while awake. The pmset log shows no sleep before 20:56.
- **Restored sources** are byte-identical to `HEAD`:
  - `controller.py` `f90c8c7a…`;
  - `_lifecycle_run.py` `48b73d96…`;
  - `patch.py` `b23163c3…`;
  - `executor.py` `d54f3622…`;
  - `reconciliation.py` `17e13fdd…`;
  - `worktree_absence.py` `77db7e24…`.
- **No repository change.** No test, support, fixture, helper or production
  file changed during the audit, so the affected-set (3,437, forward and
  reverse) and full-suite (7,116 passed, 2 skipped) results above remain
  valid. Only external harness drivers were added.

**Limits.**
- This section is local macOS evidence. (Historical: before the commit this
  bullet said "There is no commit and no Linux CI run yet", which was true
  then. The commit and Linux CI evidence are in A5.10.)
- H-2 and H-3 share one test and one assertion (sentinel absent). The plan
  probe showed each command runs the helper independently, but the test does
  not identify which command did.
- The `process` subkey is exercised with a helper that exits at once.
- The A4 same-user configuration race between enumeration and launch
  remains.

### A5.10 Linux CI evidence (2026-11-10)

**Commit and push.**
- Commit `067fca0767f140a0e1f4a20afbe4b3eeae0a80e8` ("fix: harden Git filter
  neutralization"), parent `5422046df62a90876aa6a5f051ebdbc9b881fb17`.
- Pushed to `main` as an ordinary fast-forward, `5422046..067fca0`, with no
  force push.
- The commit holds the hotfix code, tests, this amendment, the ADR 0004
  bound notes, `CLAUDE.md`, `ENGINEERING_LOG.md` and the ledger note.
- ADRs 0004 and 0006 were staged partially, as `HEAD` plus only the hotfix
  content:
  - ADR 0004 blob `c41137466b6557372d9f22bd38f8e692594abcba`;
  - ADR 0006 blob `f42046a7b804208aa8a848520a861f98f72c0a88`.
- The Proposed D5-C drafts (ADRs 0003, 0004, 0006 and 0007) stayed
  unstaged.

**Run** [42908777385](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42908777385),
re-verified independently through the Actions API and a freshly fetched raw
job log (523 lines, SHA-256 `8f0a33447dd648443ad61700118f182db63817a2fde8226c32631219cf9b746d`):
- **Run:** push-triggered on `main` for exact head
  `067fca0767f140a0e1f4a20afbe4b3eeae0a80e8`, attempt 1, `completed` /
  `success`.
- **Job:** exactly one, "Test (ubuntu-24.04, Python 3.12)", 21:49:42Z to
  21:54:17Z. All 13 reported steps `success`; zero annotations and zero
  `##[error]` lines.
- **Environment:**
  - runner image `ubuntu-24.04`, version 20261101.360.1 (Ubuntu 24.04.5,
    x86_64);
  - CPython 3.12.16 and pytest 9.1.1;
  - Git 2.56.0;
  - Docker Engine - Community 28.0.4, client and server.
- **Docker:**
  - the mandatory preflight succeeded;
  - `CODEAGENT_REQUIRE_DOCKER: 1` is present in both test steps' logged
    environments;
  - the pinned
    `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
    reported platform `linux/amd64`.
- **Dedicated Docker step** (`pytest -v` over `test_slice_c.py`,
  `test_cancellation_e2e.py` and two named `test_patch_failure_continuation.py`
  nodes): `18 passed in 20.44s`, all 18 named `PASSED`:
  - the four `test_slice_c.py` tests;
  - E1–E10, E13 and E14;
  - `test_d5a_stale_expected_text_continues_to_passing_verification_real_docker`;
  - `test_d5b_contaminated_worktree_recreates_and_passes_real_docker`.
- **Complete suite** (`python -m pytest -q`): `7114 passed, 4 skipped in
  234.92s (0:03:54)`, with zero failed and zero errors. 7,114 + 4 = 7,118,
  which equals the local collected total (7,116 passed + 2 skipped).
- **Leftover check:** the anchored
  `^codeagent-(verify-|baseline-|verification-)` listing over a full
  `docker ps -a` name list printed nothing after "currently present
  (expected: none):", and the step succeeded.

**Evidence limits.**
- **No hotfix-specific named test.** The 18 dedicated-step tests are named
  evidence, but none of them is a hotfix-specific test. (By source
  inspection, not log evidence, the D5-B real-Docker node runs a recreation,
  and therefore the changed entry gate.)
- **Inferred, not named:** the hotfix tests, the refused-creation launch pin
  and the recreation launch pin of 22 ran only in the quiet suite. Their
  execution is inferred from the whole-suite command, the matching
  collection total and zero failures; no log line names them.
- **Skips:** the four quiet-suite skips are unidentified. They are not
  attributable to Docker being unavailable, since Docker was required and
  available in that step.
- **Local only:** the six hotfix mutations (H-1–H-6) and the prior-harness
  reruns (A5.9) remain local macOS evidence.
- **Scope:** this is GitHub-hosted `ubuntu-24.04` x86_64 automated-test
  evidence, not a security review and not ARM64 evidence.
- **Correction.** The pre-reconciliation report of this run listed only 16
  of the 18 named dedicated-step tests. It omitted the two
  `test_patch_failure_continuation.py` real-Docker nodes. The figure above,
  18, is the verified one.

**Unchanged by this evidence:**
- D5 stays In Progress, and D5-C is not implemented.
- The mandatory pre-D5-C gate (A5.7) is satisfied.
- No Gate α, operator-ready or v1 claim is made.
- T-E1, T-F1 and T-F2 are unchanged.
