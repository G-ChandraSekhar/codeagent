# ADR 0004: Owned-resource lifecycle, registry, attribution, cleanup, and reconciliation

Status: Accepted (2026-09-15). Amended 2026-09-18 by Amendment 1
(Accepted): Milestone 3 Slice 3A-1's state-root, trusted-repository-
identity, and repository/generic-lock substrate design — see the end
of this document. Everything above "Amendment 1" is the original
decision, unchanged.

Implementation status: **partially implemented.** This ADR's
Milestone 2 prerequisites exist in production code: generated
lifecycle IDs, `CheckpointSession`'s in-memory transition record,
`RunController` integration, gated worktree/checkpoint-ref teardown,
and evidence capture (all Milestone 2 Slice 2B-1/2B-2, per
`CLAUDE.md`). Slice 3A-1's state-root/identity/lock substrate
(Amendment 1) is now **implemented, locally validated on macOS, and
confirmed on GitHub-hosted Linux CI**: `state-root.json` init/
validation, trusted repository identity and context discovery,
`repo.json` creation/validation, and the repository/generic lock
primitive — see Amendment 1's "Implementation status" note below for
the exact module list, verification, and scope. Slice 3A-2
(`lifecycle_store.py`) now additionally implements
`runs/<lifecycle-id>/` exclusive creation, the lifecycle-lock wrapper
(`acquire_lifecycle_lock`), and the atomically published initial
`PREPARING` `lifecycle.json` projection — also implemented, locally
validated, and confirmed on GitHub-hosted Linux CI (commit `8aefa5f`,
run
[35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790),
`ubuntu-24.04` x86_64, Python 3.12, success) — see §16's own
"Implementation status" note (added after step 7) for the exact module
list, verification, and scope. Both slices remain **unwired**: no
controller or CLI integration exists. The rest of the durable
Milestone 3 lifecycle substrate this ADR describes remains entirely
unimplemented: durable attribution, reconciliation, abandonment, and
the maintenance trace. See "Implementation order" below.

## Context

Every CodeAgent run creates host resources that can outlive the process
that created them: a disposable Git worktree, verification containers,
and a hidden checkpoint ref (ADR 0003 Amendment 1, Accepted). An
uncatchable SIGKILL or a crash prevents any in-process cleanup
(`docs/threat-model.md` T-F1, T-F2).

Stage-2 spike S5 (`spikes/s5/S5_RESULT.md`) exercised interruption and
reconciliation against real Docker and real Git worktrees on two
separate evidence domains (threat model A9): macOS/arm64 Docker
Desktop (`spikes/s5/evidence/macos-docker-desktop-arm64/run-ffd711753021/`)
and Linux/x86_64 GitHub Actions
(`spikes/s5/evidence/linux-x86_64/run-34783737248-attempt-1/`). Both
authoritative runs recorded all seven classifications as PASS, and the
29 core lifecycle/reconciliation functions are identical between the
two harness versions.

Observed live on both platforms:

- SIGKILL after READY left the labeled container and the registered
  worktree in place, with no post-action acknowledgement.
- An advisory `flock` lock excluded other processes while its exact
  holder lived and became acquirable after that holder died. PID
  identity was never the authority. **PID reuse was not induced or
  tested.** The Linux lock/worktree filesystem was observed as ext4;
  the macOS filesystem type was not recorded.
- A fresh process recomputed identity from trusted inputs, validated
  container ID/labels and worktree registration, removed the container
  first and confirmed it absent, then removed the worktree and
  confirmed it absent.
- Unlabeled, similarly named, and foreign-session canaries, and a live
  concurrent run, were left untouched; a second pass was idempotent.
- Intent was written to the manifest before each side effect.

Proven only by mocked, Docker-free focused tests: terminal entries take
zero lock/inspection/mutation calls; malformed manifests, identity
mismatches, stale flags, and registration/directory mismatches cause
zero mutation; unconfirmed container removal leaves the worktree
untouched; failed reconciliation retries with fresh inspection; a
container with no recorded ID can be attributed by exact name plus
labels (`test_preflight_container_id_null_allowed_when_labels_match` —
in both authoritative live runs the SIGKILL occurred at READY with the
container ID already recorded, so the null-ID path is design reasoning
plus mocked-test evidence, not live evidence).

Not exercised by S5: bind-mounted worktrees and production container
security flags (S5 containers had neither), repository locks, inode
verification of lock files, multiple containers per run, worktree
recreation, hidden refs, PID reuse, reboot, network filesystems, and
power loss.

S5 code is spike-only scaffolding. **No spike code is imported into or
reused by production**; the production implementation reimplements the
demonstrated principles with its own tests.

Current production (`src/codeagent/workspace.py`,
`src/codeagent/executor.py`) has none of this: worktrees use a random
`tempfile` path that cannot be recomputed after a crash and fall back to
`shutil.rmtree` plus a repository-wide `git worktree prune`; containers
carry no labels, the ID returned by `docker create` is discarded,
removal is confirmed by name only, and Docker calls have no timeouts.

Cancellation and signal ownership are decided separately in ADR 0005.

## Decision

### 1. Identities

- The public, user-visible `run_id` contract (events, reports, UI) is
  unchanged.
- Each run is assigned a separate generated **`lifecycle_id`**:
  128-bit lowercase hex (`secrets.token_hex(16)`, `^[0-9a-f]{32}$`).
  It is the only identity used for paths, locks, container names,
  labels, hidden refs, and recovery. It is never derived from `run_id`.
  The lifecycle projection records `run_id` diagnostically.
- **`state_root_id`**: 32 lowercase hex, stored once in
  `state-root.json`.
- **`repo_key`**: the first 32 hex characters of
  `sha256("codeagent.repo-key.v1\0" + canonical_common_dir_bytes)`,
  where the canonical path is derived from the operator-supplied
  (trusted) repository's absolute Git common directory. Case-variant
  aliases of the same repository must produce the same key (intended
  mechanism on macOS: the filesystem's case-canonical path via
  `fcntl.F_GETPATH` on the opened directory; proven by an
  implementation test, not assumed).

### 2. State root

- Location: `CODEAGENT_STATE_DIR` if set (must be absolute, otherwise
  refuse). Otherwise Linux `$XDG_STATE_HOME/codeagent` when
  `XDG_STATE_HOME` is absolute, else `~/.local/state/codeagent`; macOS
  `~/Library/Application Support/CodeAgent`.
- Validation at startup: resolve the root once. Refuse if the resolved
  root lies inside the trusted repository's working tree or Git common
  directory, or if the trusted repository lies inside
  `<root>/worktrees`. Below the resolved root, every managed component
  must be a real directory (never a symlink), owned by the current uid,
  not group- or world-writable, and created with mode `0700`; managed
  files are opened with `O_NOFOLLOW`.
- Initialization of `state-root.json`:
  1. If `state-root.json` exists, validate it (step 4).
  2. Otherwise list the root. If it contains any entry other than
     `state-root.json`, re-attempt step 1 once; if the file is still
     absent, return `SUBSTRATE_UNAVAILABLE` — managed repository, run,
     or worktree state without an identity file is never adopted, and a
     root that is not otherwise empty is never initialized.
  3. A genuinely new, otherwise-empty root creates `state-root.json`
     with `O_CREAT | O_EXCL | O_NOFOLLOW`, writes
     `{schema_version, state_root_id}`, then `fsync`s the file and the
     directory. No managed state is created before this completes.
  4. A process that loses the creation race (`EEXIST`) rereads the
     winner's file, retrying within a short bounded window while it is
     empty or partial, then validates schema and ID format.
  5. A missing-but-required, invalid, or corrupt identity file is
     `SUBSTRATE_UNAVAILABLE`. It is **never regenerated or overwritten
     automatically.**

### 3. Layout

```
<state-root>/state-root.json
<state-root>/repo-locks/<repo-key>.lock
<state-root>/repos/<repo-key>/repo.json
<state-root>/repos/<repo-key>/runs/<lifecycle-id>/lifecycle.json
<state-root>/repos/<repo-key>/runs/<lifecycle-id>/lifecycle.lock
<state-root>/repos/<repo-key>/runs/<lifecycle-id>/abandonment.json   (only if abandoned)
<state-root>/repos/<repo-key>/runs/<lifecycle-id>/events.jsonl       (future EventSink)
<state-root>/repos/<repo-key>/maintenance/<maintenance-id>.jsonl
<state-root>/worktrees/<repo-key>/<lifecycle-id>/
```

Every deletion target is derived from the trusted state root, the
trusted repository's `repo_key`, and a validated `lifecycle_id`. The
source-repository path recorded anywhere is diagnostic and never
determines a deletion target.

### 4. Repository identity record (`repo.json`)

Created and validated only while holding the repository lock.

- Contents: `schema_version`, `repo_key`, the canonical Git common
  directory path, the `st_dev`/`st_ino` of the **opened** Git common
  directory, and the repository's Git `object_format`.
- Creation: only when `repos/<repo-key>/` and `worktrees/<repo-key>/`
  contain no state; created with `O_EXCL`, `fsync`ed.
- **Object format (no SHA-1 assumption):** the object format is
  determined from the trusted repository through Git itself (for
  example `git rev-parse --show-object-format`) and must be `sha1`
  (object IDs match `^[0-9a-f]{40}$`) or `sha256` (`^[0-9a-f]{64}$`).
  An undeterminable or unsupported format is `SUBSTRATE_UNAVAILABLE`.
  The zero OID used for compare-and-swap creation is the all-zero
  object ID of that format's length. Every object ID CodeAgent records
  or compares — checkpoint-ref SHAs in projections, abandonment
  markers, and maintenance events — is validated against this format;
  a malformed or wrong-length value is `REFUSED`.
- Validation on every use: the key, canonical path, `st_dev`,
  `st_ino`, and object format must all match the freshly opened
  repository. Any
  mismatch — including a canonical-path match whose `st_dev` or
  `st_ino` changed because the repository was replaced, re-cloned,
  restored from backup, or its device number changed — is
  `SUBSTRATE_UNAVAILABLE`, and the namespace is refused (fail closed).
- A missing `repo.json` alongside existing run or worktree state is
  `SUBSTRATE_UNAVAILABLE`. A corrupt `repo.json` is never regenerated.
- **Accepted v1 limitation — refused repository namespaces:**
  - v1 performs no automatic rebinding, adoption, identity
    regeneration, or namespace migration.
  - A refused namespace is neither reconciled nor abandoned: no
    projection in it is trusted, and nothing is written into it.
  - Diagnostics identify the refused namespace by `repo_key` and by
    which identity field did not match (canonical path, `st_dev`, or
    `st_ino`). Recorded values are reported as recorded and
    unverified, never as trusted identity.
  - Resolution requires operator-guided cleanup of the namespace's
    resources and archival of `repos/<repo-key>/` and
    `worktrees/<repo-key>/` outside CodeAgent, with no CodeAgent process
    running for that repository; `repo-locks/<repo-key>.lock` stays in
    place (§6).
  - A safe repository-generation migration or rebind command is
    deferred beyond v1.

### 5. Lifecycle projection (`lifecycle.json`)

The typed append-only run event stream remains the canonical run and
audit history. `lifecycle.json` is a **bounded operational recovery
projection**: it records which owned resources may exist and is never
used to explain what a run did.

- Schema v1: `schema_version`; identity fields `lifecycle_id`,
  `state_root_id`, `repo_key` (each must equal its directory name or
  `state-root.json`); diagnostic `run_id` and source path; `state`;
  `containers.baseline` and `containers.verification`, each
  `{intent, id}`; `worktree {intent, expected_head}`;
  `checkpoint_ref {intent, accepted_sha, expected_old_sha,
  proposed_new_sha}` (a write-ahead transition record — see below);
  `failure {phase, detail}` or null (fixed, sanitized strings);
  `reconciliation {attempts_total, recent_failures}`.
- **`checkpoint_ref` transition record** (ADR 0003 Amendment 1). In the
  record, `null` means "no ref"; the zero OID appears only in Git argv.
  Every non-null SHA must match the repository's object format (§4).
  Valid combinations:

  | `intent` | `accepted_sha` | `expected_old_sha` | `proposed_new_sha` |
  |---|---|---|---|
  | `absent` | null | null | null |
  | `creating` | null | null (no ref expected) | initial SHA |
  | `present` | current accepted SHA | null | null |
  | `advancing` | current accepted SHA A | A | new SHA B, with B ≠ A |
  | `removing` | final accepted SHA F | F | null (absence expected) |

  Any other combination is an inconsistent projection (`REFUSED`).
  Write-ahead:
  - **Create:** write `creating`; then the compare-and-swap create
    against the zero OID; confirm the ref is non-symbolic and equals
    the initial SHA; collapse to `present` with that SHA.
  - **Advance:** write `advancing`; then the compare-and-swap advance;
    confirm the ref equals B; collapse to `present` with B.
  - **Delete:** write `removing`; then the compare-and-swap delete;
    confirm the ref is absent; collapse to `absent`.
  - **Live-owner handling after a failed or unconfirmed ref operation**
    is operation-specific. It is always based on a fresh exact
    observation of the ref:
    - **Create failed:** if the ref is confirmed absent, collapse to
      `absent`.
    - **Advance failed:**
      - Live ref confirmed still equal to `accepted_sha`: collapse to
        `present` at `accepted_sha`. The new commit is unaccepted, and
        ADR 0003 discard-and-recreate handles the contaminated worktree.
      - Live ref equal to `proposed_new_sha`: the advance succeeded
        despite the reported failure. Follow the normal
        acceptance/confirmation path to `present` at `proposed_new_sha`;
        never treat it as a rollback.
    - **Terminal delete failed with the ref still present:** remain
      `removing`, with the lifecycle in `CLEANING`, for later
      reconciliation. Never collapse back to `present` to hide an
      unconfirmed cleanup. The run cannot end cleanly (§13).
    - **Delete where absence is confirmed:** collapse to `absent`.
    - **Any other or ambiguous observation** (symbolic ref, unexpected
      value, or failed observation): leave the transitional record
      unchanged and terminate fail-closed. Dead-run reconciliation (§8)
      re-inspects it: a valid recorded candidate may be reconciled, but a
      symbolic or unexpected value is `REFUSED` and an inspection failure
      remains `SUBSTRATE_UNAVAILABLE`.
  - Within Milestone 2, before this projection exists, the owning
    process holds the same transition record in memory only.
- `recent_failures` is capped at 10 entries (oldest dropped);
  `attempts_total` is never reset. Complete history lives only in
  maintenance events (§12).
- States: the owner writes `PREPARING → ACTIVE → CLEANING → COMPLETE`;
  the reconciler writes `RECONCILING → RECONCILED | RECONCILIATION_FAILED`.
  `COMPLETE` and `RECONCILED` are the only **clean final** states.
- Writes: only the holder of the lifecycle lock writes. Sequence:
  same-directory `mkstemp`, write, `fsync` file, `os.replace`, `fsync`
  directory. Temp files are never trusted; leftovers are reported.
- Corruption: malformed JSON, schema-invalid content, an unknown
  version, an identity mismatch, or an invalid intent/ID combination is
  `REFUSED`. A corrupt projection is never overwritten — it is evidence.
- Guarantee level: process-crash atomicity only; power-loss durability
  is not claimed.

### 6. Locks

- **Lifecycle lock** `runs/<lifecycle-id>/lifecycle.lock`: acquired by
  the owner before any resource exists, held for the whole process.
  A different process acquiring it means the owner is dead. PID is
  recorded as a diagnostic only.
- **Repository lock** `repo-locks/<repo-key>.lock`: one mutating run,
  explicit reconcile, or abandonment per repository at a time
  (threat model T-E1). Acquired non-blocking.
- Both use `fcntl.flock(LOCK_EX | LOCK_NB)` and obey:
  - open with `O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC`; never
    `O_TRUNC`; never atomic-replace a lock file;
  - after `flock`, verify `fstat(fd)` and `lstat(path)` identify the
    same regular file (`st_dev`, `st_ino`); otherwise release and
    return `SUBSTRATE_UNAVAILABLE`, so different processes can never
    lock different inodes at the same pathname;
  - **lock files are never unlinked, renamed, truncated, or replaced
    while their namespace can be reused**; v1 deletes no lock files and
    no run directories (retention is deferred);
  - hold a strong reference to the descriptor for the lock's lifetime;
    descriptors are non-inheritable and `close_fds` is never disabled.
- Order: the repository lock is acquired first and released last; the
  lifecycle lock is released only after the final projection write.
  The reconciler holds a dead owner's lifecycle lock through its final
  projection write.

### 7. Container attribution

- Roles: exactly `baseline` and `verification`.
- Names: `codeagent-<role>-<lifecycle_id>`
  (`^codeagent-(baseline|verification)-[0-9a-f]{32}$`). A role's name is
  reused for a later attempt only after the previous container is
  confirmed absent; at most one container per role exists at a time.
- Exactly four required labels, set at create time:
  `codeagent.lifecycle.schema=1`,
  `codeagent.lifecycle.state-root-id=<state_root_id>`,
  `codeagent.lifecycle.id=<lifecycle_id>`,
  `codeagent.lifecycle.role=<role>`. Optional informational
  `codeagent.lifecycle.attempt` is never part of ownership. All other
  labels are ignored. No paths or secrets appear in labels.
- Valid persisted combinations per role: `(id=null, intent=absent)`,
  `(id=null, intent=creating)`, `(id=set, intent=present)`,
  `(id=set, intent=removing)`. Write-ahead: clear the ID and set
  `creating` before `docker create`; persist the ID and set `present`
  before `docker start`; set `removing` before removal; clear the ID
  and set `absent` after confirmed absence.
- Attribution requires a successful unfiltered
  `docker ps -a --no-trunc` listing of IDs and names, plus a successful
  inspect of any candidate:

| Persisted | Live observation | Result |
|---|---|---|
| ID set, `present`/`removing` | No container has that name or that ID | Confirmed absent |
| ID set, `present`/`removing` | Name maps to exactly that ID, all four labels match | Owned: remove |
| ID set | Name maps to a different ID, the ID appears under another name, or labels missing/wrong | Ownership conflict: `REFUSED` |
| ID null, `creating` | Name absent | Confirmed absent |
| ID null, `creating` | Name present, all four labels match | Owned: remove (write-ahead: `creating` -> `removing(observed_id)`, published to the projection before removal; the maintenance trace itself is retrospective and records the id afterward — see Amendment 2 section 4 and Amendment 5) |
| ID null, `creating` | Name present, labels missing or wrong | Ownership conflict: `REFUSED` — never confirmed absence |
| ID null, `absent` | Name absent | Confirmed absent |
| ID null, `absent` | Name present, any labels | Ambiguity: `REFUSED` |
| Any other combination | Anything | Inconsistent projection: `REFUSED` |
| — | Listing or inspect fails or times out | `SUBSTRATE_UNAVAILABLE` |

Ambiguity always refuses mutation. Confirming absence after removal
requires a fresh successful listing in which neither the name nor the
recorded or observed ID appears.

### 8. Worktree and checkpoint-ref attribution

Worktree, at the recomputed path `<root>/worktrees/<repo-key>/<lifecycle-id>`:

- Owned only if the path is contained and not a symlink, it has an
  exact `git worktree list --porcelain` registration in the trusted
  repository, the directory exists, and the persisted intent is
  `creating`, `present`, or `disposing`.
- Neither registered nor present: confirmed absent.
- Registered but the directory is missing, or a directory present but
  unregistered: `REFUSED`. **Registered-but-missing worktrees are never
  resolved with `git worktree prune`.**
- Intent `absent` while either is present: `REFUSED`.
- Git listing failure or timeout: `SUBSTRATE_UNAVAILABLE`.
- Removal only via `git -C <trusted repo> worktree remove --force
  <path>`, confirmed by exact registration absence and directory
  absence. No recursive filesystem deletion fallback.

**Implementation status note (Milestone 3 Slice 3C-2, 2026-09-29):** the
recomputed path `<root>/worktrees/<repo-key>/<lifecycle-id>` this
section already specifies is now real, for the first time, as a
placement primitive: `StateRoot.reserve_worktree_leaf()` and
`GitWorktree`'s optional `reservation` parameter
(`src/codeagent/state_root.py`, `src/codeagent/workspace.py`) place a
real worktree at exactly this path. This is a placement capability
only — no production caller exists yet, worktree *removal* during
reconciliation remains unimplemented (the "Owned only if..."/"Removal
only via..." bullets above remain a specified design, not yet
production code), and no new amendment is introduced: this note
records that the layout this section already accepted is implemented,
not a new decision. Confirmed on GitHub-hosted Linux CI (commit
`7ec1fd2ce892c123491492c95ef6a990f841cbd2`, run
[36642099820](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36642099820),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see
`CLAUDE.md`'s Slice 3C-2 entry for the full evidence detail); this is
implementation/automated-test evidence only, not a security review, and
is `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 claim. It does not claim any production composition/entry-point
wiring or T-E1 mitigation — Slice 3C-2 remains an unwired prerequisite.

Checkpoint ref `refs/codeagent/runs/<lifecycle_id>/checkpoint`
(reachability semantics: ADR 0003 Amendment 1, Accepted). Dead-run
reconciliation observes the exact recomputed ref through structured
argv against the trusted repository, then acts on the persisted
transition record (§5):

| Persisted `intent` | Deletion candidates | Live ref missing | Live value is a candidate | Any other value, or symbolic |
|---|---|---|---|---|
| `absent` | none | Confirmed absent | — | `REFUSED` (any existing ref) |
| `creating` | `proposed_new_sha` | Confirmed absent | Delete | `REFUSED` |
| `present` | `accepted_sha` | Confirmed absent | Delete | `REFUSED` |
| `advancing` | `accepted_sha` (= `expected_old_sha`) or `proposed_new_sha` | Confirmed absent | Delete | `REFUSED` |
| `removing` | `accepted_sha` (= `expected_old_sha`) | Confirmed absent | Delete | `REFUSED` |

- **Delete** means `git update-ref --no-deref -d <ref> <observed
  candidate SHA>`, then confirming the ref is absent, then recording
  `absent`. The expected value is always the observed live value, and
  only when that value is a valid candidate for the persisted intent.
- An inconsistent or malformed transition record, or a SHA that does
  not match the repository's object format, is `REFUSED`.
- A failed observation is `SUBSTRATE_UNAVAILABLE`.
- Intermediate discard-and-recreate never deletes or moves the ref
  (§9). Terminal teardown and dead-run reconciliation delete it last.
- `refs/codeagent/` is never swept; discovery is report-only.
- While a ref exists it is visible to `git for-each-ref` and would be
  pushed by `git push --mirror`; this is documented, accepted
  visibility.

### 9. Cleanup ordering

- **Intermediate recovery within a live run (ADR 0003
  discard-and-recreate):** confirm both role containers absent, dispose
  of the contaminated worktree and confirm disposal, then recreate the
  worktree from the pinned checkpoint. **The checkpoint ref is kept.**
- **Terminal teardown (owner):** export the diff, then containers
  (both roles) → worktree → checkpoint-ref deletion, each confirmed
  before the next begins; then fire the terminal trigger.
- **Dead-run reconciliation:** the same container → worktree →
  checkpoint-ref order.
- An unconfirmed step stops all later steps. Every Docker and Git
  lifecycle call has a timeout; a timeout is unconfirmed.

### 10. Reconciliation: trigger, scope, outcomes, blocking

- **Automatic pre-run reconciliation (primary):** for every mutating
  run, after acquiring the repository lock and before generating a new
  `lifecycle_id` or creating any run directory. Read-only commands
  never reconcile.
- **Scope:** only the current repository's `repos/<repo-key>/runs/`.
  Other repositories' projections are read without lock probing or
  mutation and reported as non-final with liveness unknown.
- **Per-entry outcomes:**
  - `SKIPPED_TERMINAL` — `COMPLETE` or `RECONCILED`; zero lock,
    inspection, or mutation calls.
  - `SKIPPED_ABANDONED` — a valid `ABANDONED` marker (§11); zero calls;
    always reported.
  - `SKIPPED_ABANDONED_UNRESOLVED` — a valid `ABANDONED_UNRESOLVED`
    marker (§11); zero calls; always reported with a prominent warning.
  - `SKIPPED_ACTIVE` — lifecycle lock busy.
  - `RECONCILED` — every attributable resource removed and confirmed.
  - `REFUSED` — conflict, ambiguity, corruption, inconsistent
    projection, or a corrupt marker; zero mutation.
  - `FAILED` — mutation attempted but not confirmed; retried on later
    passes.
  - `SUBSTRATE_UNAVAILABLE` — state root, identity files, locks, Docker
    listing/inspect, or Git listing unusable; zero mutation.
- Every entry's resources are inspected successfully before any of that
  entry's resources are mutated.
- **Blocking (fail-closed):** after the automatic pass, a new mutating
  run is refused if the current repository has any `REFUSED`, `FAILED`,
  `SKIPPED_ACTIVE` (an invariant violation while the repository lock is
  held), other unresolved non-final entry, or any
  `SUBSTRATE_UNAVAILABLE` result. On refusal the repository lock is
  released, the exit status is nonzero, and no run directory is
  created. Problems in other repositories are reported and never block
  the current repository.
- `ABANDONED` and `ABANDONED_UNRESOLVED` entries do not block. A
  mutating run may proceed past an `ABANDONED_UNRESOLVED` entry **only
  because the operator explicitly acknowledged the unresolved risk**
  (§11), and it must surface a prominent warning — on the CLI and in
  its report — naming each such `lifecycle_id` and its recorded
  unresolved-resource summary.

### 11. Explicit reconcile and abandonment

`codeagent reconcile --repo <path>`:

- Same reconciler, trigger `explicit`; a diagnostic and retry path.
  Automatic pre-run reconciliation remains primary.
- Takes the repository lock non-blocking; if busy, reports the
  repository as active and exits nonzero without mutation.
- Writes a maintenance trace, prints categorized results, and reports
  exactly one overall result, with precedence
  `BLOCKED` > `UNRESOLVED_ACKNOWLEDGED` > `CLEAN`:
  - `CLEAN` (exit `0`): every current-repository entry is `COMPLETE`,
    `RECONCILED`, or `ABANDONED`.
  - `UNRESOLVED_ACKNOWLEDGED` (a distinct nonzero exit status, never
    `0`): no blocking problem remains, but at least one entry is
    `ABANDONED_UNRESOLVED`. This result is never reported as clean.
  - `BLOCKED` (a nonzero exit status distinct from
    `UNRESOLVED_ACKNOWLEDGED`): any `REFUSED`, `FAILED`, unresolved
    non-final, or `SUBSTRATE_UNAVAILABLE` result, a refused repository
    namespace (§4), or a busy repository lock.
  - The numeric exit values are fixed at implementation and pinned by
    tests.
- `--dry-run` inspects and reports without mutation, projection writes,
  markers, or maintenance events.
- Never creates a `lifecycle_id` or run directory.

`codeagent reconcile --repo <path> --abandon <lifecycle-id>`:

- **Never deletes or mutates any container, worktree, or ref.**
- Requires the repository lock and acquisition of that entry's
  lifecycle lock; refuses if either is busy or fails inode verification
  (an active run is never abandoned). Refuses entries already
  clean-final or abandoned.
- Performs a fresh exact inspection using recomputed identities: both
  role names (and recorded IDs when the projection is readable; with an
  unreadable projection, any container occupying a role name counts as
  remaining), the recomputed worktree path and registration, and the
  recomputed checkpoint ref.
- **Normal abandonment** succeeds only if inspection succeeds and no
  attributable resource or registration remains. It records disposition
  `ABANDONED`.
- **Forced acknowledgement** (a separate explicit flag,
  `--acknowledge-unresolved`, plus a required `--reason <text>`) may
  record `ABANDONED_UNRESOLVED` even when resources remain or
  inspection fails. It records what remains or could not be inspected,
  the sanitized, length-bounded operator reason, and reports
  `UNRESOLVED_ACKNOWLEDGED`, never `CLEAN`. It never claims
  successful cleanup.
- Abandonment is unavailable in a refused repository namespace (§4).
- Both write `abandonment.json` next to the projection with
  `O_EXCL` (+ `fsync`), containing `schema_version`, `lifecycle_id`,
  `repo_key`, `state_root_id`, disposition, `maintenance_id`,
  timestamp, and (forced only) the reason and remaining-resource
  summary. The projection itself is never rewritten by abandonment.
  Both emit a maintenance event.
- Both dispositions are **administratively final**: they prevent
  permanent startup deadlock, remain visible in every report and
  reconcile listing, are never rewritten into `COMPLETE` or
  `RECONCILED`, and are never touched again automatically.
  - `ABANDONED` records that fresh inspection confirmed no attributable
    resource or registration remained at abandonment time. It is
    reported distinctly from `COMPLETE`/`RECONCILED` (CodeAgent did not
    confirm its own cleanup against a valid projection), but it may
    contribute to a `CLEAN` reconcile result.
  - `ABANDONED_UNRESOLVED` is **never clean**. It is never equivalent
    to `COMPLETE`, `RECONCILED`, `ABANDONED`, or confirmed absence; it
    never contributes to a fully clean report, a `CLEAN` result, or any
    cleanup claim; and resources behind it may remain leaked until the
    operator removes them outside CodeAgent.

### 12. Maintenance trace

- Post-crash reconciliation and abandonment emit a separate typed,
  versioned maintenance trace at
  `repos/<repo-key>/maintenance/<maintenance-id>.jsonl`
  (`maintenance_id`: 32 lowercase hex), written while holding the
  repository lock.
- Event types: `ReconciliationStarted`, `ReconciliationEntryRecorded`,
  `AbandonmentRecorded`, `ReconciliationFinished`.
- Persisted fields: `maintenance_id`, `state_root_id`, `repo_key`,
  trigger (`pre_run` or `explicit`), `lifecycle_id`, diagnostic
  `run_id`, outcome category, attempt number, resource roles, container
  names and IDs, worktree presence/registration by
  `(repo_key, lifecycle_id)`, ref name and SHA, and sanitized
  categorical details.
- Not persisted: absolute host paths, raw Docker/Git stderr, full
  inspect objects, or container environment values.
- Nothing is ever appended retroactively to a dead process's
  `events.jsonl`. An interrupted run is detected from a missing
  `RunFinished` plus its lifecycle projection or abandonment marker.

### 13. Terminal outcomes and cleanup

A run never records a success-like terminal reason unless terminal
teardown (§9) is confirmed. If cleanup cannot be confirmed, the
controller fires `UNRECOVERABLE_ERROR` with a new cleanup-unconfirmed
error code (exact name fixed at implementation and pinned by
`errors.py` tests), leaves the projection in `CLEANING`, and exits
nonzero. The existing `RunFinished` rules are unchanged. The
cancellation-specific application of this rule is in ADR 0005.

### 14. Invariants

- **I1** Targets derive only from the trusted state root, the trusted
  repository's `repo_key`, a validated `lifecycle_id`, and live
  observation; projection paths and IDs are compared, never used as
  targets.
- **I2** No generic operations: never `docker container prune`, never
  removal by filter, prefix, or label query, never `git worktree
  prune`, never recursive filesystem deletion, never a sweep of
  `refs/codeagent/`.
- **I3** A container is removed only when both locks are held, §7 shows
  ownership, and inspection of all the entry's resources succeeded.
- **I4** A worktree is removed only when both locks are held, §8 shows
  ownership, and every container for that lifecycle is confirmed
  absent.
- **I5** A checkpoint ref is removed only in terminal teardown or
  dead-run reconciliation (never during intermediate
  discard-and-recreate), with both locks held, the worktree confirmed
  absent, and a compare-and-swap whose expected value is a valid
  candidate for the persisted transition intent (§5, §8), validated
  against the repository's object format.
- **I6** Any inspection error or timeout causes zero mutation for that
  entry and blocks the current repository.
- **I7** Conflicts, ambiguities, inconsistent projections, and
  corruption are `REFUSED` and never reinterpreted as absence.
- **I8** Other state roots, other repository keys, unlabeled
  containers, unknown schemas, and paths outside
  `worktrees/<repo-key>/` are never candidates.
- **I9** Automatic reconciliation never locks or mutates another
  repository's entries.
- **I10** Only the lifecycle-lock holder writes a projection; no process
  appends to another process's event stream.
- **I11** Clean-final and abandoned entries are never locked, inspected
  (automatically), or mutated again.
- **I12** Lock files and identity files are never unlinked, renamed,
  truncated, or replaced while their namespace can be reused; lock
  acquisitions are inode-verified; corrupt identity state is never
  regenerated.
- **I13** The repository lock is acquired first and released last; the
  lifecycle lock is taken before any resource exists and released only
  after the final projection write.
- **I14** A mutating run starts only when every current-repository
  entry is `COMPLETE`, `RECONCILED`, `ABANDONED`, or
  `ABANDONED_UNRESOLVED` after the automatic pass, and the repository
  namespace is not refused; an `ABANDONED_UNRESOLVED` entry always
  produces a prominent warning.
- **I15** No success-like terminal reason without confirmed cleanup.
- **I16** Abandonment never deletes resources and never claims cleanup;
  `ABANDONED_UNRESOLVED` never contributes to a clean result, a fully
  clean report, or any cleanup claim.
- **I17** A refused repository namespace is never automatically
  rebound, adopted, regenerated, migrated, reconciled, or abandoned.

### 15. Guarantee boundary

CodeAgent v1 provides **recovery of owned resources plus interruption
detection** — never crash consistency.

- **G1** CodeAgent removes only resources it can exactly attribute,
  after successful inspection, to a dead lifecycle in the current
  repository's namespace.
- **G2** On completion, handled failure, or cooperative cancellation,
  cleanup is confirmed before success or cancellation is recorded;
  otherwise the run ends `UNRECOVERABLE_ERROR` and remains
  reconcilable.
- **G3** After SIGKILL, a crash, or unconfirmed cleanup, leftovers are
  **eligible** for reconciliation. They are removed only after exact
  attribution and successful inspection. Ambiguous, corrupt, and
  substrate-unavailable cases are not removed: they remain blocked and
  reported until resolved by a later reconciliation or an explicit
  abandonment.
- **G4** An interrupted run is detectable and is never presented as
  completed. `ABANDONED` runs are presented as abandoned, distinct from
  completed or reconciled; `ABANDONED_UNRESOLVED` runs are presented as
  unresolved and never as clean.

Not guaranteed: crash-consistent writes (ADR 0003); resumption of
interrupted runs; a bound on orphan lifetime for repositories that are
never run again; cleanup of resources behind `ABANDONED_UNRESOLVED`
entries; recovery of a repository namespace refused after the
repository was replaced at the same path (§4); cleanup while the owner
is alive but hung; reboot recovery (not precluded, not promised —
T-F4); network filesystems; power-loss durability; PID-reuse behavior
(untested); protection from a malicious local user or compromised
Docker daemon (A4).

## Consequences

- A stuck, corrupt, or conflicting entry blocks new mutating runs in
  its repository until reconciliation succeeds or the operator abandons
  it. This is the intended fail-closed trade-off; abandonment is the
  escape hatch, and it never deletes anything.
- Resources behind `ABANDONED_UNRESOLVED` entries stay leaked until the
  operator removes them outside CodeAgent. Later runs in that
  repository proceed only on the strength of the operator's explicit
  acknowledgement, always with a prominent warning, and neither they
  nor `codeagent reconcile` ever report the repository as fully clean
  while such an entry exists.
- **Accepted v1 limitation:** when the repository at a recorded
  canonical path is replaced, re-cloned, restored from backup, or its
  device number changes, the changed `st_dev`/`st_ino` makes the whole
  repository namespace fail closed. Abandonment cannot clear it, and v1
  provides no rebinding, adoption, identity regeneration, or migration.
  Resolution requires operator-guided cleanup and archival outside
  CodeAgent (§4); a safe repository-generation migration or rebind
  command is deferred beyond v1.
- A crash between creating `state-root.json` and finishing its write
  leaves an invalid identity file that CodeAgent will not repair.
- Orphans of repositories that are never run again persist
  indefinitely; running orphaned containers keep consuming resources
  until reconciled.
- Run-history growth is unbounded in v1 (retention deferred), and
  projection reads grow linearly with history.
- Container names change from `codeagent-verify-*` to
  `codeagent-(baseline|verification)-*`; `.github/workflows/ci.yml`'s
  leftover-container check must change with the implementation.
- New error codes and a maintenance event contract extend Milestone 0's
  pinned vocabularies.
- The worktree root must be shareable with Docker Desktop on macOS.

Required tests (none exist yet): state-root initialization race,
otherwise-non-empty root refusal, and no regeneration; `repo.json`
replacement and case-alias detection; projection crash between temp
write and replace, corruption refusal, bounded history; lock contention
across real subprocesses, release after SIGKILL with a `docker`/`git`
child running, inode-mismatch refusal, never-unlink; every row of the
container, worktree, and ref tables; every valid and invalid
`checkpoint_ref` transition combination, including a crash at each
write-ahead step of create, advance, and delete; object-format
detection and SHA validation in SHA-1 repositories and — where the
installed Git supports `--object-format=sha256` — SHA-256
repositories (an unsupported environment is reported as a skipped
test with its reason, never a silent pass); a canary suite (unlabeled and
same-prefix containers, foreign state root and lifecycle IDs, renamed
owned containers, images carrying `codeagent.*` labels, operator
worktrees present/missing/locked, locked CodeAgent worktrees, symlinked
paths, live runs in other repositories); the blocking rule;
`--dry-run`, normal abandonment, and forced acknowledgement (never
deleting); the `CLEAN` / `UNRESOLVED_ACKNOWLEDGED` / `BLOCKED` results
and their distinct exit statuses; the prominent warning when a run
proceeds past `ABANDONED_UNRESOLVED`; a refused namespace after
same-path repository replacement (no reconcile, abandonment, or writes
into it); maintenance events free of absolute host paths; and
production interruption acceptance on macOS and Linux with real
bind-mounted verification.

## Rejected alternatives

- **Reusing `run_id` for resources** — its public format is not safe as
  a path or name component; tightening it breaks a public contract.
- **Name prefix or per-attempt random names** — a prefix does not prove
  ownership (S5 canaries), and random names cannot be recomputed after
  a crash.
- **Repo-local state in `.git/`** — writes CodeAgent state into shared
  Git metadata (T-M1) and strands orphans of deleted repositories.
- **SQLite** — contradicts the handoff's "Database: None in v1".
- **Rebuilding lifecycle state from `events.jsonl`** — ties cleanup
  correctness to event-log truncation handling (T-F3) and to an
  EventSink that does not yet exist.
- **Labels as the only registry** — worktrees are not Docker objects,
  and a manifest must exist before `docker create`.
- **System temp directories** — unstable across environments, and OS
  cleaners create registered-but-missing worktrees.
- **PID liveness, `lockf`, or `O_EXCL` lock files** — PID reuse;
  release on any descriptor close; never released after a crash.
- **Deriving liveness from the repository lock** — its key would come
  from projection content.
- **Warn-and-proceed on current-repository problems** — runs would
  proceed without the substrate needed to confirm their own cleanup.
- **`git worktree prune`, including a dry-run-verified variant** —
  repository-wide effect on operator worktrees and a check/prune race.
- **Worktree-first cleanup, `docker run --rm`, name-only confirmation**
  — risks deleting a live bind-mount source; `--rm` removes the
  container before OOM state can be inspected and does not help when
  the controller dies; renamed containers defeat name-only checks.
- **Automatic regeneration of identity files** — would orphan every
  labeled container and silently adopt unattributed state.
- **Automatic rebinding or migration of a replaced repository's
  namespace in v1** — would adopt projections and resources whose
  repository identity no longer matches; deferred until a safe
  repository-generation migration can be designed.

## Evidence

`spikes/s5/S5_RESULT.md` and its retained evidence:
`spikes/s5/evidence/macos-docker-desktop-arm64/run-ffd711753021/`
(authoritative macOS run; seven earlier macOS runs retained as history)
and `spikes/s5/evidence/linux-x86_64/run-34783737248-attempt-1/`
(authoritative Linux run, including `workflow_diagnostics/`). Mocked
focused tests in `spikes/s5/test_spike_s5.py`. Production findings from
reading `src/codeagent/workspace.py`, `src/codeagent/executor.py`, and
`src/codeagent/patch.py` at commit `ad362cf`.

## Implementation order

Nothing here is implemented. Milestone 2 comes first, including the
checkpoint-ref mechanics of ADR 0003 Amendment 1 (Accepted); the
mechanisms in this ADR are implemented afterwards as Milestone 3
lifecycle work. See `CLAUDE.md` and `ENGINEERING_LOG.md`.

---

## Amendment 1 (Accepted 2026-09-18): Milestone 3 Slice 3A-1 — state root, trusted repository identity, and the repository/generic lock substrate

Implementation status: **design accepted; not implemented.** This
amendment records the persistent-format and filesystem-safety decisions
reached through a multi-round planning-only architecture review for
Milestone 3 Slice 3A-1 — the substrate this ADR's §2 (state root), §1's
`repo_key` identity, and the repository-lock half of §6 depend on.
Nothing in this amendment exists in production code yet; no
`src/codeagent/_lifecycle_fs.py`, `state_locks.py`, `state_root.py`, or
`repo_identity.py` module exists. This amendment does not revise any
decision made above — it fills in exact mechanisms this ADR's original
text left unspecified, and narrows one detail (the ambient-symlink
policy has no analogue here at all; see below) the way ADR 0003
Amendment 2 and ADR 0006 Amendment 4 previously narrowed their own
ADRs' text without reversing it.

### 1. `repo_key` derivation

`canonical_common_dir_bytes = os.fsencode(canonical_common_dir)` — a
**same-host identity only**, deliberately: `os.fsencode` uses the
interpreter's filesystem encoding with `surrogateescape`, so a
canonical path containing non-UTF-8 bytes (legal on Linux, where a
pathname is an opaque byte string) produces a stable, deterministic
`repo_key` on a given host without any cross-host portability claim,
none being needed. Pinned by an exact test vector:
`sha256(b"codeagent.repo-key.v1\0" + os.fsencode("/tmp/codeagent-fixed-repo-key-vector")).hexdigest()[:32]
== "126b3309737aaf2addc754b014f19c79"` — a hardcoded literal, not a
value recomputed from the function under test, so an accidental
domain-prefix or truncation-length regression cannot be silently
"fixed" by updating the test to match new behavior.

### 2. Canonical directory identity (macOS case bug)

**`Path.resolve()` does not canonicalize case on a case-insensitive-
but-preserving macOS (APFS/HFS+) volume** — reproduced directly this
session: `/Users/chandrasekhar/code/codeagent` and
`/users/chandrasekhar/code/codeagent` both resolve while preserving
their distinct spelling, meaning two spellings of the same directory
would otherwise compute two different `repo_key`s or be missed by a
containment check. One shared primitive closes this:

- Open the directory safely (`O_NOFOLLOW | O_DIRECTORY`), `fstat` it.
- On Darwin, canonicalize via `fcntl.fcntl(fd, fcntl.F_GETPATH,
  bytes(1024))` — **exactly this call shape**: passing `array.array`
  or `bytearray` instead of an immutable `bytes` buffer raises
  `TypeError` on the current macOS/CPython runtime. The returned value
  must contain a NUL terminator; the bytes before it must be nonempty
  and start with `/`. Decode with `os.fsdecode` only — never a
  UTF-8-then-fallback branch, since `os.fsdecode`/`os.fsencode` are the
  one paired filesystem conversion this design relies on everywhere
  else. Any failure of this step (missing `F_GETPATH`, a `TypeError`,
  a malformed buffer, or an `OSError`) is `SUBSTRATE_UNAVAILABLE`,
  **with no fallback to the non-canonical path** — silently falling
  back would defeat the entire point of the mechanism.
- On Linux, the already-resolved absolute path used to open the
  directory is the canonical value (Linux filesystems are normally
  case-sensitive, and `st_dev`/`st_ino` are already the authoritative,
  case-independent identity there).

Applied to exactly three starting points before any containment check
runs: the resolved state root, the trusted working-tree root, and the
trusted Git common directory. **Containment checks operate only on
these canonical values** — never on a bare `Path.resolve()` output.
`StateRoot.path` stores the case-canonical path, never the
pre-`F_GETPATH` spelling.

### 3. Canonical JSON: strict UTF-8, `ensure_ascii=True`

Persisted JSON (`state-root.json`, `repo.json`, and — when 3A-2 defines
it — `lifecycle.json`) is serialized with `json.dumps(payload,
sort_keys=True, separators=(",", ":"), ensure_ascii=True)` followed by
**strict** `.encode("utf-8")`, and deserialized with **strict**
`.decode("utf-8")` before `json.loads`. `ensure_ascii=True` is
required, not optional: escaping every non-ASCII code point — including
a lone surrogate in `os.fsdecode`'s surrogateescape range — as a
`\uXXXX` sequence of plain ASCII characters means the serialized string
is pure ASCII, so strict UTF-8 encoding always succeeds and the file on
disk is always genuinely valid UTF-8 JSON text. (A prior design using
`ensure_ascii=False` plus a `surrogateescape`-mode encode was rejected
during review: it could write raw invalid-UTF-8 bytes into a file
claiming to be JSON.)

A `\udcXX` escape (U+DC80–U+DCFF, exactly `os.fsdecode`'s
surrogateescape range for raw bytes 0x80–0xFF) is a **legitimate
filesystem surrogate** and round-trips through `os.fsencode()` to
recover the original path bytes exactly. Any other lone surrogate
(U+D800–U+DC7F or U+DD00–U+DFFF) is refused — CodeAgent's own encoder
never produces one, so its presence is either corruption or hostile
input.

### 4. Fixed persisted bounds

| Item | Bound | Measured as |
|---|---|---|
| `state-root.json` | 4096 bytes | encoded JSON file size |
| `repo.json` | **32768 bytes (32 KiB)** | encoded JSON file size — sized for a 4096-filesystem-byte path where every byte needs a worst-case 6-character `\udcXX` escape (24,576 bytes) plus fixed-field/JSON-punctuation overhead (≈150 bytes), leaving ≈8,042 bytes (≈24%) of headroom above that worst case |
| `lifecycle.json` (3A-2's own schema) | 64 KiB | encoded JSON file size — the budget accommodates one maximally-escaped diagnostic path plus the capped `recent_failures` history; 3A-2 must re-verify this arithmetic once its exact schema is fixed |
| `run_id` | 256 bytes | encoded JSON file size — a diagnostic field of `lifecycle.json` only, **never stored in `repo.json`** (whose §4 schema has no such field) |
| Stored paths (canonical common dir, any diagnostic source path) | 4096 bytes | filesystem bytes (`len(os.fsencode(path))`) — a deliberate, fixed **v1 product limit** applied uniformly on both platforms, not a claim that 4096 is either OS's actual `PATH_MAX` |
| Sanitized failure `detail` | 512 bytes | encoded JSON file size |

Duplicate-key, unknown-field, invalid-UTF-8, oversized, and
schema-invalid content are all refused (never partially trusted); the
precise state-machine treatment of a syntactically incomplete document
is §7 below.

### 5. State-root location

`resolve_state_root_path()` returns a typed `StateRootLocation` (`path`,
`origin` ∈ {`EXPLICIT`, `MACOS_DEFAULT`, `XDG_DEFAULT`,
`LINUX_HOME_DEFAULT`}, `conventional_parent_creation_allowed`) rather
than a bare path:

- `CODEAGENT_STATE_DIR` (`EXPLICIT`) must be absolute; a missing parent
  is refused, never auto-created — an operator-supplied explicit path
  with an absent ancestor is a typo or an intentional refusal signal,
  not a convenience gap to paper over.
- `XDG_STATE_HOME` is used only when set **and absolute**; a relative
  value falls back to `~/.local/state/codeagent`, per this ADR's
  original §2 text.
- For the three default origins only, **precisely bounded** ancestor
  creation is permitted: macOS may create at most the two named
  components `Library/Application Support` beneath `$HOME`; the Linux
  home default may create at most `.local/state` beneath `$HOME`; the
  XDG default may create at most `$XDG_STATE_HOME` itself. None of
  these ever recurses into an unconstrained `os.makedirs` over an
  arbitrary ancestor chain, and a missing `$HOME` itself is refused,
  never worked around.

### 6. Canonicalization before directional containment

Containment is a **directional** primitive (`is_within_or_equal`,
component-aware via resolved `Path.parts` comparison, never a string
prefix), applied to the **canonical** (§2) values of the state root,
the trusted working-tree root, and the trusted Git common directory —
never to raw `Path.resolve()` output, which is what the reproduced
macOS case bug would otherwise slip past. Exactly the four checks this
ADR's original §2 text already implies, made explicit: the state root
must not be within-or-equal-to the trusted working-tree root; the state
root must not be within-or-equal-to the trusted Git common directory;
the trusted working-tree root must not be within-or-equal-to
`<state-root>/worktrees`; the trusted Git common directory must not be
within-or-equal-to `<state-root>/worktrees`. A bare repository has no
working-tree root (`working_tree_root=None`); the working-tree-side
checks are skipped, never defaulted to some other path. Production
discovery itself refuses a bare repository outright
(`BARE_REPOSITORY_UNSUPPORTED`, matching `workspace.py`'s existing
requirement that the trusted source repository have a working tree) —
`working_tree_root=None` is exercised only by isolated unit tests of
the generic containment primitive, never by any production code path.

### 7. `dir_fd`-relative authority below the state root; no ambient-symlink exception

The operator-chosen state-root location may itself be reached through
an ordinary, pre-existing symlink (an ambient OS convention, or an
operator's own `~`/environment-variable resolution) — resolved **once**
via `Path.resolve()` plus the case-canonicalization of §2. **Everything
CodeAgent itself creates below that resolved root — `repo-locks/`,
`repos/<repo-key>/`, and everything nested under them — is reached only
through `dir_fd`-relative `os.mkdir`/`os.open` calls anchored to an
already-open, already-validated parent descriptor, never a fresh
full-pathname lookup of an intermediate component.** This is a
**stricter** policy than `evidence.py`'s Darwin-gated, target-verified
`/tmp`/`/var`/`/etc` exception (ADR 0003 Amendment 2's own correction):
nothing below the state root has any legitimate reason to be a
symlink, so no allowlist of any kind exists here, and none of
`evidence.py`'s allowlist logic is reused. `state-root.json`, `repo.json`,
and repository-lock operations all use the `dir_fd`-relative primitives
(`open_private_create_exclusive_at`, `acquire_lock_nonblocking_at`,
`fsync_directory_fd`, `os.lstat(basename, dir_fd=...)`); a `Path` value
constructed for a managed location below the root (e.g. a `LockHandle`'s
diagnostic path) is **diagnostic only** and is never re-resolved or
used as filesystem authority after the fact.

`StateRoot` itself owns a long-lived, open file descriptor for the
resolved root for as long as the object is alive — a structural
consequence of the "never reopen by pathname" rule, since every later
dir_fd-relative operation beneath it needs that descriptor to anchor
to.

### 8. Private-file creation, independent of umask

`state-root.json` and `repo.json` creation both go through the **same**
one primitive: `O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC` (dir_fd-
relative), followed by an explicit `fchmod(0o600)` and an `fstat`-
verified exact-mode/regular-file/current-uid check — never a bare
`os.open(..., 0o600)`, since umask can silently narrow the requested
mode below what the caller asked for.

### 9. Non-inheritable descriptors, checked at operation time

Every descriptor this substrate opens — the state-root directory, the
trusted working-tree root, the trusted Git common directory and Git
directory (during discovery), every intermediate managed directory,
the `repo-locks/` directory, the `repos/<repo-key>/` directory, lock
files, and private JSON files — uses `O_CLOEXEC` where the platform
supports it, with `FD_CLOEXEC` **reasserted and verified** via
`fcntl.fcntl(fd, F_SETFD, ...)` / `fcntl.fcntl(fd, F_GETFD)` rather than
trusted from the open flag alone. Missing `fcntl`, `flock`,
`O_NOFOLLOW`, `O_CLOEXEC`, or (Darwin) `F_GETPATH` support is
`SUBSTRATE_UNAVAILABLE`, raised **at the operation that actually needs
it**, never at module import time — so a platform lacking one of these
capabilities fails closed with a categorical, testable error rather
than an uncaught `ImportError`/`AttributeError`.

### 10. Complete descriptor cleanup, never swallowed, never short-circuited

Every primitive that owns a descriptor's lifetime — directory
canonicalization, managed-directory-chain traversal, `StateRoot`'s own
close, repository-context/identity discovery, private-JSON-file
creation, and lock acquisition/release — follows one uniform rule:
**every close in a batch is attempted, even after an earlier one in the
same batch fails** (an aggregation implemented as an explicit loop that
accumulates a failure flag across every element, never a short-
circuiting `any()` over a generator, which would stop attempting
further closes as soon as the first one failed); and **a cleanup
failure dominates** — it becomes the primary raised error, explicitly
chained from whatever sanitized exception was already active (`raise
cleanup_error from original_error`), never silently discarded and
never left to compound as an unraised, already-forgotten failure. This
applies uniformly, with no per-module exception:

- **Directory canonicalization** (§2): a close failure after a
  canonicalization failure is not silently discarded — it is chained
  from that failure the same as everywhere else.
- **Managed-directory-chain traversal**: on a traversal failure, every
  opened intermediate descriptor is closed (all of them, not stopping
  at the first close failure); a resulting cleanup failure is chained
  from the traversal failure. On a *successful* traversal, every
  intermediate descriptor except the final one returned to the caller
  is closed the same way; a cleanup failure here has no prior failure
  to chain from and is itself the primary error (the final descriptor
  is also closed at that point, rather than compounding the leak
  further).
- **Repository-context/identity discovery**: every descriptor opened
  during Git-directory canonicalization, common-directory
  canonicalization, and working-tree-root canonicalization is tracked
  on an explicit owned-descriptor stack as it is acquired — never
  assumed closeable only via straight-line code that only reaches a
  cleanup step on the successful path. A failure at any acquisition
  point closes every descriptor acquired so far (in the same
  all-attempted, no-short-circuit, cleanup-dominates-and-chains
  manner) before propagating.
- **Lock acquisition, partial success**: if the lock file itself has
  been successfully opened and `flock`ed, but cleanup of the short-lived
  parent-directory descriptor used to reach it then fails, the acquired
  lock is never left unreachable — release of the just-acquired lock is
  attempted *before* raising. If both the parent-fd cleanup and the
  lock release fail, both failures are reported categorically together
  in one sanitized cleanup error (never only one, silently dropping the
  other), still chained from any earlier sanitized failure that was
  already active. A dedicated test proves a second process can acquire
  the same lock afterward whenever release was independently confirmed,
  regardless of what happened to the unrelated parent-fd descriptor.
- **Lock release** (unchanged from the accepted design of the prior
  planning rounds): a release failure is the primary raised error,
  explicitly chained from any already-propagating body exception rather
  than merely recorded as a side-channel attribute — deliberately
  stricter than `GitWorktree.__exit__`'s "never mask an in-flight
  exception" convention, since a stuck lock can block every future run
  against the repository, not just this one's own cleanup.

### 11. `LockScope`

```
LockScope(kind: LockKind, repo_key: str, lifecycle_id: str | None = None)
```

`repo_key` (and, when present, `lifecycle_id`) are validated as
**exactly 32 lowercase hexadecimal characters** in `LockScope`'s own
construction — before any path component is derived from them, not
after. `LockKind.LIFECYCLE` requires a `lifecycle_id`; `LockKind.
REPOSITORY` forbids one. `load_or_create_repo_json` requires the
presented lock handle to be currently held and its `scope` to equal
exactly `LockScope(kind=REPOSITORY, repo_key=<this repository's key>)`
— a lifecycle-scoped or wrong-repository handle is rejected. `LockKind.
LIFECYCLE` is fully defined now specifically so 3A-2 does not require a
breaking change to this capability model later; **nothing in Slice
3A-1 ever constructs a `LockScope` of kind `LIFECYCLE`.**

### 12. Repository/lifecycle lock ordering boundary (3A-1 vs. 3A-2)

Slice 3A-1 implements **only** the generic, verified, nonblocking lock
primitive and the one named repository-lock wrapper. It does **not**
create `runs/<lifecycle-id>/` and does **not** expose a lifecycle-lock
wrapper. Slice 3A-2 is responsible for atomically ordering: acquire the
repository lock (reusing 3A-1's primitive unchanged) → create
`runs/<lifecycle-id>/` → acquire the lifecycle lock (reusing 3A-1's
generic primitive with `LockKind.LIFECYCLE`) → write the initial
`PREPARING` `lifecycle.json` projection — all strictly before any
Docker container, worktree, or checkpoint ref exists, per this ADR's
§6/§10. This ordering is a documented contract for 3A-2's own
composition, not something 3A-1 itself provides as a combined
function.

### 13. `repo.json` identity-mismatch classification

A module-local `RepoIdentityFailure.IDENTITY_MISMATCH` reason is
retained as the precise diagnostic label, carrying a **bounded,
immutable set of mismatched field names only** — drawn exclusively from
`{repo_key, canonical_common_dir, st_dev, st_ino, object_format}`,
**never the recorded or observed values themselves** (no path, no
inode number, appears in the exception). Its **ADR-level classification
remains exactly `SUBSTRATE_UNAVAILABLE`**, per this ADR's original §4
text, and the namespace is refused (fail closed) — `IDENTITY_MISMATCH`
is not, and must never be described as, a distinct `REFUSED` outcome
category of its own.

### 14. `repo.json` crash behavior

| Point | Behavior |
|---|---|
| Before `O_EXCL` | No file exists; a later attempt (by any correctly-locked process) creates normally. |
| After creation, before the write completes | File exists with 0 or partial bytes; a later locked reader's ordinary validation path fails schema/JSON validation and reports `SUBSTRATE_UNAVAILABLE` — never regenerated or overwritten automatically, identical to `state-root.json`'s crash-handling philosophy. |
| After the write, before file `fsync` | Process-crash atomicity only (this ADR's §15, unchanged) — no additional guarantee for a power-loss event. |
| After file `fsync`, before directory `fsync` | The file's content is durable; the directory entry's durability is unconfirmed, with no observable difference for a live (non-power-loss) process. |
| Directory-`fsync` failure | The **creating process's own call** reports failure (§10's cleanup-dominates rule); the file itself is left exactly as written, never deleted or reverted. A **later, fresh invocation** finds a genuinely complete and correct `repo.json`, validates it successfully, and proceeds — even though the original writer's own call failed. No claim beyond this ADR's existing power-loss-durability boundary is made. |

### 15. Precise state-root initialization / race classification

A four-state probe — `VALID`, `ABSENT`, `RETRYABLE_PARTIAL`,
`PERMANENTLY_INVALID` — replaces any ad hoc two-attempt substitute.
**The bounded retry window (2.0 s, polled every 50 ms, `time.monotonic`)
applies only when this process itself first observed `state-root.json`
absent, attempted `O_CREAT | O_EXCL`, and received `EEXIST`** — never
during ordinary startup, where any of the same content shapes is
immediately `SUBSTRATE_UNAVAILABLE`. Inside that one legitimate window,
only two content shapes are retried: an exactly-empty file, and content
that fails to parse as syntactically complete JSON (categorized
precisely as `JSON_SYNTAX_INVALID`, not a generic "malformed" bucket —
Python's JSON parser cannot always distinguish genuine truncation from
arbitrary invalid syntax, so this category is deliberately named for
what it actually is: syntactically incomplete-or-invalid JSON,
retryable **only** inside the post-`EEXIST` window, because our own
writer's single-shot complete write means a live writer can only ever
be observed as "nothing yet" or "some invalid-JSON-shaped prefix" —
never as a fully-parseable, wrong-schema document). Every other content
shape — invalid UTF-8, a duplicate JSON key, oversized content, a
symlink, a schema-invalid-but-syntactically-complete document, or any
other I/O failure — is `PERMANENTLY_INVALID` immediately, **even inside
the retry window**: none of these is a shape a live, still-writing
process could ever legitimately produce, so waiting out the window for
any of them would only delay an already-certain failure. `clock`/
`sleeper` are injectable seams (defaulting to `time.monotonic`/
`time.sleep`) so the state machine itself is tested deterministically
without a real wait; exactly one real cross-process test uses genuine
wall-clock timing.

### 16. Exact production ordering (documented contract, not a new function)

```
1. discover_repository_identity_and_context(repo_path)
      -> (RepositoryIdentity, TrustedRepositoryContext)
2. resolve_state_root_path() -> StateRootLocation
3. open_or_create_canonical_root(location) -> (root_fd, canonical_root_path)
4. directional containment validation (§6), on canonical paths only
5. probe/initialize state-root.json via root_fd -> StateRoot (owns root_fd thereafter)
6. acquire_repository_lock(state_root, identity.repo_key) -> LockHandle
7. load_or_create_repo_json(state_root, identity, repo_lock) -> RepositoryIdentity
   [Slice 3A-2 continues here; automatic pre-run reconciliation (§10)
   belongs exactly at this point in a later slice, before step 8]
8. new_lifecycle_id() -> lifecycle_id
9. exclusively create repos/<repo_key>/runs/<lifecycle_id>/ -> run_dir_fd
10. acquire_lifecycle_lock(run_dir_fd, repo_key, lifecycle_id) -> LockHandle
11. publish the initial PREPARING lifecycle.json (run_dir_fd-relative,
    same-directory temp file + fsync + os.replace + directory fsync)
```

Steps 0 (Git preflight, `check_git_preflight()`, ADR 0006 §6) runs
before step 1; it is omitted from the numbered list above because it
was already an existing cross-cutting precondition, not new to this
ADR's own composition.

### Milestone boundary

**Slice 3A-1** (this amendment's scope): `state-root.json` init/
validation; trusted repository identity and context discovery; the
repository-lock primitive; the generic lock primitive `LockScope`
already anticipates for lifecycle locks; `repo.json` creation/
validation while the correct repository lock is held. **Not** in
3A-1: `runs/<lifecycle-id>/` creation, any lifecycle-lock wrapper,
`lifecycle.json` of any kind, container/worktree/checkpoint-ref
attribution or reconciliation, `codeagent reconcile`/`--abandon`, the
maintenance trace, any `errors.ErrorCode`/`ErrorDomain` addition, or
any controller/CLI wiring. **Slice 3A-2** owns `runs/<lifecycle-id>/`
creation, the lifecycle-lock wrapper, and the initial `PREPARING`
`lifecycle.json` projection, ordered per §12 above. The remainder of
this ADR (attribution, reconciliation, abandonment, the maintenance
trace) remains later Milestone 3 work, unchanged in scope by this
amendment.

### Evidence

A multi-round, planning-only architecture review, including a real,
reproduced probe of the macOS case-canonicalization gap
(`/Users/chandrasekhar/code/codeagent` vs.
`/users/chandrasekhar/code/codeagent` both resolving via
`Path.resolve()` while preserving their distinct spelling) and a real
computation of the pinned `repo_key` test vector in §1. No
implementation exists yet; the test matrix recorded in
`ENGINEERING_LOG.md`'s Slice 3A-1 planning entry is the implementation
obligation this amendment creates.

### Implementation status (Slice 3A-1, added 2026-09-22)

Implemented and locally validated on macOS: `src/codeagent/
_lifecycle_fs.py` (shared fd-based filesystem-safety primitives,
including `resolve_state_root_path()` per §5 and §16 step 2),
`state_root.py` (§2, §15), `state_locks.py` (§11, §12's generic
primitive), and `repo_identity.py` (§4, §13, §14), with matching test
modules. A prior in-process test regression (a shared-module
`importlib.reload` that desynchronized exception class identity across
these four modules, plus several mid-test `monkeypatch.undo()` sites)
was found and corrected with a subprocess-isolated capability test and
`monkeypatch.context()` scoping; that specific fix changed no
production behavior. Separately, earlier hardening passes in this
slice did change production behavior — fail-closed operation-time
capability loading, descriptor ownership/cleanup, private-file
validation, lock-cleanup classification, state-root probing, and
`repo.json` validation were all corrected before final verification.
Verified: `py_compile` on all eight files; the four focused test files
collected and passed together as 199/199 in both forward and reverse
file order (no cross-test ordering dependency); the full suite passed
(2063/2063, including the 3 real-Docker tests with
`CODEAGENT_REQUIRE_DOCKER=1`); and a post-run resource check confirmed
no leftover `codeagent-verify` containers, extra git worktrees,
`refs/codeagent` refs, child processes, or temp state roots. A
focused security review of the four files, limited to high/medium
exploitable findings at an >=8/10 reporting threshold, produced no
reportable finding — two candidates were independently rejected at
3/10 and 2/10; this is not a claim that the slice is vulnerability-free
or fully audited (the security review itself was a macOS-only pass and
was not repeated on CI). This slice's implementation and automated
test coverage were also exercised successfully on GitHub-hosted Ubuntu
CI: commit `8aefa5f`, run
[35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790)
(`ubuntu-24.04` x86_64, Python 3.12) concluded success — the dedicated
mandatory real-Docker step passed 3/3 with no skips, the complete-
suite step passed 2,177 with 3 skipped. **Corrected 2026-09-23**: this
step also ran with `CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker
daemon, so those 3 skips were **not** the real-Docker tests, which had
already passed again within this same step; the narrowest supportable
claim is three platform/host-specific tests skipped (source inspection,
not proven from this run's own log, identifies the likely candidates as
Darwin-only tests in `test_evidence.py`/`test_lifecycle_fs.py` and a
case-insensitive-filesystem-dependent alias test in
`test_repo_identity.py` — see `ENGINEERING_LOG.md`'s dated erratum
entry for detail). No leftover `codeagent-verify` containers remained
afterward. This is `ubuntu-24.04` x86_64 evidence specifically, not a
general Linux or ARM64 claim. Slice 3A-2 now implements the
lifecycle-lock wrapper and
the `prepare_lifecycle()` composition (see that slice's own
"Implementation status" note below); the remaining limitation is that
neither slice is wired into `RunController`, the CLI, or any other
real entry point — that wiring remains later Milestone 3 work. This
note does not amend or restate the accepted design above it — it
records implementation status only.

### Implementation status (Slice 3A-2, added 2026-09-22)

Implemented and locally validated on macOS: `src/codeagent/
lifecycle_store.py` (§5, §6, §12, §16 steps 8–11 — the exact
`prepare_lifecycle()` composition: Git preflight, repository
discovery, trusted state root, repository lock, `repo.json`, a fresh
exclusive `runs/<lifecycle_id>/` directory, the lifecycle lock, and
the atomically published initial `PREPARING` `lifecycle.json`), plus
two small, genuinely reusable additions to Slice 3A-1's own modules:
`_lifecycle_fs.py` gained `create_exclusive_directory_at()` (never
adopts a pre-existing entry, unlike the shared managed-directory-chain
primitive) and `publish_private_file_atomically_at()` (same-directory
temp file, write, `fsync`, `os.replace`, directory `fsync`); a review
pass found and fixed a real outcome ambiguity in the latter — a
pre-installation file-`fsync` failure and a post-`os.replace`
directory-`fsync` failure both reported the same generic
`FSYNC_FAILED` reason, which conflated "nothing was installed" with
"the projection is installed but its durability is unconfirmed." A
new `LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED` reason (and
a matching `LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED` /
`.PROJECTION_DURABILITY_UNCONFIRMED` split one layer up) now
distinguishes the two outcomes explicitly, preserving cause-chain
causality and never deleting or reverting an already-installed file.
`state_locks.py` gained `acquire_lifecycle_lock()`, a thin wrapper
over the existing generic lock primitive with `LockKind.LIFECYCLE`
that — unlike `acquire_repository_lock` — opens no short-lived
parent-fd of its own, since its parent (the run directory) is the
caller's own long-lived descriptor.

The initial projection instantiates the complete schema (§5): all
three identities, diagnostic `run_id`/**canonical working-tree root**
(a review pass found and fixed this slice initially persisting the
Git *common* directory, typically `<repo>/.git`, instead — repository
discovery already refuses a bare repository, so `prepare_lifecycle()`
additionally fails closed explicitly if the working-tree root is ever
unexpectedly absent), `state=PREPARING`, both containers and the
worktree and checkpoint-ref attributions at their `absent` shape,
`failure=null`, and a zero-attempt empty-history reconciliation
summary. The `checkpoint_ref` field reuses
`checkpoint_session.CheckpointIntent`/`CheckpointTransition` directly
— a review pass found this slice had instead defined a second,
duplicate copy of ADR 0004 §5's own transition vocabulary
(`CheckpointRefIntent`/`CheckpointRefAttribution`), against this ADR's
own stated intent ("so Milestone 3's durable lifecycle store can
serialize this object rather than redesigning it").

A pure schema validator (`validate_lifecycle_json_schema`) enforces
the complete container (§7) and checkpoint-ref (§5) valid-combination
tables plus exact key-set (unknown-field) refusal at every nesting
level, and is now **object-format-aware**: every non-null persisted
Git object id in `checkpoint_ref` must be an exact lowercase-hex
SHA-1 (40 chars) or SHA-256 (64 chars) value matching the
*repository's actual* object format, supplied by the caller — a
review pass found the validator had instead accepted any nonempty
string (including single-character placeholders like `"A"`/`"B"`) as
a persisted SHA, which this ADR's §4 object-format requirement does
not permit. `worktree` and `failure`, by contrast, are **deliberately
narrowed** to only the one shape (`absent`/`null`) this slice actually
produces: unlike containers and checkpoint_ref, this ADR gives neither
field a combination table precise enough to validate their other
possible shapes (e.g. what `worktree.expected_head` must look like for
a `present` intent) with confidence, so this validator refuses every
other shape categorically rather than silently inventing and shipping
an unreviewed future-state contract. It has no production caller in
this slice (nothing yet reads an existing projection back) and is
exercised directly by unit tests only — the same accepted pattern this
ADR's `ContainerCleanupStatus.NOT_APPLICABLE` already uses elsewhere.
One schema detail remains a documented, reusable-bound choice rather
than a newly invented one: each `recent_failures` entry reuses the
ADR's existing 512-byte sanitized-detail bound (§4), pending Slice
3B's own confirmation once it actually populates that field.

A follow-up review of this same correction pass found the
object-format-aware OID check above still accepted the all-zero OID
as a persisted `checkpoint_ref` value — structurally valid-length hex,
but exactly what this ADR's §5 text already forbids ("`null` means
'no ref'; the zero OID appears only in Git argv"). Fixed at the shared
boundary this ADR's §5 text already designates for reuse:
`checkpoint_session._require_oid_shape()` (used by
`CheckpointTransition.__post_init__`) now refuses an all-zero-
character OID outright, so `validate_lifecycle_json_schema()`
inherits the rule through its existing `CheckpointTransition`
construction with no duplicate check added in `lifecycle_store.py`
itself. The same check was added to `CheckpointSession._validate_
against_repository()` so neither of that module's two validation
paths lets a raw `ValueError` escape past its own sanitized exception
boundary. `checkpoint_ref.ObjectFormat.zero_oid` (the Git-argv-only
value) is untouched.

`LifecycleLease` retains the state-root descriptor, the run-directory
descriptor, the repository lock, and the lifecycle lock for the
caller's required lifetime, releasing them on `close()` in the exact
order this ADR's §6/§13 ordering implies — lifecycle lock,
run-directory descriptor, repository lock, state-root descriptor —
attempting every stage regardless of an earlier stage's outcome, with
a cleanup failure dominating and chaining from the earliest failure
(or, via the context-manager protocol, from an in-flight body
exception, the same convention `StateRoot`/`LockHandle` already use).
A composition failure at any point unwinds exactly what was acquired
so far through the same `close()` path; nothing acquired is ever
leaked, and nothing already durably written (e.g. an installed
`lifecycle.json` that only the trailing directory-`fsync` then failed
to confirm) is ever deleted or reverted.

Verified: `py_compile` on all seven changed/new files (including
`checkpoint_session.py` for the zero-OID follow-up); the directly
affected test files (`test_checkpoint_session.py`,
`test_checkpoint_ref.py`, `test_lifecycle_store.py`,
`test_lifecycle_fs.py`, `test_state_locks.py`) collected and passed
together, 474 passed; the five focused test files (`test_lifecycle_fs.py`,
`test_repo_identity.py`, `test_state_locks.py`, `test_state_root.py`,
`test_lifecycle_store.py`) collected and passed together, 311 passed,
in both forward and reverse file order; the full local suite: 2,180
passed. With a real Docker daemon confirmed genuinely ready (not
merely the application open) and `CODEAGENT_REQUIRE_DOCKER=1` forcing
a skip to fail: the 3 dedicated real-Docker tests, 3 passed, 0
skipped; the complete suite, 2,180 passed, 0 skipped; no leftover
`codeagent-verify` containers afterward. A real two-process test
confirms both the repository lock and the lifecycle lock are
cross-process exclusive, and a real SIGKILL test confirms a fresh
process can still acquire both locks afterward (kernel-released
`flock`), creating a new, separate lifecycle_id/run directory rather
than adopting or touching the dead run's own directory — matching
this slice's explicit non-goal of reconciliation. Local evidence spans
both macOS and, via the real-Docker run above, the Docker Desktop
Linux VM. **GitHub-hosted Linux CI is now confirmed**: commit
`8aefa5f`, run
[35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790)
(`ubuntu-24.04` x86_64, Python 3.12) concluded success — the pinned
verification image was pulled and confirmed `linux/amd64`; the
dedicated mandatory real-Docker step passed 3/3 with no skips; the
complete-suite step passed 2,177 with 3 skipped. **Corrected
2026-09-23**: this step also ran with `CODEAGENT_REQUIRE_DOCKER=1`
against a ready Docker daemon, so those 3 skips were **not** the
real-Docker tests, which had already passed again within this same
step; see `ENGINEERING_LOG.md`'s dated erratum entry for the corrected
explanation. No leftover `codeagent-verify` containers remained
afterward. This is `ubuntu-24.04` x86_64 evidence specifically, not a
general Linux or
ARM64 claim, and it confirms this slice's own composition and test
suite only — it does not itself mitigate T-E1 or wire `prepare_
lifecycle()` into `RunController` or the CLI.

Not implemented, per this ADR's own accepted 3A-1/3A-2 boundary:
automatic pre-run reconciliation (§10 — the exact insertion point is
now marked in §16's own step list, between step 7 and step 8),
abandonment (§11), the maintenance trace (§12), and any
container/worktree/checkpoint-ref attribution or mutation (a static
AST-based test confirms no such identifier or mutating call is
reachable from this module). Nothing from this slice is wired into
`RunController` or the CLI. **T-E1 is not mitigated by this slice
alone** (`docs/threat-model.md`): nothing yet calls `prepare_lifecycle()`
before a real run starts. This note does not amend or restate the
accepted design above it — it records implementation status only.

---

## Amendment 2 (Accepted 2026-09-22): Milestone 3 Slice 3B-1 — initial-shape automatic reconciliation

Implementation status: **implemented.** This amendment fills in the
durable decisions §10/§12 leave unpinned, narrowed to the one slice
this repository actually builds. It does not reverse any earlier
decision.

### 1. Narrow slice boundary

Slice 3B-1 recognizes and reconciles only entries whose durable state
is `PREPARING` or `RECONCILING` **and** whose attribution is the
complete initial absent shape: both containers `{intent: absent, id:
null}`, worktree `{intent: absent, expected_head: null}`,
`checkpoint_ref` exactly `ABSENT_TRANSITION`, `failure: null`. After
freshly confirming every recomputed external resource (both container
role names, the recomputed worktree path, the recomputed checkpoint
ref) is absent, it writes `RECONCILING → RECONCILED`. It never removes
or mutates a container, worktree, or checkpoint ref, and it never
writes `LifecycleState.RECONCILIATION_FAILED` — that state remains
schema-defined but unwritten, reserved for a future slice that has a
real reason to stop retrying automatically (this slice never does,
because its only side effects are read-only inspection plus its own
idempotent, freely-retryable projection writes).

### 2. Clean-final cross-field invariant

`SKIPPED_TERMINAL` (zero lock, inspection, or mutation calls) is
returned only when `state ∈ {COMPLETE, RECONCILED}` **and** the same
complete absent-shape invariant above holds. A terminal state with any
non-absent attribution or a populated `failure` is `REFUSED`, never
skipped — the state value alone is never sufficient.

### 3. Enumeration classification

Within `runs/` and within a validated run directory, a **positively
observed** wrong state (malformed name, symlink, wrong type, wrong
owner, unsafe permissions, or an unrecognized inner entry) is
`REFUSED`; a **genuine inability to inspect** (a failed `stat`/`lstat`/
`listdir` syscall) is `SUBSTRATE_UNAVAILABLE`. This extends I6/I7
explicitly to this reconciliation substrate, which the original ADR
text does not cover. A malformed entry directly beneath `runs/` aborts
the whole pass before any legitimate entry is inspected or mutated,
since `runs/` is the shared trust boundary every entry depends on; an
unrecognized entry inside one otherwise-valid run directory refuses
only that one `lifecycle_id`. Inside a run directory, only
`lifecycle.json`, `lifecycle.lock`, and the exact temp-publication
pattern `^\.lifecycle\.json\.tmp-[0-9a-f]{16}$` are recognized; a
recognized temp leftover is never opened, trusted, or deleted, only
noted in the maintenance trace.

### 4. Maintenance-trace contract

One exclusive private file per pass, `repos/<repo_key>/maintenance/
<maintenance_id>.jsonl` (`maintenance_id`: 32 lowercase hex,
`secrets.token_hex(16)`), never reopened or resumed by a later pass.
Envelope: one canonical-JSON object per line, UTF-8, sorted keys. Every
event carries `schema_version=1`, `event_type`, `maintenance_id`,
`state_root_id`, `repo_key`, `trigger="pre_run"` (the only value this
slice emits; `"explicit"` is reserved for a later CLI slice), and a
fixed UTC second-precision timestamp
(`strftime("%Y-%m-%dT%H:%M:%SZ")`). Event types: `ReconciliationStarted`,
`ReconciliationEntryRecorded` (+ `lifecycle_id`, `run_id`, `outcome`,
`attempt_number`, per-role container `{id, confirmed_absent}`,
worktree `{confirmed_absent}`, checkpoint-ref `{ref_name,
confirmed_absent}`, `has_recognized_temp_leftover` (boolean — see
below), `detail`), `ReconciliationFinished` (+ per-outcome counts,
`blocked`). `has_recognized_temp_leftover` is `true` only when
reconciliation observed at least one valid, recognized lifecycle-
publication temporary file (the exact `^\.lifecycle\.json\.tmp-
[0-9a-f]{16}$` pattern, itself fd-relative, no-follow validated as a
private regular file) in that run directory — diagnostic evidence
only. It never means the file was opened, trusted, adopted, or
deleted; the recognized leftover is left exactly as found either way.
This is not a new decision: it is the explicit boolean representation
of the requirement, already accepted when this slice was implemented,
that a recognized temp leftover be noted in the maintenance trace
rather than silently ignored. Bounds: `run_id` reuses `RUN_ID_MAX_ENCODED_BYTES`
(256); `detail` reuses the existing 512-byte sanitized-detail bound;
a new `CONTAINER_ID_MAX_BYTES` (128) and `ref_name` bound (128); a new
whole-line bound `MAINTENANCE_EVENT_MAX_BYTES` (4096). Durability:
every event is written and `fsync`ed individually (never batched); the
`maintenance/` directory itself is `fsync`ed once, at file-creation
time. A future reader must tolerate and discard an unparseable line
only when it is the last line (the per-event `fsync`-before-next-write
ordering guarantees nothing else can be torn). Ordering: an entry's
trace event is written only after that entry's outcome is already
known — the trace is retrospective, never write-ahead; the projection
is the sole write-ahead-protected record.

Authority: **failure to create, write, `fsync`, directory-`fsync`, or
close the maintenance trace blocks admission of the new run in this
slice.** Slice 3B-1 has no controller, CLI, event sink, or other
observable channel through which an in-memory trace warning could ever
reach an operator, so a silently incomplete or missing trace would be
indistinguishable from one nobody ever looks at. This is a deliberate,
narrow policy for this slice — a later wiring slice, once a real
observable destination for such a warning exists, may revisit whether
trace-write failure should still be blocking. Trace success is
diagnostic evidence of a pass, never part of projection correctness
itself: if an entry reaches durable `RECONCILED` before the trace
finalizes, that entry's projection is correct and final regardless —
the pass still blocks this run, but a later pass recognizes the
already-clean-final entry via `SKIPPED_TERMINAL`, writes its own fresh,
complete trace, and may then proceed.

### 5. Attempt counting

`reconciliation.attempts_total` counts durable reconciliation
write-cycles, not process invocations or inspection attempts. It
increments exactly once, at a fresh (non-`RECONCILING`-resuming)
`PREPARING → RECONCILING` transition, and is carried forward unchanged
by the subsequent `RECONCILING → RECONCILED` write. A process that
finds a pre-existing `RECONCILING` projection treats it as continuing
the already-counted cycle: it repeats inspection from scratch (safe,
since inspection is read-only and idempotent) but does not increment
again before writing `RECONCILED`. An inspection failure performs no
projection write and cannot affect the durable counter. A
pre-installation publication failure of the incrementing write leaves
the previous durable value unchanged (the attempt happened but could
not be recorded). An installed-but-durability-unconfirmed write leaves
whatever complete, incremented value was actually installed.

### 6. Outcome vocabulary

Per-pass entry outcomes (§10, unchanged names): `RECONCILED`,
`SKIPPED_TERMINAL`, `SKIPPED_ACTIVE`, `REFUSED`, `FAILED`,
`SUBSTRATE_UNAVAILABLE`. `FAILED` covers both projection-publication
failure modes this slice can produce — pre-installation failure and
installed-but-durability-unconfirmed — distinguished only by their
`detail` string, both meaning "mutation attempted but not confirmed;
retried on later passes," per §10's own definition. `SKIPPED_ACTIVE`
blocks the pass (an invariant violation while the repository lock is
held, never treated as a benign concurrent run). A pass-level
enumeration or maintenance-trace failure aborts the entire pass and is
treated identically to `blocked=True`.

### 7. Missing projection

A run directory with no valid, identity-matched `lifecycle.json`
(missing, symlinked, hard-linked, unsafe, malformed, or oversized) is
`REFUSED`, exactly as any other corrupt or inconsistent state (I7). It
is never automatically deleted, repaired, or adopted. The designed
recovery path is future abandonment (§11, not yet implemented); until
it exists, such an entry blocks its repository, a named residual risk
of this slice.

### Milestone boundary

Slice 3B-1 owns exactly the above. Container/worktree/checkpoint-ref
*removal*, `RECONCILIATION_FAILED`, abandonment, the explicit
`codeagent reconcile`/`--abandon` CLI, retention, and
`RunController`/CLI wiring of `prepare_lifecycle()` itself all remain
later Milestone 3 work, unchanged in scope by this amendment.

### Evidence

`src/codeagent/reconciliation.py` and `tests/unit/test_reconciliation.py`
implement and verify every rule above, including real cross-process
SIGKILL-and-reconcile and real-Docker container/worktree/ref
inspection; see `ENGINEERING_LOG.md`'s dated entry for exact totals.

Implementation/test evidence (not a security review): confirmed on
GitHub-hosted Linux CI at commit
`048314e8713777f3401a2e64445e3e8da9507cc1`, run
[35814528028](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35814528028)
(`ubuntu-24.04` x86_64, Python 3.12, success) — the dedicated mandatory
real-Docker step 3 passed/0 skipped, the complete suite 2,246 passed/3
skipped. **Corrected 2026-09-23**: this step also ran with
`CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
skips were **not** the real-Docker tests, which had already passed
again within this same step; the narrowest supportable claim is three
platform/host-specific tests skipped (exact likely identities come
from source inspection, not this run's own `pytest -q` log — see
`ENGINEERING_LOG.md`'s dated erratum entry for detail). No leftover
`codeagent-verify` containers.

---

## Amendment 3 (Accepted 2026-09-23): Milestone 3 Slice 3B-2 — locked, authoritative lifecycle-projection writer

Implementation status: **implemented.** Fills in the durable write-side
API and exact transition rules this ADR's §5/§7 tables imply but never
themselves specified as a callable contract. Reverses no earlier
decision.

### 1. Writer boundary and API

`LifecycleLease.open_projection_writer() -> (writer, current)` is the
only sanctioned way to obtain a `_LifecycleProjectionWriter`. It
refuses categorically — before any raw `TypeError`/`AttributeError`/
invalid-descriptor error can escape — an incomplete lease
(`run_dir_fd`/`object_format`/`state_root` unset), then performs the
exact same complete lock-scope validation every write method uses
(held, `LIFECYCLE`-kind, exact `repo_key`/`lifecycle_id` match — which
also transitively refuses an already-`close()`d lease, since
`release()` clears `is_held`), then performs exactly one authoritative
read and returns it as the caller's first `expected`. The writer is
never constructed directly.

`LifecycleLease` gained `object_format` (the trusted `"sha1"`/`"sha256"`
value from `RepositoryIdentity.object_format`, set once in
`prepare_lifecycle()`) — never inferred from a caller-supplied SHA or
untrusted projection content.

The writer never performs a Docker or Git call, never inspects an
external resource, and never claims to have done so — every precondition
named below ("caller-confirmed absence," "confirmed applied") is the
future resource-owning caller's own responsibility.

### 2. Authoritative-read and stale-expectation behavior

Every write method: verify lock scope → load and fully validate the
currently installed authoritative projection fresh (a corrupt or
identity-mismatched file is refused exactly as `load_lifecycle_
projection` already refused it — `SCHEMA_INVALID`/`SUBSTRATE_UNAVAILABLE`
— and is never overwritten) → compare against the caller's `expected`
→ refuse a mismatch (`STALE_EXPECTED_PROJECTION`) before any
publication I/O → derive the candidate only from the freshly validated,
currently installed projection. A successful read here validates that
installed content; it cannot retroactively prove a prior failed
directory-`fsync` durable — no power-loss durability claim is made.

`STALE_EXPECTED_PROJECTION` is deliberately distinct from
`ILLEGAL_TRANSITION`: the former means the requested edge may be
perfectly legal from the *actual* current state — the caller's belief
is simply out of date (a compare-and-swap rejection); the latter means
the edge is not legal from *any* state. A refusal at this stage still
performed one authoritative *read* — described as "zero publication/
write I/O," never "zero I/O."

### 3. Lock enforcement

`_require_lease_lock_held()`: the lease's `lifecycle_lock` must be
non-null, held, and its `scope` must exactly equal
`LockScope(kind=LIFECYCLE, repo_key=lease.repo_key,
lifecycle_id=lease.lifecycle_id)` — never merely `is_held`. A
repository-kind lock, a mismatched `repo_key`/`lifecycle_id`, or an
incomplete lease's `None` identity fields are all refused
categorically (`WRONG_LOCK_SCOPE`) without ever constructing an invalid
`LockScope`.

### 4. Publication outcomes and recovery

- **Pre-installation failure** (`PROJECTION_PUBLICATION_FAILED`):
  nothing installed; the durable projection is confirmed still equal to
  the pre-call `expected`; retrying the same call with the same
  `expected` is accepted, not stale.
- **Installed, durability unconfirmed** (`PROJECTION_DURABILITY_UNCONFIRMED`):
  the new complete projection is currently installed (`os.replace`
  already confirmed it), but its directory-entry durability was not
  confirmed by this process — no power-loss durability claim is made
  either way. The caller's old `expected` is now stale.
  `writer.refresh()` performs one authoritative read (zero publication/
  write I/O) and returns the currently installed, fully validated
  projection for reconsideration; nothing retries automatically. A
  caller who ignores this and retries with the stale `expected` anyway
  is caught by `STALE_EXPECTED_PROJECTION`.
- **Temporary-publication cleanup unconfirmed** (`CLEANUP_UNCONFIRMED`):
  preserved as its own distinct, fail-closed reason — never silently
  treated as an ordinary retryable pre-installation failure.
- **Confirmed success**: the newly published projection is returned;
  callers thread it forward as their next `expected`.

### 5. Exact transition tables

**Owner lifecycle state** (`advance_lifecycle_state`):
`PREPARING→ACTIVE`, `ACTIVE→CLEANING`, `CLEANING→COMPLETE` (only with
the complete clean-final absent shape — both containers absent, worktree
absent, `checkpoint_ref == ABSENT_TRANSITION`, `failure` null — the
shared `is_projection_fully_absent_shape()` predicate, ADR's clean-final
rule and I15), plus exact same-state no-op *only* among these four
states. `RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED` are refused
unconditionally by this API, including an identical-state request —
those remain `reconciliation.py`'s own private, reconciler-owned write
path (`_publish_projection_state`), untouched by this amendment.

**Container** (`record_container_transition`, per role independently):
`(absent,null)→(creating,null)`, `(creating,null)→(present,<set>)`,
`(creating,null)→(absent,null)` (failed-create recovery — see below),
`(present,X)→(removing,X)`, `(removing,X)→(absent,null)`, plus exact
tuple no-op. Every other combination, including `(present,X)→(present,Y)`
for `Y≠X`, is `ILLEGAL_TRANSITION`.

`(creating,null)→(absent,null)` is an explicit clarification of the
accepted write-ahead/reuse rule, not an assumed analogy to
checkpoint-ref: legal only when the caller has independently confirmed,
by real Docker inspection outside this writer, that no container was
ever created for the attempt — enabling the role name to be safely
reused. This writer never performs that inspection itself.

**Checkpoint-ref** (`record_checkpoint_ref_transition`): the writer
never re-derives an outcome — `transition` is trusted to already be the
decided output of `checkpoint_session.CheckpointSession`'s own collapse
logic. Beyond `CheckpointTransition.__post_init__`'s existing per-record
shape validation, this amendment adds cross-transition SHA continuity:

| Edge | Continuity requirement |
|---|---|
| `absent→creating` | target's own shape only (schema-valid proposed SHA) |
| `creating→present` | target `accepted_sha == current.proposed_new_sha` |
| `creating→absent` | confirmed-unchanged create-failure recovery |
| `present→advancing` | target `accepted_sha == expected_old_sha == current.accepted_sha` |
| `advancing→present` | target `accepted_sha ∈ {current.accepted_sha, current.proposed_new_sha}` |
| `present→removing` | target `accepted_sha == expected_old_sha == current.accepted_sha` |
| `removing→absent` | confirmed-removal collapse |
| any, exact full-record equality | no-op |
| every other edge or SHA discontinuity | `ILLEGAL_TRANSITION` |

Every non-null SHA is additionally validated against the lease's
trusted `object_format` (never inferred from the SHA's own length).

Both resource methods share a uniform call order, applied strictly
before their own request validation: verify exact lock scope → load and
validate the authoritative projection → refuse a stale `expected` → only
then validate the requested role/shape/edge, so a wrong lock scope or a
stale `expected` can never be masked by an invalid request argument
being checked first.

### 5a. Resource-transition state gate (correction, 2026-09-23)

Both `record_container_transition()` and `record_checkpoint_ref_transition()`
additionally require, immediately after the mandatory lock verification
and authoritative load/stale comparison and *before* their own exact-
no-op handling, that the authoritative lifecycle state be
owner-controlled and nonterminal: `PREPARING`, `ACTIVE`, or `CLEANING`.
`COMPLETE`, `RECONCILING`, `RECONCILED`, and `RECONCILIATION_FAILED` are
all refused (`ILLEGAL_TRANSITION`) — including an exact resource
no-op — since none of those projections may ever be mutated again by
this live-owner writer boundary. This enforces the already-accepted
clean-final rule and I11/I15; it is not a new lifecycle state graph, and
it does not change the owner state graph in section 5 above.

`record_checkpoint_ref_transition()` also sanitizes its `transition`
argument's type immediately after this gate, before any field access:
a non-`CheckpointTransition` value (including `None`) is refused as
`ILLEGAL_TRANSITION` with fixed categorical text, never a raw
`AttributeError`/`TypeError`/`ValueError`.

### 6. `CheckpointSession` boundary — Option A

`CheckpointSession.establish()`/`advance()`/`delete()` set their
transitional intent and call the corresponding `CheckpointRef` method
on the very next line, with no seam between them — confirmed by direct
inspection before this amendment was written. **This writer's
`record_checkpoint_ref_transition` is therefore not yet a usable durable
write-ahead boundary in production**: it correctly persists whatever
transition it is given, at whatever moment it is called, but nothing
in production can call it at the ADR-required moment (before the Git
mutation) for a real checkpoint-ref operation today. `checkpoint_session.py`
is untouched by this slice — the smaller option, since that module is
already a live production collaborator (`RunController` requires it),
and adding a seam nothing yet uses would grow this slice's blast radius
onto currently-live behavior. A later slice must inject or refactor a
persistence seam into `CheckpointSession` before durable checkpoint-ref
write-ahead exists.

### 7. Deferrals unchanged

Populated `failure` writing and non-absent worktree writing remain
deferred, exactly as Slice 3A-2 and 3B-1 already narrowed them — no new
policy invented for either. Both remain refused at the schema-validation
layer (`SCHEMA_INVALID`) before this slice's own transition-edge logic
would ever see them.

### 8. Shared predicate

`is_projection_fully_absent_shape()` now lives in `lifecycle_store.py`
(previously a private `reconciliation.py`-only helper) and is imported
explicitly by `reconciliation.py` for its own terminal/nonterminal
recognition — the one predicate this ADR's clean-final rule and I15
require, used identically by both this writer's `CLEANING→COMPLETE`
guard and reconciliation's `SKIPPED_TERMINAL` recognition.

### Milestone boundary

Slice 3B-2 owns exactly the above: the projection-transition writer
substrate. No Docker or Git call, no `workspace.py`/`executor.py`
change, no controller or CLI wiring, no resource removal, no
abandonment, and no `checkpoint_session.py` change. T-E1's mitigation
status is unchanged by this slice — nothing here is wired into a real
run.

### Evidence

`src/codeagent/lifecycle_store.py` and
`tests/unit/test_lifecycle_store.py` implement and verify every rule
above; see `ENGINEERING_LOG.md`'s dated entry for exact totals.

Implementation/test evidence (not a security review): confirmed on
GitHub-hosted Linux CI at commit
`0bf66f65b8cbe37ea897af3eb00ca8741522a8da`, run
[35826244500](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35826244500)
(`ubuntu-24.04` x86_64, Python 3.12, success) — the dedicated mandatory
real-Docker step 3 passed/0 skipped, the complete suite 2,296 passed/3
skipped. **Corrected 2026-09-23**: this step also ran with
`CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
skips were **not** the real-Docker tests, which had already passed
again within this same step; the narrowest supportable claim is three
platform/host-specific tests skipped (exact likely identities come
from source inspection, not this run's own `pytest -q` log — see
`ENGINEERING_LOG.md`'s dated erratum entry for detail). No leftover
`codeagent-verify` containers.

---

## Amendment 4 (Accepted 2026-09-23): Milestone 3 Slice 3B-3 — durable checkpoint-ref transition-publication seam

Implementation status: **implemented.** Adds the one call-ordering
mechanism that lets Slice 3B-2's already-accepted
`record_checkpoint_ref_transition()` become genuinely usable — nothing
in Amendment 3's transition/edge tables changes; no new legal
checkpoint-ref transition edge is added here.

### 1. Protocol and adapter boundary

`checkpoint_session.py` gains a structural `CheckpointTransitionPublisher`
Protocol (`publish(transition: CheckpointTransition) -> None`) and an
optional, keyword-only `transition_publisher` constructor parameter on
`CheckpointSession`, defaulting to `None`. `checkpoint_session.py`
imports nothing new beyond `typing` (for the `Protocol` itself) — it
still knows nothing about `LifecycleProjection`, `LifecycleLease`, or
any filesystem/lock primitive.

`lifecycle_store.py` gains `LifecycleCheckpointRefPublisher`, the one
concrete implementation Milestone 3 provides: it wraps a
`_LifecycleProjectionWriter` and tracks its own `current` expected
`LifecycleProjection` across calls, so `CheckpointSession` never needs
to construct or thread a `LifecycleProjection` itself.

### 2. Exact pre-mutation and post-outcome ordering

Every `self._transition` reassignment in `establish()`/`advance()`/
`delete()` is immediately followed by a publish attempt, in this exact
sequence:

1. Assign the new transitional intent to `self._transition`.
2. Publish it (a no-op if no publisher is configured).
3. Only after step 2 succeeds does the corresponding `CheckpointRef`
   mutation (`create`/`advance`/`delete`) run.
4. After a confirmed Git outcome, assign the collapse and publish it.

`delete()`'s `ABSENT`-no-op early return publishes nothing and makes no
Git call — nothing changed, so nothing is recorded.

### 3. Assignment-first in-memory behavior when publication fails

If the **pre-mutation** publish (step 2) raises: the Git mutation is
never reached, the publication exception propagates unchanged, and
`self._transition` remains exactly the transitional intent just
assigned — no rollback, no retry, no reinterpretation.

If the **post-outcome** publish (step 4, or the confirmed-`UNCHANGED`
recovery collapse below) raises: the Git result already happened and
is never undone; the publication exception propagates; `self._transition`
remains the decided collapsed value regardless of whether it could be
durably recorded.

### 4. Projection-consistency failure dominance during `UNCHANGED` recovery

For `establish()`/`advance()`'s confirmed-`UNCHANGED` recovery path:
the original `CheckpointRefError` is retained, the recovery collapse is
assigned, and its publication is attempted. If that publication
succeeds, the original `CheckpointRefError` is re-raised unchanged. If
it fails, the publication exception is raised **explicitly** `from` the
original `CheckpointRefError` (`raise publish_exc from exc`) — a
deliberate chain, not incidental Python `__context__`. This is named
**projection-consistency failure dominance**, a distinct rule from this
repository's existing cleanup-dominance convention (release-order
resource cleanup): here, an already-decided in-memory collapse could
not be durably confirmed, which is a different failure class from an
unconfirmed release of an acquired resource.

Every other non-collapsing outcome (`UNEXPECTED`/`SYMBOLIC`/`UNKNOWN`)
leaves the record transitional and makes **no** publish call beyond the
original pre-mutation one — no collapse is invented to publish when
none was decided.

### 5. Adapter expected-projection tracking and `refresh()`

`LifecycleCheckpointRefPublisher.publish()` calls `writer.
record_checkpoint_ref_transition(expected=self._current,
transition=...)` and replaces `self._current` **only** after that call
returns normally. `publish()` does not catch or translate anything:
`record_checkpoint_ref_transition()` both freshly loads the currently
installed authoritative projection and performs the write, so every
`LifecycleStoreError` it can raise propagates unchanged — including but
not limited to `STALE_EXPECTED_PROJECTION`, `WRONG_LOCK_SCOPE`,
`ILLEGAL_TRANSITION`, `PROJECTION_PUBLICATION_FAILED`,
`PROJECTION_DURABILITY_UNCONFIRMED`, `CLEANUP_UNCONFIRMED`, and the
authoritative-load failures `SCHEMA_INVALID`/`SUBSTRATE_UNAVAILABLE`.
`current` is left exactly as it was before the failed call —
`PROJECTION_DURABILITY_UNCONFIRMED` is never silently treated as
success.

`refresh()` is an explicit, separate method: it calls the writer's own
`refresh()`, updates `current` to the currently installed authoritative
projection, and returns it. It is **never invoked automatically** by
`publish()`. A future integration owner decides when to call it — this
slice only provides the operation.

### 6. No automatic retry

Neither `CheckpointSession`'s publish calls nor
`LifecycleCheckpointRefPublisher.publish()`/`refresh()` retry anything
automatically. Every failure propagates to the caller, once.

### 7. Default-`None` backward compatibility

Every existing `CheckpointSession` caller remains source-compatible and
behaviorally unchanged when the publisher is omitted:
`transition_publisher` defaults to `None`, and `_publish()` no-ops when
unset. The pre-existing behavioral tests in `test_checkpoint_session.py`
continue to pass unmodified; the file's own static import-boundary
assertion was deliberately updated to admit the new `typing` import (see
Evidence below).

### 8. Controller error translation remains deferred

This seam is real and production-shaped — proven by a real end-to-end
test (real repo, real `prepare_lifecycle()`, real `_LifecycleProjectionWriter`,
real `LifecycleCheckpointRefPublisher`, real `CheckpointRef`, real
`CheckpointSession`) — but it is **not** itself controller-ready.
`RunController` today catches only `CheckpointRefError`/
`CheckpointSessionError` (`_map_checkpoint_error`); a publisher
exception (a `LifecycleStoreError`) is a new exception family that
future controller wiring must explicitly translate. This slice does
not add that translation and does not claim the seam is ready for
`RunController` integration on its own.

### Milestone boundary

Slice 3B-3 owns exactly the above: the publication seam in
`checkpoint_session.py` and its one adapter in `lifecycle_store.py`. No
`RunController`, `executor.py`, `workspace.py`, CLI, container/worktree
handling, abandonment, reconciliation, signal handling, model
integration, or UI change. No new legal checkpoint-ref transition edge.
No change to `docs/threat-model.md` — nothing here is wired into a real
run, so no threat-model claim becomes true or false by this slice.

### Evidence

`src/codeagent/checkpoint_session.py`, `src/codeagent/lifecycle_store.py`,
`tests/unit/test_checkpoint_session.py`, and
`tests/unit/test_lifecycle_store.py` implement and verify every rule
above, including a real end-to-end test proving durable checkpoint-ref
publication with no mocking of the writer; see `ENGINEERING_LOG.md`'s
dated entry for exact totals.

Implementation/test evidence (not a security review): confirmed on
GitHub-hosted Linux CI at commit
`bc8cb770bcfeea9a8161c102536e9b69896f24ef`, run
[35889103564](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35889103564)
(`ubuntu-24.04` x86_64, Python 3.12, success) — the pinned verification
image confirmed `linux/amd64`, the dedicated mandatory real-Docker step
3 passed/0 skipped, the complete suite 2,326 passed/3 skipped.
**Corrected 2026-09-23**: this step also ran with
`CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
skips were **not** the real-Docker tests, which had already passed
again within this same step; the narrowest supportable claim is three
platform/host-specific tests skipped (exact likely identities come
from source inspection, not this run's own `pytest -q` log — see
`ENGINEERING_LOG.md`'s dated erratum entry for detail). No leftover
`codeagent-verify` containers.

---

## Amendment 5 (Accepted 2026-09-23): Milestone 3 Slice 3B-5 — safe reconciliation and removal of ADR-attributable Docker containers

Implementation status: **implemented.** Extends Slice 3B-1's automatic
reconciliation with the container half of section 7's persisted-
combination table: observation, ownership proof, write-ahead
transitions, and removal. Worktree and checkpoint-ref removal remain
entirely out of scope, unchanged from Slice 3B-1.

### 1. Widened eligibility

Slice 3B-1 recognized only `PREPARING`/`RECONCILING` entries in the
complete initial absent shape. A crash can leave a dead owner in any
owner-writable state, not only `PREPARING` — `record_container_
transition` permits container mutation from `PREPARING`, `ACTIVE`, or
`CLEANING` alike (Amendment 3's resource-state gate). Reconciliation
eligibility is therefore widened to `PREPARING`/`ACTIVE`/`CLEANING`/
`RECONCILING`, with the required shape narrowed correspondingly: the
worktree and checkpoint ref must both be at their initial absent shape
and `failure` must be null (`lifecycle_store.
is_projection_reconciliation_eligible_shape`), while either container's
own shape is otherwise unconstrained — it is exactly what this slice
now inspects and, where legal, resolves. `COMPLETE`/`RECONCILED`
terminal recognition is unchanged.

### 2. A reconciliation-specific container edge is not the live owner's edge

`record_container_transition`'s own `CREATING -> ABSENT` edge is
documented as meaning "no container was ever created" — a strong,
historical claim only the process that never called `docker create`
can make. Reconciliation can only ever prove a weaker, point-in-time
claim: "a fresh, successful, unfiltered listing shows no container
currently bears the deterministic name." A dead owner could in
principle have created and even started a container between its own
last durable write and its crash, and it could reappear as a Docker
daemon restart artifact; reconciliation's claim is bounded by what its
own observation actually shows, not by anything it can conclude about
history.

Because of this, reconciliation writes exclusively through its own new
table, `_RECONCILER_CONTAINER_TRANSITION_EDGES` (`lifecycle_store.py`):
`(creating, absent)`, `(creating, removing)`, `(present, removing)`,
`(removing, absent)` — distinct from, not layered on top of,
`_CONTAINER_TRANSITION_EDGES`. Notably absent: any direct
`present -> absent` edge. A persisted `present`/`removing` role that a
fresh observation already shows confirmed absent must still pass
through the `removing` write-ahead step before absence is declared —
there is no shortcut, even when no `docker rm` call will actually be
issued.

### 3. `_publish_reconciler_container_transition`

A new function in `lifecycle_store.py`, parallel to but distinct from
`_LifecycleProjectionWriter.record_container_transition`: the live-
owner writer is categorically refused while the authoritative state is
`RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED` (Amendment 3's
resource-state gate) — precisely the states a reconciliation pass runs
in — so reconciliation needed its own write path rather than reusing
that one. It hard-codes its target `state` to `RECONCILING`; every
other state transition, including the terminal `RECONCILING ->
RECONCILED` collapse, remains `_publish_projection_state`'s own
exclusive responsibility, unchanged.

Per call: validates the current state is eligible, the role is exactly
`baseline` or `verification`, the id grammar matches the intent (full
64-lowercase-hex, or `None`), the requested edge is legal
(`removing -> removing` additionally requires the same id, for a
resumed in-flight removal), and `attempts_total` is consistent with a
fresh cycle (exactly current + 1) or a resumed one (unchanged) — a
mismatch is `ILLEGAL_TRANSITION`, never inferred from a Docker
observation. A true no-op (nothing published) requires all three:
state already `RECONCILING`, `attempts_total` already correct, and the
target role's attribution already exactly equal — anything less still
performs a real publish, so a first entry into `RECONCILING`, or a
resumed cycle's own attempt bookkeeping, is never skipped merely
because a dead owner had already published a matching container shape.
One atomic `publish_private_file_atomically_at` call per invocation;
state and the container edge are never split across two writes.

### 4. Entry-wide inspection order

Per entry, strictly: load and validate the projection; confirm the
checkpoint ref, then the worktree, absent (unchanged from Slice 3B-1,
reordered ahead of containers); one complete, unfiltered
`docker ps -a --no-trunc --format '{{.ID}}\t{{.Names}}'` listing
(`reconciliation._docker_ps_all_id_name_pairs`, strictly parsed —
exactly one tab-separated id/name pair per row, 64-lowercase-hex id,
Docker's own name grammar, no duplicate id or name anywhere, or the
whole listing is untrusted); an ownership-proof
`docker inspect --type container --format
'{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{json .Config.Labels}}' <id>`
(`reconciliation._docker_inspect_ownership`, by immutable id only,
16 KiB bound) for every candidate either role's classification needs;
both roles' complete decisions are computed before either is mutated —
a conflict (ownership mismatch, ambiguity, or a genuine inspection
failure) on either role aborts the whole entry with zero mutation
attempted for both.

### 5. Write-ahead, then baseline-first removal

Once both decisions are conflict-free: every required write-ahead or
direct confirmed-absence transition is published for baseline, then
verification — entirely before any `docker rm` is issued for either
role. Only after both roles' write-ahead writes are durably confirmed
does removal begin, baseline first: `docker rm --force <id>` (skipped
entirely when nothing was ever observed live — e.g. the "already
confirmed absent while `present`" case above), then always, regardless
of that removal attempt's own outcome (launch failure, timeout,
overflow, nonzero exit, or unconfirmed termination alike — `docker
rm`'s own result is never authoritative), one fresh independent strict
listing, classified as follows. Neither the name nor the id present in
that listing -> confirmed absent -> `removing -> absent`. A listing-
level disagreement about which container exists (the id under a
different name, or a different id at the expected name) -> `REFUSED`,
retaining `removing(id)`, with no further inspection — the listing
alone already disproves continuity. The exact owned id/name pair still
present in that listing is not by itself sufficient to conclude
anything beyond absence: the surviving candidate is re-inspected by
immutable id, exactly like the original classification (section 4)
did, requiring both identity and all four required labels again before
this function will report anything further. A well-formed re-inspect
that still confirms ownership -> `FAILED`, retaining `removing(id)`. A
well-formed re-inspect whose identity or labels now disagree ->
`REFUSED`, retaining `removing(id)`. A failed or malformed re-inspect
-> `SUBSTRATE_UNAVAILABLE`, retaining `removing(id)`. Verification's own removal is only attempted
once baseline reaches durable absence this pass; if baseline does not,
the entry stops there for this pass (verification's own write-ahead,
already published, stands and is resumed on a later pass). The final
`RECONCILING -> RECONCILED` collapse (`_publish_projection_state`,
unchanged) is written only once both containers, the worktree, and the
checkpoint ref are all durably absent.

### 6. Maintenance-trace container ids

`ReconciliationEntryResult` gains `baseline_id`/`verification_id`
(`str | None`), populated from a positively observed candidate id and
retained even after a successful removal — the maintenance trace is
retrospective, never write-ahead (Amendment 2 section 4); the
projection's own write-ahead `removing(id)` transition is what actually
protects the id before removal, not the trace. `None` when a role was
already absent or no well-formed candidate was ever observed for a
conflict.

### Milestone boundary

Slice 3B-5 owns exactly the above: container reconciliation and
removal inside `reconciliation.py`, plus the one new writer primitive
and predicate in `lifecycle_store.py`. No `executor.py` change, no
lifecycle-aware container creation, no producer-side publisher seam
wired into `DockerVerifier`, no deterministic-name creation, no
`RunController` or CLI wiring, and no worktree or checkpoint-ref
removal — all later Milestone 3 work. `docs/threat-model.md`'s T-E1 is
unchanged by this slice specifically (nothing here changes when or
whether `prepare_lifecycle()` is called before a real run starts); a
dead run's *containers* are now recoverable once reconciliation does
run, extending the partial T-E1 mitigation Slice 3B-1 already recorded
from worktree/ref-adjacent state to containers as well.

### Evidence

`src/codeagent/reconciliation.py`, `src/codeagent/lifecycle_store.py`,
and `tests/unit/test_reconciliation.py` implement and verify every rule
above, including real Docker fixtures created directly with the exact
deterministic names and required labels (never through `DockerVerifier`)
proving both a genuine owned-removal path and a genuine unlabeled-
conflict refusal end to end; see `ENGINEERING_LOG.md`'s dated entries
for exact local verification totals and evidence (macOS, a real Docker
daemon, `CODEAGENT_REQUIRE_DOCKER=1`).

**Confirmed on GitHub-hosted Linux CI** (commit
`923d99a6596771c37defb801237e4642c56a3305`, run
[35954619345](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35954619345),
job `Test (ubuntu-24.04, Python 3.12)`, `ubuntu-24.04` x86_64, Python
3.12, conclusion `success`): the mandatory Docker preflight passed; the
pinned verification image was pulled and confirmed `linux/amd64`; the
dedicated real-Docker step ran with `CODEAGENT_REQUIRE_DOCKER=1` and
passed 3 tests, 0 skipped; the complete-suite step also ran with
`CODEAGENT_REQUIRE_DOCKER=1` and reported 2,532 passed, 3 skipped —
since the real-Docker tests already passed within this same step under
that env var, these 3 skips are not the real-Docker tests; the run
used `pytest -q`, which prints no test identities, so these are three
platform/host-specific skips, not a guessed identity. This slice's own
`codeagent-baseline-*`/`codeagent-verification-*` leftover-container
assertion ran and passed inside this same complete-suite step; the
workflow's separate final cleanup step checks only the unrelated
`codeagent-verify` family (Milestone 1's, unchanged by this slice) and
confirmed empty. This is implementation/automated-test evidence
only — it does not constitute or substitute for a security review, and
is `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim.

## Amendment 6 (Accepted 2026-09-27): Milestone 3 Slice 3B-6 — lifecycle-aware `DockerVerifier` container production

Slice 3B-5 gave reconciliation a container-removal path but nothing yet
*produced* a container under a deterministic name with the required
ownership labels — `executor.py` still minted a UUID-suffixed name for
every attempt and wrote no lifecycle projection at all. This amendment
accepts the producer-side design that closes that gap, entirely
opt-in: `DockerVerifier` behaves exactly as before when constructed
without a `lifecycle_context`, and adopts deterministic naming,
labeling, and durable write-ahead publication only when one is
supplied.

### 1. Dependency inversion at the exception boundary

`executor.py` must never import, catch, compare, annotate with, or
expose `lifecycle_store.LifecycleStoreError`/`LifecycleStoreFailure`.
A new dependency-light leaf module, `container_lifecycle.py` (stdlib
only), defines the producer-facing vocabulary instead: `ContainerRole`,
`ContainerIntent` (re-imported by `lifecycle_store.py` under the same
name, for source compatibility — it is no longer defined there),
`ContainerTransitionPublisher` (a structural `Protocol`, not
runtime-checked, matching the existing `checkpoint_session.
CheckpointTransitionPublisher` precedent), `ContainerPublicationFailure`,
and `ContainerPublicationError`. `lifecycle_store.
LifecycleContainerPublisher` is the *only* place a `LifecycleStoreError`
is ever caught and translated (`raise ContainerPublicationError(...)
from exc`, preserving causality); `executor.py` catches only
`ContainerPublicationError`, never a blanket `Exception`/`BaseException`
— a genuine programming bug or cancellation propagates unchanged.

The translation is exhaustive and explicit: `lifecycle_store.
_CONTAINER_PUBLICATION_FAILURE_MAP` maps every current
`LifecycleStoreFailure` member (subscript access, never `.get(...,
default)`, so an unmapped future member raises `KeyError` immediately
rather than silently misclassifying); `LIFECYCLE_ID_COLLISION` and
`RECONCILIATION_BLOCKED` map to `UNCLASSIFIED` since both are confirmed
unreachable from `record_container_transition`'s own call path today.
`tests/unit/test_lifecycle_store.py` asserts the map's keys equal the
complete real enum.

### 2. Shared strict Docker observation

A second new leaf module, `_docker_ownership.py` (depends only on
`_bounded_subprocess`), generalizes the strict `docker ps -a`/`docker
inspect` parsing grammar that previously existed as three independent,
hand-written copies (`executor._parse_cleanup_listing`,
`reconciliation._docker_ps_all_id_name_pairs`/`_docker_inspect_ownership`).
It exposes both pure parsers (`parse_ps_all_output`/
`parse_inspect_output`) and full run-and-parse convenience functions.
`executor.py`'s new lifecycle-aware code calls the convenience
functions directly; `reconciliation.py` deliberately keeps issuing its
own `run_bounded_stdout` call (so its own existing tests, which
monkeypatch `reconciliation.run_bounded_stdout` directly, continue to
intercept every Docker call that module makes unchanged) and delegates
only the parsing step to the shared pure parsers — a thin-delegator
migration, not a behavior change. `CONTAINER_LABEL_*`/`labels_match()`
also move to `container_lifecycle.py`, re-exported from
`reconciliation.py` under their existing names for the same reason.

### 3. Deterministic naming and labeling, no `attempt` label

`container_lifecycle.deterministic_container_name(role, lifecycle_id)`
produces `codeagent-baseline-<lifecycle_id>`/
`codeagent-verification-<lifecycle_id>` — stable across sequential
attempts within the `verification` role, so `DockerVerifier.run()`
reuses one container name across attempts rather than minting a fresh
UUID-suffixed one each time. `required_labels()`/`labels_match()`
implement section 7's exact four required labels and no `attempt`
label; extra or image-provided labels (e.g. baked into the pinned
image's own Dockerfile) never defeat ownership proof — only the four
required keys' presence/value is checked, never exclusivity.

### 4. Full ordered algorithm and the corrected cleanup-disposition contract

`DockerVerifier` gains an optional `lifecycle_context:
DockerVerifierLifecycleContext | None` constructor parameter, holding
only a `publisher`. **Identity binding (correction pass)**:
`lifecycle_id`/`state_root_id` are *not* independently supplied
constructor fields alongside `publisher` — that shape let the two
disagree (the publisher durably recording lifecycle A's transitions
while Docker names/labels used lifecycle/state-root B), a real,
dangerous mismatch a crash could leave unattributable to any
reconciler. `container_lifecycle.ContainerTransitionPublisher` (the
Protocol `publisher` must satisfy) instead exposes `lifecycle_id`/
`state_root_id` as read-only properties itself, and
`DockerVerifierLifecycleContext.lifecycle_id`/`state_root_id` are
computed properties that simply read `publisher.lifecycle_id`/
`publisher.state_root_id` — the single source of identity, validated
eagerly against the canonical 32-lowercase-hex grammar (redefined
independently in `container_lifecycle.py` rather than importing the
persistence stack's own copy) at context-construction time, before this
context can ever be handed to a `DockerVerifier`. `lifecycle_store.
LifecycleContainerPublisher` implements these properties by reading
directly from its own `current` projection's identity fields, so a
real writer's publisher is always the projection's own truth. When
present, `_execute()` dispatches to a new `_execute_lifecycle_aware()`
path; the legacy path (`lifecycle_context is None`) is completely
unmodified and remains the sole code path when it is omitted.

The lifecycle-aware algorithm publishes `CREATING` write-ahead, then
performs a read-only pre-create occupied-name check
(`docker_ps_all_id_name_pairs`/`docker_inspect_ownership`); an owned
occupant is recovered via `CREATING(implicit)->PRESENT(id)->REMOVING(id)
->rm(id)->confirmed absence->ABSENT` before this invocation's own
`docker create` ever runs; a foreign/unproven occupant is refused
without any mutation. **Correction pass**: a fully successful occupied-
name recovery collapses the projection all the way back to `ABSENT` —
there is no direct `ABSENT->PRESENT` edge in the live-owner's own
transition table (only `ABSENT->CREATING`), so this invocation must
re-publish its own `CREATING` immediately afterward, before it can ever
publish `PRESENT` for its own container below; a failure on that
re-publish is classified identically to the very first `CREATING`-
publish failure (`NOT_APPLICABLE` — nothing of this invocation's own
doing exists, since the prior occupant is already confirmed removed).
This gap was found by the real end-to-end integration test (a real
writer genuinely enforces the edge table; the mock-based unit tests'
fake publisher originally did not, and was hardened in the same pass to
enforce the identical edge table locally). `docker create` sets the
four required labels. On success, `PRESENT(id)` is published strictly
before `docker start --attach`; `start`/`inspect` are otherwise
unchanged from the existing Milestone 1/3B-4 implementation.
`REMOVING(id)`/`ABSENT` are published strictly around the final `docker
rm` (via a new id-only `_cleanup_by_id`, never the legacy name-
fallback-capable `_cleanup`, and never a name-targeted `docker rm`).
Every publish happens strictly before its corresponding Docker
mutation; any `ContainerPublicationError` stops this invocation before
the next mutation — no automatic refresh, retry, or continuation.

**The exact public `ContainerCleanupStatus` contract** (binding
correction to the slice's own internal working drafts, superseding an
earlier internal draft that conflated some of these rows):
`NOT_APPLICABLE` is legal only when this invocation never entered its
own `docker create`. Before create: a `CREATING` publication failure, a
foreign/conflicting occupied name, or a pre-create listing/inspection
failure are all `NOT_APPLICABLE` — nothing this invocation created
could possibly exist yet. After `docker create` has been entered,
including a bare launch failure: a fresh strict observation confirming
absence is `CONFIRMED_ABSENT` even though the lifecycle projection
conservatively remains `CREATING` (this observation is a point-in-time
claim, not the live-owner's own stronger "nothing was ever created"
precondition its own `CREATING->ABSENT` edge requires, so no publish is
attempted for this row); a proven-owned candidate recovered
successfully is `CONFIRMED_ABSENT`; an owned candidate that remains, or
whose removal is unconfirmed, is `UNCONFIRMED`; and a foreign/
conflicting candidate or an unavailable/malformed observation is also
`UNCONFIRMED` — never `NOT_APPLICABLE` once create has been entered.
`create_attempted` is retained purely as diagnostic evidence and never
drives this decision; the internal `_CleanupDisposition` enum
(`NOT_APPLICABLE`/`REQUIRES_ID_CLEANUP`/`CONFIRMED_ABSENT`/
`UNCONFIRMED_DEFERRED`) is the single field that does.

`REMOVING(id=None)` is structurally unreachable: the two call sites
that publish `REMOVING` always carry a real, freshly-validated,
non-`None` id (a recovered occupant's id, or this invocation's own
`_parse_create_id`-validated id) — proven by a spy-publisher test
asserting no `(REMOVING, None)` call ever occurs across every branch.

### 5. Residual-state-to-reconciliation table

Unchanged from Slice 3B-5's own accepted table (section 2 above), now
also proven end to end with a real producer: `CREATING` (SIGKILL before
this invocation's own `docker create`) → no container exists → the
reconciler's `CREATING` branch collapses directly to absent;
`PRESENT(id)` (SIGKILL after create, before start) → a created,
never-started container exists → the reconciler's `PRESENT`/`REMOVING`
branch proves ownership and removes it; `REMOVING(id)` (SIGKILL after
start/inspect, before removal) → a started-and-finished container
exists → the reconciler's same-id-continuity `OWNED_REMOVE` path
resumes removal. All three are proven with real, independently spawned
SIGKILL child processes against a real Docker daemon in
`tests/integration/test_slice_3b6.py`, each followed by a fresh
`reconcile_repository()` pass.

### 6. CI leftover-container detection, corrected

`docker ps -a --filter name=X` is Docker's own substring match, not an
anchored family-grammar proof. `.github/workflows/ci.yml`'s leftover-
container check is corrected to list every container name once and
apply an anchored client-side `grep -E
'^codeagent-(verify-|baseline-|verification-)'` covering all three
families (the legacy UUID-suffixed `codeagent-verify-*` and the
deterministic `codeagent-baseline-*`/`codeagent-verification-*`),
remaining strictly detection-only. `tests/unit/
test_ci_container_leftover_check.py` proves the exact pattern detects
all three real families and rejects decoy substring names (e.g.
`some-codeagent-verify-thing`).

### Milestone boundary

This slice is exactly: `container_lifecycle.py` and
`_docker_ownership.py` (new leaf modules), `executor.py`'s opt-in
lifecycle-aware production path, `lifecycle_store.
LifecycleContainerPublisher` and its exhaustive publication-failure
map, `reconciliation.py`'s import-only migration to the shared leaf
modules (no behavior change), and the CI workflow's anchored leftover
check. Explicitly out of scope, unchanged from every prior Milestone 3
slice: `RunController`/CLI wiring, abandonment, worktree or checkpoint-
ref removal, signal handling, and model integration. `docs/
threat-model.md`'s T-E1 entry is unaffected by this slice specifically
— nothing here changes when or whether `prepare_lifecycle()` is called
before a real run starts; this slice only makes the *producer* side of
an eventual real run lifecycle-aware, proven here via direct
construction, not via any existing entry point.

### Evidence

`src/codeagent/container_lifecycle.py`, `src/codeagent/
_docker_ownership.py`, `src/codeagent/executor.py`, `src/codeagent/
lifecycle_store.py`, and `src/codeagent/reconciliation.py` implement
and verify every rule above. Three same-day correction passes preceded
this evidence being considered final (see `ENGINEERING_LOG.md`'s dated
entry for the complete detail): the first fixed the identity-binding
gap in section 1 and a real `ILLEGAL_TRANSITION` bug in the occupied-
name recovery fall-through (section 4); the second fixed a fail-open
gap in `tests/integration/test_slice_3b6.py`'s own cleanup helpers
(`_container_exists()` previously treated any `docker inspect` failure,
including a genuinely unavailable daemon, as confirmed absence — now
derived exclusively from a fresh, strict `_docker_ownership.
docker_ps_all_id_name_pairs()` listing, with a listing failure
propagating rather than being silently treated as absence); the third
fixed that same test file's own Docker-availability gating, which had
been a module-wide skip covering even its five mock-only cleanup-helper
regressions (which make no Docker call and exist specifically to test
failure injection) — replaced with a per-test `requires_docker` marker
applied only to the seven genuine real-Docker tests, with a dedicated
structural test pinning that exact placement. All three passes are
test-infrastructure/production-bug corrections to this same slice, not
scope changes.

**Final, post-all-correction-passes totals** (macOS, Docker Desktop,
real daemon, `CODEAGENT_REQUIRE_DOCKER=1`): the thirteen-file focused
set (the eight established since Slice 3A-1, plus
`test_bounded_subprocess`, `test_container_lifecycle`,
`test_docker_ownership`, `test_executor`, and
`test_ci_container_leftover_check`) collected and passed together,
1,025 passed, in both forward and reverse file order; the directly
affected set (`test_executor` + `test_lifecycle_store` +
`test_slice_3b6`), 336 passed; the dedicated real-Docker tests
(`test_slice_c` + `test_slice_3b6`), 16 passed, 0 skipped; the complete
suite, 2,616 passed, 0 skipped, identical with and without
`CODEAGENT_REQUIRE_DOCKER=1` whenever Docker is actually available; no
leftover containers of any CodeAgent family (a strict listing covering
all three name families), extra worktrees, `refs/codeagent` refs,
processes, or temp state roots afterward; `git diff --check` clean.
`tests/integration/test_slice_3b6.py` is 13 tests total (7 genuine
real-Docker end-to-end/SIGKILL tests, each individually marked; 5
mock-based regression tests for its own cleanup helpers; and 1
structural test proving the marker placement itself) — real end-to-end
evidence (real `prepare_lifecycle()` lease, real
`_LifecycleProjectionWriter`, real `LifecycleContainerPublisher`, real
Docker — no mocking anywhere in the seven real-Docker tests) including
the three real-SIGKILL boundary tests, and the five mock-only tests
plus the structural test all run and pass with Docker unavailable.
**Confirmed on GitHub-hosted Linux CI** (commit
`388e80a496fe39075c8261ab9c407d16c7130b82`, run
[36338011857](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36338011857),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the mandatory
Docker preflight passed and the pinned verification image was pulled
and confirmed `linux/amd64`. The dedicated "Run real Docker
verification tests" step ran only `tests/integration/test_slice_c.py`
(3 passed, 0 skipped) — it did not run `test_slice_3b6.py`. This
slice's own seven Docker-dependent tests ran instead inside the
separate "Run complete test suite" step (`python -m pytest -q`, with
`CODEAGENT_REQUIRE_DOCKER=1` confirmed present), which reported `2613
passed, 3 skipped`; that step used `pytest -q`, which prints no test
identities, so the three skips are three unidentified platform/host-
specific skips, never guessed at, and are not evidence of Docker
unavailability (the daemon, image pull, and `CODEAGENT_REQUIRE_DOCKER=1`
were all already confirmed for that same step). Local macOS evidence
(2,616 passed, 0 skipped) and this CI run account for the identical
2,616 collected outcomes. The final leftover-container check (the
anchored `grep -E '^codeagent-(verify-|baseline-|verification-)'` over
a complete `docker ps -a` listing) returned empty and succeeded. This is
implementation/automated-test evidence only, scoped specifically to
GitHub-hosted `ubuntu-24.04` x86_64 — not a general Linux or ARM64
claim, and not a security review. It does not change T-E1's existing
partial-mitigation boundary in `docs/threat-model.md`, and does not
claim any `RunController`/CLI entry-point wiring exists.

## Amendment 7 (Accepted 2026-09-27): Milestone 3 Slice 3B-7 — shared lifecycle-projection coordination and the controller-facing checkpoint lifecycle-publication boundary

Slice 3B-6 gave containers a durable write-ahead publisher; Amendment 4
(Slice 3B-3) gave the checkpoint ref one too. Both `LifecycleContainerPublisher`
and `LifecycleCheckpointRefPublisher` each independently tracked their
own `_current` expected `LifecycleProjection`. This was safe only as
long as each was ever used alone against a lease. The moment more than
one of them, or a direct `writer.advance_lifecycle_state()` call, ever
touched the same lease — the exact shape any future entry-point wiring
would need — it was not merely a latent risk: `LifecycleProjection`
equality is whole-object, so *any* successful write through *any* path
invalidates every other independently-held `_current` snapshot for its
very next write, which then fails with `STALE_EXPECTED_PROJECTION` on
its very next use, not eventually. Independently reproduced and fixed
in this slice, before any entry-point wiring was ever attempted.

Separately, `LifecycleCheckpointRefPublisher.publish()` let a raw
`LifecycleStoreError` escape uncaught. `RunController`'s two checkpoint
call sites (`_dispatch_apply_patch`'s `establish()`/`advance()` calls)
caught only `CheckpointRefError`/`CheckpointSessionError` (this
amendment's own predecessor, Amendment 4, explicitly recorded that as
the controller's exact catch set) — a raw `LifecycleStoreError` would
have propagated straight out of `RunController.run()` itself (which has
no enclosing try/except), skipping `_fail()`, the domain transition,
`_terminate()`, evidence capture, worktree disposal, and checkpoint-ref
deletion entirely. An outer wrapper catching it after the fact cannot
repair any of that.

This amendment revises Amendment 4's stated exception-identity contract
at exactly these two named call sites — nowhere else — and closes the
staleness gap. No entry-point wiring, no `RunController` construction
change beyond its exception-catching surface, and no worktree-tracking
work are part of this slice.

### 1. `LifecycleProjectionCursor` — the one authoritative cursor

```python
class LifecycleProjectionCursor:
    def __init__(self, writer: _LifecycleProjectionWriter, initial: LifecycleProjection) -> None: ...
    @property
    def current(self) -> LifecycleProjection: ...
    def advance_state(self, state: LifecycleState) -> LifecycleProjection: ...
    def record_container(self, *, role: str, intent: ContainerIntent, id: str | None) -> LifecycleProjection: ...
    def record_checkpoint_ref(self, transition: CheckpointTransition) -> LifecycleProjection: ...
    def refresh(self) -> LifecycleProjection: ...
```

Each write method delegates to the writer with `expected=self._current`
and updates `self._current` only after a confirmed successful write —
exactly what each adapter did individually before, but now there is
only one such value per lease, shared by every facade constructed
against it. Raises the writer's own raw `LifecycleStoreError`
unchanged — translation to a facade-specific public exception remains
each facade's own responsibility. Never retries or refreshes
automatically; `refresh()` re-syncs every facade sharing the cursor in
one call.

### 2. Coordinated construction: `SharedLifecyclePublishers` and its factory

```python
@dataclass(frozen=True)
class SharedLifecyclePublishers:
    cursor: LifecycleProjectionCursor
    checkpoint_ref_publisher: LifecycleCheckpointRefPublisher
    container_publisher: LifecycleContainerPublisher

def create_shared_lifecycle_publishers(
    writer: _LifecycleProjectionWriter, initial: LifecycleProjection
) -> SharedLifecyclePublishers: ...
```

A named bundle, not a positional tuple, so a call site can never
confuse which field is which. Both `LifecycleCheckpointRefPublisher`
and `LifecycleContainerPublisher` gain a private `_from_cursor`
classmethod — the only way to construct an instance that shares a
cursor with another facade; not part of either class's public
interface. Their existing public `__init__(writer, initial_projection)`
signatures are unchanged and remain source-compatible for isolated use
— each such instance privately owns its own cursor, never shared with
anything else. Both classes' docstrings now explicitly warn against
combining independently constructed instances against one writer;
`create_shared_lifecycle_publishers()` is the only sanctioned path for
any scenario touching more than one facade against one lease. A
structural test proves a facade built via `_from_cursor` holds nothing
but `{"_cursor"}` — no shadow `_current`/`_writer` state that could
silently drift from the shared one.

### 3. Checkpoint lifecycle-publication error boundary

`checkpoint_session.py` (dependency-light — never imports
`lifecycle_store`) gains, beside `CheckpointTransitionPublisher`:

```python
class CheckpointPublicationFailure(str, Enum): ...   # mirrors container_lifecycle.ContainerPublicationFailure exactly
class CheckpointPublicationError(Exception):
    def __init__(self, reason: CheckpointPublicationFailure, message: str) -> None:
        ...
        self.pre_recovery_cause: BaseException | None = None
```

`LifecycleCheckpointRefPublisher.publish()` now catches
`LifecycleStoreError` and translates it (`raise CheckpointPublicationError(...)
from exc`), via an exhaustive, subscript-access map
(`_CHECKPOINT_PUBLICATION_FAILURE_MAP`, `tests/unit/test_lifecycle_store.py`
asserts its keys equal the complete `LifecycleStoreFailure` enum) —
mirroring `LifecycleContainerPublisher`'s own Amendment 6 inversion
pattern exactly. This is the one place a `LifecycleStoreError` is ever
caught for the checkpoint-ref publisher path.

`RunController`'s two checkpoint call sites now catch
`(CheckpointRefError, CheckpointSessionError, CheckpointPublicationError)`;
`_map_checkpoint_error` gains one `elif isinstance(exc,
CheckpointPublicationError)` branch mapping to the new
`ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED` (`ErrorDomain.LIFECYCLE`)
— placed before the generic `CheckpointSessionError`-or-else fallback,
so it is never conflated with a programming-invariant violation. The
name is deliberately not `CHECKPOINT_PUBLICATION_FAILED`: it must read
unambiguously as "the durable lifecycle-projection record of this
checkpoint-ref transition failed to publish," never as "the Git
checkpoint-ref mutation itself failed" (every existing `CHECKPOINT_REF_*`
code already means that). The finer-grained
`CheckpointPublicationError.reason` is deliberately not persisted in
the `OperationalError` — it stays available in-process for
logging/debugging without widening this stable, serialized taxonomy,
mirroring the same choice Amendment 6 already made for containers.

`session.delete()`'s own failures are unaffected: they still sit under
`RunController._terminate()`'s existing broad `except Exception` — any
delete-time `CheckpointPublicationError` still folds into the existing
`LIFECYCLE_CLEANUP_UNCONFIRMED` path unchanged. This slice adds no
second, conflicting mapping for delete-time publication failure.

**Scope boundary, explicit**: this amendment revises only
`LifecycleCheckpointRefPublisher`'s prior raw-`LifecycleStoreError`-
propagation behavior and the two named `RunController` catch sites. A
caller-supplied, non-lifecycle-store `CheckpointTransitionPublisher`
implementation's own exception type still propagates through
`checkpoint_session.py` exactly as before — untouched by this
amendment.

### 4. Dual-cause preservation during confirmed-`UNCHANGED` recovery

`establish()`/`advance()`'s existing recovery flow
(`raise publish_exc from exc`, Slice 3B-3's own "projection-consistency
failure dominance") reuses the *same* exception object across two
`raise ... from ...` statements when the recovery-collapse publish
itself raises `CheckpointPublicationError`. Python's exception chaining
is single-linked: this silently overwrote `publish_exc.__cause__`
(ordinarily the adapter's own `LifecycleStoreError`) with `exc` (the
confirmed-`UNCHANGED` `CheckpointRefError`), demoting the original cause
to `__context__` — which `raise ... from ...` also sets
`__suppress_context__=True` for, hiding it from every standard
traceback/log renderer. Independently reproduced with a minimal
reduction (`raise b from a; ...; raise b from c` — confirmed `b.__cause__
== c`, `b.__context__ == a`, `b.__suppress_context__ is True`) before
any fix was written.

**Contract, exact, per phase** (verified against every call site in
`establish()`/`advance()`/`delete()`):

| Phase | `__cause__` | `pre_recovery_cause` |
|---|---|---|
| Pre-mutation publish failure (no Git call attempted yet) | the adapter's own `LifecycleStoreError` | `None` |
| Post-mutation collapse-publish failure (Git mutation already succeeded) | the adapter's own `LifecycleStoreError` | `None` |
| Confirmed-`UNCHANGED` Git failure, then recovery-collapse publish failure | the `CheckpointRefError` that triggered recovery (Slice 3B-3's own rule, unchanged) | the `CheckpointPublicationError`'s own prior `__cause__` (ordinarily a `LifecycleStoreError`), preserved instead of silently lost |
| `delete()`'s transitional-publish failure | the adapter's own `LifecycleStoreError` | `None` |
| `delete()`'s Git failure after a successful transitional publish | N/A — a plain `CheckpointRefError`, no `CheckpointPublicationError` involved at all | N/A |

Only the third row required a change:

```python
except Exception as publish_exc:
    if isinstance(publish_exc, CheckpointPublicationError):
        publish_exc.pre_recovery_cause = publish_exc.__cause__
    raise publish_exc from exc
```

`CheckpointPublicationError.reason` always remains the categorical
reason the publish call itself failed, in every phase. A generic
(non-`CheckpointPublicationError`) recovery-publish exception from an
alternate `CheckpointTransitionPublisher` implementation is completely
untouched by this `isinstance` guard — existing `_Boom`-based recovery
tests in `tests/unit/test_checkpoint_session.py` continue to pass
unmodified, proving it.

Neither an `ExceptionGroup` nor any other multi-cause representation
was used: nothing in this repository's existing exception-handling
precedent, and no `RunController` catch site, justifies one, and a
linear `__cause__` chain already had a well-defined, deliberate meaning
(Slice 3B-3's own dominance rule) this amendment preserves rather than
redesigns.

### Milestone boundary

This slice is exactly: `LifecycleProjectionCursor`,
`SharedLifecyclePublishers`/`create_shared_lifecycle_publishers`,
`CheckpointPublicationFailure`/`CheckpointPublicationError`, the two
`RunController` catch-site/mapping changes, and
`ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED`. Explicitly **not**
part of this slice, and not accepted APIs of this amendment: any
`prepare_lifecycle()` invocation from production orchestration; any
lifecycle entry-point/composition-root construction; a `CLEANING`-
publication hook or any other new `RunController` constructor
parameter beyond the exception types it now catches; any lifecycle
state change during a real controller run; any worktree-tracking
schema or allocation change; CLI/model/UI integration; abandonment or
signals; and any Docker, SIGKILL, or cross-process test. `docs/
threat-model.md`'s T-E1 entry is completely unaffected — this slice
adds zero entry-point behavior.

A `CLEANING`-publication hook and a future composition root both
depend on this slice's fixes: a hook publishing `CLEANING` alongside a
container/checkpoint write built on the pre-fix adapters would
reproduce the staleness collision the first time a real run touched
both in one `_terminate()` pass, and a checkpoint-publication failure
surfacing during that hook's own call would have escaped uncaught
exactly as this amendment's own investigation demonstrated. Both are
sequencing consequences of this slice, not APIs it accepts.

### Evidence

`src/codeagent/lifecycle_store.py`, `src/codeagent/checkpoint_session.py`,
`src/codeagent/controller.py`, `src/codeagent/errors.py`, and
`src/codeagent/events.py` implement every rule above. Verified locally
(macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`): the directly
affected files (`tests/unit/test_lifecycle_store.py`,
`tests/unit/test_checkpoint_session.py`, `tests/unit/test_errors.py`,
`tests/unit/test_events.py`, `tests/integration/test_controller.py`)
collected and passed together, 1,099 passed, in both forward and
reverse file order; the thirteen-file focused set established since
Slice 3B-6, 1,036 passed; the complete suite, 2,648 passed, 0 skipped,
identical with and without `CODEAGENT_REQUIRE_DOCKER=1`; `git diff
--check` clean. See `ENGINEERING_LOG.md`'s dated entry for the complete
detail, including the real-lease/real-writer interleaving test proving
`PREPARING→ACTIVE`, both container roles through confirmed absence, and
`checkpoint ABSENT→CREATING→PRESENT→ADVANCING→PRESENT→REMOVING→ABSENT`
all succeed through the shared bundle with zero spurious staleness,
genuine external staleness still refused, and explicit `refresh()`
correctly re-syncing both facades after a fault-injected durability-
unconfirmed publication.

**Confirmed on GitHub-hosted Linux CI** (commit
`6a452b1d095584d754cbc51f291d1ebd5de9f3c9`, run
[36350258312](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36350258312),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the mandatory
Docker preflight passed and the pinned verification image was pulled
and confirmed `linux/amd64`. The dedicated "Run real Docker
verification tests" step ran only the legacy
`tests/integration/test_slice_c.py` (3 passed in 2.63s) — it does not
specifically exercise Slice 3B-7, which adds no real-Docker test of its
own. The separate "Run complete test suite" step (with
`CODEAGENT_REQUIRE_DOCKER=1` confirmed present) reported `2645 passed,
3 skipped in 47.16s` (2645 + 3 = the local collected total of 2,648);
that step used `pytest -q`, which prints no test identities, so the 3
skips are recorded only as unidentified platform/host-specific skips,
never guessed at, and are not evidence of Docker unavailability, since
Docker was required and confirmed available for that step. The final
leftover-container check (the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'` over a complete `docker
ps -a` listing) returned empty and succeeded. This is implementation/
automated-test evidence only — it does not constitute or substitute for
a security review — and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
specifically, not a general Linux or ARM64 claim. It adds no
entry-point wiring, no lifecycle state change during a real controller
run, no `CLEANING` integration, no worktree tracking, and no CLI
behavior; `docs/threat-model.md`'s T-E1 entry is unchanged.

## Amendment 8 (Accepted 2026-09-28): Milestone 3 Slice 3C-1 — dependency-light owner-state lifecycle-publication boundary and `RunController` terminal hook

Implementation status: **implemented**, locally validated on macOS
(2026-09-28) with a real Docker daemon. Slices 3B-6 and 3B-7 gave
containers and the checkpoint ref durable write-ahead publishers, but
nothing published the run's own owner-level `PREPARING -> ACTIVE ->
CLEANING -> COMPLETE` state (Amendment 3 section 5) during a real
controller run, and no `RunController` collaborator existed to call it.
This amendment adds exactly that boundary — entirely opt-in, mirroring
`checkpoint_session.CheckpointTransitionPublisher`/`container_lifecycle.
ContainerTransitionPublisher`'s existing dependency-inversion pattern —
and nothing else. **No `prepare_lifecycle()` composition helper, no
lifecycle-aware application entry point, and no lease-ownership/
closure decision are part of this slice** — see "Explicitly deferred"
below.

### 1. Dependency-light boundary: `lifecycle_owner.py`

A new stdlib-only leaf module, `src/codeagent/lifecycle_owner.py` — no
import of `lifecycle_store.py` or `controller.py`, matching
`container_lifecycle.py`'s own precedent — defines
`OwnerStatePublicationFailure` (exactly the same 10 members as
`ContainerPublicationFailure`/`CheckpointPublicationFailure`:
`NOT_INSTALLED`, `DURABILITY_UNCONFIRMED`, `CLEANUP_UNCONFIRMED`,
`STALE_EXPECTATION`, `WRONG_LOCK_SCOPE`, `ILLEGAL_TRANSITION`,
`SUBSTRATE_UNAVAILABLE`, `SCHEMA_INVALID`, `OVERSIZED`, `UNCLASSIFIED`)
and `OwnerStatePublicationError(reason, message)`.

`controller.py` gains a local `LifecycleOwnerPublisher` Protocol
(`activate()`, `begin_cleanup()`, `complete()`) — placed beside its
existing `Workspace`/`CheckpointSessionLike` Protocols, following this
module's established habit of declaring narrow Protocols for its own
collaborators — and imports only `OwnerStatePublicationError` from the
new leaf module. `RunController.__init__` gains one new keyword-only,
defaulted parameter, `lifecycle_owner: LifecycleOwnerPublisher | None =
None`; every existing caller is source- and behavior-compatible with no
change.

`lifecycle_store.py` gains the one concrete implementation,
`LifecycleOwnerStatePublisher`, wrapping the shared
`LifecycleProjectionCursor` exactly like the two existing adapters
(`_from_cursor` classmethod, `current` property, explicit
never-automatic `refresh()`). Its three semantic methods hide
`LifecycleState` entirely — `activate()`/`begin_cleanup()`/`complete()`
request `ACTIVE`/`CLEANING`/`COMPLETE` respectively via
`cursor.advance_state()`, so `RunController` never needs to import or
reference `LifecycleState` at all. `complete()` needs no separate
pre-check of `is_projection_fully_absent_shape()`: it simply requests
`COMPLETE` and lets the writer's own existing `CLEANING -> COMPLETE`
clean-final gate (Amendment 3 section 5, unchanged) raise
`ILLEGAL_TRANSITION` naturally when the shape isn't actually absent,
translated like any other failure. An exhaustive, subscript-access
`_OWNER_STATE_PUBLICATION_FAILURE_MAP` (identical reachability analysis
to the two existing maps — `LIFECYCLE_ID_COLLISION`/
`RECONCILIATION_BLOCKED` are `prepare_lifecycle()`-only and unreachable
from `advance_lifecycle_state()` too) translates every
`LifecycleStoreError`; `tests/unit/test_lifecycle_store.py` asserts its
keys equal the complete `LifecycleStoreFailure` enum, and a second test
asserts `UNCLASSIFIED` is used for exactly those two reasons and no
others.

`SharedLifecyclePublishers` gains a third field, `owner_publisher:
LifecycleOwnerStatePublisher`; `create_shared_lifecycle_publishers()`
constructs it via `_from_cursor(cursor)` alongside the other two — so a
future composition slice combining all three against one lease can
never reproduce the independent-cursor staleness bug Amendment 7 fixed.

### 2. Exact `RunController` sequence

1. `run()` emits `RunStarted`, transitions via `RUN_STARTED`.
2. If `lifecycle_owner` is set, `activate()` is attempted immediately —
   before any baseline, model, or tool work begins. No controller-driven
   baseline, Docker verification, patch, or checkpoint-ref mutation has
   begun yet at this point — but a physical workspace (the entered
   `GitWorktree` a future composition layer constructs before
   `RunController` itself) may already exist, since `RunController` is
   always handed an already-entered `Workspace`, never one it enters
   itself. This is precisely why routing a failure here through the
   ordinary `_terminate()` sequence is safe rather than skipping it:
   `_terminate()` still disposes or preserves that workspace and invokes
   the checkpoint session's cleanup — the same early-abort pattern
   already used for a failed initial `READ_FILE`.
3. A confirmed `activate()` success sets `self._lifecycle_owner_active
   = True`; normal run work proceeds only from here.
4. A confirmed `activate()` failure maps to a new
   `OperationalError(ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED,
   ...)` (fixed, sanitized message; see section 4) and enters
   `_terminate(0, UNRECOVERABLE_ERROR, error=...)` directly.
5. Inside `_terminate()`: `begin_cleanup()` is attempted **first**,
   before evidence capture, but **only when
   `self._lifecycle_owner_active` is true** — `PREPARING -> CLEANING`
   is not a legal owner-state edge (Amendment 3 section 5's table is
   unchanged), so a run whose `activate()` never succeeded never
   attempts `begin_cleanup()`/`complete()` at all. Its failure is
   recorded (`lifecycle_publication_unconfirmed = True`) but never
   skips evidence capture or the existing worktree-dispose-or-preserve/
   checkpoint-delete sequence, which always run regardless — mirroring
   this method's existing "never skip real cleanup because a bookkeeping
   write failed" convention for
   `self._any_verifier_cleanup_unconfirmed`.
6. `complete()` is attempted once `begin_cleanup()` succeeded and every
   owned-resource cleanup step (verifier/worktree/ref) is confirmed —
   **deliberately independent of `evidence_receipt.success`** (section
   3). Its failure also sets `lifecycle_publication_unconfirmed`.
7. `cleanup_unconfirmed` is computed once, at the very end, as the
   logical OR of the existing verifier/worktree/ref flags and
   `lifecycle_publication_unconfirmed` — a single, unchanged precedence
   tier, exactly as the existing `LIFECYCLE_CLEANUP_UNCONFIRMED` branch
   already worked, now with a wider set of inputs. The existing `elif
   not evidence_receipt.success` branch (evidence-code precedence) is
   completely unchanged in structure.
8. No automatic retry or `refresh()` call is ever made by
   `RunController` — `LifecycleOwnerPublisher`'s Protocol has no
   `refresh()` method at all, so this is structurally, not just
   behaviorally, true.

### 3. `COMPLETE` is independent of evidence-capture success

`lifecycle.json` is an operational resource-recovery projection, not
the audit/evidence record (`EvidenceSink`/`events.jsonl`). ADR 0003
already treats evidence-capture outcome as independent of resource-
teardown confirmation. `complete()` is therefore attempted whenever
`begin_cleanup()` succeeded and all owned-resource cleanup is
confirmed — **regardless of whether evidence capture itself
succeeded**. A run can legitimately reach `RunFinished
(UNRECOVERABLE_ERROR, error=EVIDENCE_*)` while its lifecycle projection
durably reaches `COMPLETE`: no recoverable owned resource remains, so
the lifecycle projection's own job is genuinely done, even though the
separate audit record failed to capture. If `complete()` **also**
fails (a combined failure), `LIFECYCLE_CLEANUP_UNCONFIRMED` dominates
the evidence error under the existing, unchanged precedence — the
evidence failure is not lost, merely outranked. A dedicated,
load-bearing test
(`test_evidence_failure_and_complete_failure_together_are_dominated_by_lifecycle_cleanup_unconfirmed`,
`tests/integration/test_controller.py`) proves this exact dominance,
alongside a separate test proving evidence failure alone never blocks
a successful `complete()`
(`test_evidence_failure_does_not_block_complete`).

### 4. New `ErrorCode`

`ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED =
"lifecycle_state_publication_failed"` (`ErrorDomain.LIFECYCLE`) — used
**only** for a confirmed `activate()` failure at run start, before any
baseline/model/tool work began. Deliberately distinct from the
existing `LIFECYCLE_CLEANUP_UNCONFIRMED` (which now also covers a
terminal-path `begin_cleanup()`/`complete()` publication failure, in
addition to its existing verifier/worktree/ref cleanup meaning — no
second new code was introduced for the terminal-path case). No
`events.py` schema change was required or made: `RunFinished.
_UNRECOVERABLE_ERROR_CODES` is computed dynamically as `set(ErrorCode)
- {POLICY_VIOLATION_SEVERE}`, so the new code is automatically legal.
`tests/unit/test_errors.py` pins its value and domain the same way
every other `ErrorDomain.LIFECYCLE` code already is.

### 5. Durability-unconfirmed: installed-on-disk vs. cursor belief

A `PROJECTION_DURABILITY_UNCONFIRMED` result means `os.replace` already
succeeded — the new target projection is the one currently installed
on disk; only the trailing directory-`fsync` confirmation failed. The
in-memory `LifecycleProjectionCursor._current` does **not** advance in
this case (Amendment 7 section 1: "updates `self._current` only after
a confirmed successful write"). This produces a genuine split: after
an `activate()`-durability-unconfirmed call the cursor stays at
`PREPARING` while the *installed* projection is actually `ACTIVE`;
after `begin_cleanup()` the cursor stays `ACTIVE` while installed is
`CLEANING`; after `complete()` the cursor stays `CLEANING` while
installed is genuinely `COMPLETE`. In every case `RunController`
reports the call as failed (folds into `cleanup_unconfirmed`) and never
calls `refresh()` or retries. A later reconciliation pass reading the
*installed* file after a `complete()`-durability-unconfirmed run
correctly sees `COMPLETE` and returns `SKIPPED_TERMINAL` — this is not
a bug: the run was right to be conservative at the moment it couldn't
confirm the write, and the write turned out fine. All three phases are
proven independently, with real fault injection for each — not merely
the `activate()` phase generalized by assertion:
`test_owner_state_publisher_activate_durability_unconfirmed_installed_vs_cursor_split`,
`test_owner_state_publisher_begin_cleanup_durability_unconfirmed_installed_vs_cursor_split`,
and
`test_owner_state_publisher_complete_durability_unconfirmed_installed_vs_cursor_split`
(all in `tests/unit/test_lifecycle_store.py`) each drive the real
writer through the real preceding transitions first, then fault-inject
the trailing directory-`fsync` on the phase under test, and prove the
split via `refresh()`; the `complete()` variant additionally asserts the
installed projection genuinely satisfies
`is_projection_fully_absent_shape()`. A new, independent reconciliation-
level test,
`tests/unit/test_reconciliation.py::test_complete_absent_shape_is_skipped_with_zero_calls`,
separately proves a real reconciliation pass classifies a seeded
`COMPLETE`-plus-absent-shape entry as `SKIPPED_TERMINAL` with zero
lock/inspection calls — the same code path `RECONCILED` already
exercised, now proven for `COMPLETE` specifically too.

### 6. Explicitly deferred (Slice 3C-2 and later)

No `prepare_lifecycle()` call from production orchestration, no
`RunController` composition/entry-point wiring, no `GitWorktree`/
`DockerVerifier`/`CheckpointSession`/`patch.py`/`reader.py`/CLI change,
and — most consequentially — **no decision about `LifecycleLease`
ownership or closure timing**. This slice never constructs or closes a
lease and has no standing to settle that question. In particular:

- **Lease-close timing versus `RunFinished` finality remains an open
  question**, not resolved here. This slice's own guarantee is limited
  to: owner-state `COMPLETE` (or the correct non-`COMPLETE` outcome) is
  published *inside* `_terminate()`, strictly before `_finish()`
  appends `RunFinished` — a fact this slice's code structurally
  guarantees, and which keeps I13 ("the lifecycle lock is released only
  after the final projection write") satisfiable by whichever future
  slice actually owns the lease. This amendment does **not** claim
  lease release is "outside I15" or "orthogonal to run success" as a
  settled design position — that framing is explicitly left for Slice
  3C-2, once it defines who owns the lease and what a lease-close
  failure does to the outer operation's result.
- A previously-sketched `LifecycleAwareRunResult` return shape is
  recorded only as one candidate for Slice 3C-2 to evaluate, not an
  accepted API.
- `docs/threat-model.md`'s T-E1 entry is **unchanged and remains
  unmitigated by this slice**: nothing here calls `prepare_lifecycle()`
  or constructs a real entry point; this slice only gives a future
  composition layer a collaborator to wire in.

### Milestone boundary

This slice is exactly: `lifecycle_owner.py` (new), the
`LifecycleOwnerStatePublisher` adapter and exhaustive map in
`lifecycle_store.py`, the `SharedLifecyclePublishers` field, and
`controller.py`'s new Protocol/parameter/`run()`/`_terminate()` logic,
plus the new `ErrorCode`. Nothing else changed.

### Evidence

Verified locally (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`):
the directly affected files (`test_errors.py`, `test_lifecycle_store.py`,
`test_reconciliation.py`, `test_controller.py`) collected and passed
together, 500 passed, in both forward and reverse file order; the
established focused Milestone-3 set (sixteen files) collected and
passed together, 1,843 passed, in both forward and reverse file order;
the complete suite, 2,690 passed, 0 skipped; `git diff --check` clean;
no leftover `codeagent-*` containers, extra worktrees, `refs/codeagent`
refs, or temp state roots afterward; a real default-macOS-location
check (`~/Library/Application Support/CodeAgent`) confirmed no state
root was ever created there — the slice's own real-writer tests
redirect `prepare_lifecycle()` to an isolated per-test `tmp_path` state
root via `CODEAGENT_STATE_DIR`, never the default location. This is
implementation/automated-test evidence only — it does not constitute or
substitute for a security review.

**Confirmed on GitHub-hosted Linux CI** (commit
`3476b3d7a50e2f979059031ab9c670949a7c20fe`, run
[36483533854](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36483533854),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the
mandatory Docker preflight passed; the pinned verification image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v` (`3 passed in 2.33s`) — this
file's own legacy Milestone-1 tests, exactly; it does **not**
specifically exercise Slice 3C-1, which adds no real-Docker test of its
own. The separate "Run complete test suite" step (`python -m pytest
-q`, with `CODEAGENT_REQUIRE_DOCKER=1` confirmed present in that step's
own logged environment) reported `2687 passed, 3 skipped in 47.83s`;
`2687 + 3` equals the local collected total of 2,690 — the
skipped-test identities are not printed by the `pytest -q` log, and are
not guessed here. These skips are not evidence of Docker unavailability,
since the daemon, the pulled image, and `CODEAGENT_REQUIRE_DOCKER=1`
were all already confirmed for that same step. The final
leftover-container check used the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'` pattern over a
complete, unfiltered `docker ps -a` listing; its captured output was
empty and the step succeeded. This is implementation/automated-test
evidence only — it does not constitute or substitute for a security
review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
specifically, not a general Linux or ARM64 portability claim. It does
not claim `prepare_lifecycle()`, `LifecycleLease` ownership/closure, CLI
wiring, or any production entry-point composition exists — none of that
is part of this slice, unchanged from the "Explicitly deferred" section
above.

## Amendment 9 (Accepted 2026-10-02): Milestone 3 Slice 3C-3 — `RunController` ordinary-exception terminalization boundary

### Context

Slice 3C-2 closed the deterministic-worktree-placement gap but left an
independently-discovered correctness gap unresolved, flagged explicitly
in that slice's own review for 3C-3's planning: `RunController.run()`
has no top-level exception boundary. An unexpected, ordinary `Exception`
from any collaborator (model, approval provider, verifier, patch
applier, reader, or a lifecycle publisher) propagates straight out of
`run()`, never reaching `_terminate()` — skipping evidence capture,
worktree disposal, checkpoint-ref deletion, and owner-state
`begin_cleanup()`/`complete()` entirely. This is a real, present gap in
already-shipped code, independent of the still-undecided worktree
transition table or `LifecycleLease` ownership/close-timing questions
(both remain open — see Amendment 8's own unresolved items, unchanged
by this amendment).

### Decision

An ordinary `Exception` that escapes normal run execution, after
`RunStarted` is emitted and `RUN_STARTED` is recorded, is routed exactly
once through `_terminate()`, producing
`RunFinished(terminal_reason=UNRECOVERABLE_ERROR,
error.code=ErrorCode.UNCLASSIFIED_FAILURE)` with one fixed, sanitized
message: `"the run terminated due to an unanticipated internal error"`.
No new `ErrorCode` is introduced — `UNCLASSIFIED_FAILURE`'s own existing
contract (`errors.py`) already permits exactly this use: "Permitted only
as `RunFinished.error` when `terminal_reason` is `UNRECOVERABLE_ERROR`
... there is no legitimate case for using this code anywhere but the
run's final, single terminal report."

**Deliberately narrow — explicitly NOT covered by this amendment:**
- Any `BaseException` that is not an `Exception`
  (`KeyboardInterrupt`/`SystemExit`/`GeneratorExit`) — these propagate
  untouched by construction, since `except Exception:` cannot intercept
  them; no special-case code exists or is added.
- Signal-driven cancellation — remains entirely ADR 0005's domain
  (accepted, unimplemented). This amendment makes no cancellation-safety
  claim and does not touch ADR 0005.
- A failure during the initial `RunStarted` emission or `RUN_STARTED`
  transition, which remain outside the guarded region — the event
  contract does not establish whether terminalization is even valid
  before `RunStarted` exists in the log, and this amendment does not
  invent an answer.
- A failure arising inside `_terminate()` itself. `_terminate()` gains a
  genuine one-shot guard — a check-and-raise, not a bare flag write —
  as its first two lines:
  ```python
  if self._termination_started:
      raise RuntimeError("run termination has already started")
  self._termination_started = True
  ```
  If an exception escapes an already-started `_terminate()`, it
  propagates unchanged; `_terminate()` is never invoked a second time,
  and no cleanup, lifecycle publication, transition, or event emission
  is ever duplicated.

### Mechanism (why no `__context__`/`__cause__` leak)

`run()`'s fallback `_terminate()` call is placed strictly *after* the
`except Exception:` block exits, never inside it, and the caught
exception is never bound to a name:

```python
try:
    return self._run_after_start()
except Exception:
    if self._termination_started:
        raise
    # falls through; the exception is retained nowhere

return self._terminate(
    self._pass_index,
    domain.Trigger.UNRECOVERABLE_ERROR,
    error=OperationalError(
        code=ErrorCode.UNCLASSIFIED_FAILURE,
        error_id=f"{c.run_id}-unclassified-failure",
        message="the run terminated due to an unanticipated internal error",
    ),
)
```

Once Python exits an `except` block without re-raising, the "currently
being handled" exception state is cleared. Because the fallback
`_terminate()` call sits outside that block, a failure inside it starts
with a clean `__context__`/`__cause__` — never implicitly chained to the
discarded, uncontrolled collaborator exception. This was verified
directly, not merely asserted: a real end-to-end regression (an
unexpected collaborator exception combined with a second, independent
teardown failure) confirms the escaping exception's `__context__` and
`__cause__` are both `None`, and that the original collaborator
exception's text/type appears nowhere in it or in any emitted event.

No type, message, repr, traceback, marker, or other uncontrolled detail
derived from the caught exception is ever persisted, emitted, logged,
interpolated, or chained by this mechanism. This is narrower than a
claim that nothing from the run reaches persisted events: ordinary
events emitted *before* the exception occurred (e.g. `RunStarted`,
`BaselineRecorded`, a prior `ToolCompleted`) still carry their normal,
schema-approved run/plan/tool data exactly as they always have — this
amendment sanitizes only the caught exception itself, not the run's
otherwise-ordinary event trace.

### What is unaffected

Every existing recognized-failure path is preserved exactly: checkpoint-
ref/lifecycle-publication error mappings, patch/read/verifier typed
`OperationalError` returns, evidence-capture and cleanup-unconfirmed
precedence inside `_terminate()`, and every normal
REJECTED/REVISION_REQUESTED/budget-exceeded/PASSED/TEST_FAILURE outcome.
This amendment adds exactly one new catch-all at exactly one point; it
changes no existing `_terminate()` ordering or precedence rule.

### Explicitly out of scope

`prepare_lifecycle()` production wiring; any composition root; the
worktree transition/combination table (or a decision to omit worktree
attribution); `LifecycleLease` ownership/close-timing; CLI/UI; signal
handling/cancellation; Docker behavior changes; reconciliation changes;
abandonment; any `ErrorCode`/`ErrorDomain`/events-schema change beyond
reusing the existing `UNCLASSIFIED_FAILURE`. `docs/threat-model.md` is
unchanged — this is a controller-internal correctness fix, not a new
concurrent-run or lifecycle-attribution mitigation; it does not alter
T-E1's status.

### Evidence

Verified locally (macOS, real Docker daemon,
`CODEAGENT_REQUIRE_DOCKER=1`): the directly affected files
(`src/codeagent/controller.py`, `tests/integration/test_controller.py`)
compile cleanly; `test_controller.py` alone, 60 passed; the directly
affected set (`test_controller.py` + `test_errors.py` + `test_events.py`)
collected and passed together, 806 passed, in both forward and reverse
file order; the established fifteen-file focused Milestone-3 set
(the fourteen-file set established since Slice 3A-1, plus
`test_controller.py`) collected and passed together, 1,220 passed, in
both forward and reverse file order; the complete suite, 2,744 passed,
0 skipped, with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check` clean;
no leftover `codeagent-*` containers, extra worktrees,
`refs/codeagent` refs, lingering processes, or temp/default state
roots afterward.

One pre-existing test required a correction, not a weakening:
`test_fake_model_gate_actually_fails_without_the_marker` had asserted
that `MarkerGatedFakeModel`'s internal `AssertionError` (itself an
ordinary `Exception`) propagated out of `run()` raw — true only because
no boundary previously existed. It now asserts the new, correct
behavior: the same `AssertionError` is caught by this amendment's
boundary and surfaces as a terminalized
`UNCLASSIFIED_FAILURE`/`UNRECOVERABLE_ERROR` `RunFinished`, exactly like
any other unanticipated collaborator exception. The gate still
provably fires; it no longer escapes raw, which is the amendment's own
intended effect, not an accommodation of a regression.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review.

### Correction pass (2026-10-02)

A targeted review found four gaps between the acceptance criteria above
and what this amendment's first pass actually tested or documented, all
closed with no change to the production control flow described above:

1. No test had proved the fallback path drives lifecycle-owner cleanup
   (`begin_cleanup()`/`complete()`) to completion when activation had
   already succeeded — only the activation-*failure* case (where those
   two calls are correctly never attempted) was covered. Added a test
   asserting `owner.calls == ["activate", "begin_cleanup", "complete"]`
   for a fallback triggered after confirmed activation.
2. No precedence test exercised the new fallback path specifically —
   every pre-existing evidence-failure/cleanup-unconfirmed precedence
   test triggers through one of the controller's own typed-result
   returns, never through the new `except Exception:` catch-all. Added
   two: an evidence-capture failure and a workspace-disposal failure,
   each triggered by an unexpected collaborator exception, each
   asserting the existing, unchanged precedence rule still overrides
   `UNCLASSIFIED_FAILURE` correctly (`EVIDENCE_CAPTURE_FAILED` and
   `LIFECYCLE_CLEANUP_UNCONFIRMED` respectively, with their exact
   existing sanitized messages, and neither injected exception's text
   leaking anywhere). The previously-unused permissive
   `allow_override_codes` test-helper parameter is removed in favor of
   these two tests' fully explicit, exact assertions.
3. The fallback teardown-chaining regression had inferred "`_terminate()`
   entered exactly once" from side effects rather than measuring it
   directly, never checked escaping-instance identity, and only
   scanned the escaped exception's own `str()` for the discarded
   collaborator exception's text — not every already-emitted event.
   Strengthened with a direct call-count spy, an identity assertion
   against the exact sentinel instance raised, and a scan of every
   emitted event's `repr()`.
4. This section's own "no persistence... of collaborator input"
   wording (above) was an overclaim, corrected in place: it does not
   and cannot mean the run's otherwise-ordinary events (emitted before
   the unexpected exception) stop carrying their normal, schema-
   approved data — only that nothing *derived from the caught
   exception itself* is ever persisted, emitted, logged, interpolated,
   or chained.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`):
`test_controller.py` alone, 63 passed (up from 60); the directly
affected set, 809 passed (up from 806), forward and reverse file order;
the fifteen-file focused Milestone-3 set, 1,223 passed (up from 1,220),
forward and reverse file order; the complete suite, 2,747 passed (up
from 2,744), 0 skipped, with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff
--check` clean; no leftover `codeagent-*` containers, extra worktrees,
`refs/codeagent` refs, lingering processes, or temp/default state
roots afterward. No scope change from the amendment as originally
accepted above. This is implementation/automated-test evidence only.

**Confirmed on GitHub-hosted Linux CI** (commit
`bd90a992a001a2f9e9aed42ea65138802065f5cb`, run
[37052793645](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37052793645),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success`, 1 job, 13
steps, every step `success`): the mandatory Docker preflight step
succeeded (Docker Engine - Community, version `28.0.4`); the pinned
verification image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v`
(`CODEAGENT_REQUIRE_DOCKER=1` confirmed present in that step's own
logged environment): `3 passed in 2.37s`, 0 skipped — this is this
file's own legacy Milestone-1 suite, exactly, and is **not** specific
evidence for Slice 3C-3, which adds no real-Docker test of its own. The
separate "Run complete test suite" step (`python -m pytest -q`,
`CODEAGENT_REQUIRE_DOCKER=1` also confirmed present in that step's own
logged environment) reported `2744 passed, 3 skipped in 49.20s`;
`2744 + 3` equals the local collected total of 2,747 reported just
above. The run used `pytest -q`, which prints no test identities, so
these 3 skips are **not identified from this run's own log** and are
not guessed here — they are not evidence of Docker unavailability,
since the daemon, the pulled image, and `CODEAGENT_REQUIRE_DOCKER=1`
were all already confirmed for that same step. Source inspection — not
CI-log evidence — continues to identify the same three
Linux-unconditional, platform/host-specific skips named in every
earlier amendment's own evidence paragraph as the likely candidates;
this is stated separately as source-based inference, not something
this run's own log proves. The final leftover-container check ran the
anchored `grep -E '^codeagent-(verify-|baseline-|verification-)'`
pattern, covering all three CodeAgent container families, over a
complete, unfiltered `docker ps -a --format '{{.Names}}'` listing; its
captured output was empty and the step succeeded. This is
implementation/automated-test evidence only — it does not constitute
or substitute for a security review, and is GitHub-hosted
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim. It does not claim any composition root,
worktree lifecycle-projection table, `LifecycleLease` ownership
decision, CLI/UI, or signal/cancellation implementation exists, and
changes no cleanup precedence or exception semantics beyond what this
amendment already states above. `docs/threat-model.md`'s T-E1 entry
remains unaffected — this amendment is a controller-internal
correctness change, not production lifecycle composition or
concurrent-run protection.

## Amendment 10 (Accepted 2026-10-02): worktree-attribution substrate — persisted shapes, owner edges, and durable publication

### Context

Amendment 9's own "Explicitly out of scope" list named the one concrete
remaining question this ADR had left open: "the worktree transition/
combination table (or a decision to omit worktree attribution)." A
joint review first identified that the two originally-proposed candidate
designs for `worktree.expected_head` were both incomplete:

- **Independent re-published field** ("expected_head tracks the
  worktree's current HEAD, re-published on every checkpoint advance")
  would require new `RunController`/`CheckpointSession` ordering and
  failure-interleaving — not an independent substrate change, and out
  of scope for this amendment.
- **Cross-field invariant against `checkpoint_ref.accepted_sha`**
  ("worktree HEAD must equal checkpoint_ref.accepted_sha") is **provably
  wrong**, confirmed by direct code trace: `controller.py`'s
  `_dispatch_apply_patch` calls `self._patch_applier.apply(...)` (which
  commits directly into the worktree, moving its real `HEAD` to a new
  commit `B`) *before* calling `self._session.advance(result.commit_hash)`.
  `CheckpointSession.advance()` then durably publishes
  `ADVANCING(accepted_sha=A, expected_old_sha=A, proposed_new_sha=B)`
  *before* attempting the Git compare-and-swap. So, on **every single
  successful patch application** — not merely a crash — there is an
  entirely ordinary window where `checkpoint_ref.intent == ADVANCING`,
  `accepted_sha == A`, `proposed_new_sha == B`, while the real worktree
  `HEAD` is *already* `B`. A rule requiring `HEAD ==
  checkpoint_ref.accepted_sha` would misclassify this routine window as
  a conflict. Separately, if the Git CAS fails confirmed-`UNCHANGED`,
  `advance()` recovers `checkpoint_ref` to `PRESENT(A)` while the real
  worktree remains contaminated at `B` until disposal — exactly the
  scenario ADR 0003's own entry/resume gate (checking `HEAD` against
  `checkpoint_ref.accepted_sha`/`workspace.initial_commit`, not against
  any worktree-projection field) already exists to catch.

### Decision

**`worktree.expected_head` is the immutable materialization/origin
commit for one worktree incarnation** — the commit the worktree was
created from, fixed once at `creating`, retained unchanged through
`present` and `disposing`, cleared only at `disposing->absent`. It is
**never** republished on checkpoint advances and is **not** intended to
equal the worktree's continuously advancing live `HEAD`. The field name
`expected_head` is kept unchanged. The precise rationale: every existing
schema-v1 `lifecycle.json` already persists the `worktree` object with
this exact key (in its `absent`/`null` shape) — renaming the key would
require reader/writer compatibility handling or a schema-version
decision for that reason alone, even though no non-absent worktree
attribution has ever shipped. No migration of non-absent *values* is
required, since those shapes were previously refused outright and
therefore never persisted — but the key name itself is not free to
change without schema-compatibility cost. Keeping `expected_head`
avoids that unnecessary compatibility work; renaming merely for
clarity, with no functional benefit, was rejected on that basis — this
decision instead gives the existing name its exact, narrow, documented
meaning.

**Exact persisted shapes:**

| `intent` | `expected_head` |
|---|---|
| `absent` | `null` |
| `creating` | required, exact object-format OID — the materialization commit |
| `present` | required, exact object-format OID — same commit, unchanged |
| `disposing` | required, exact object-format OID — same commit, unchanged |

Every non-null `expected_head` is validated with the identical rejection
behavior already applied to `checkpoint_ref`'s own SHA fields:
lowercase-hex only, exact length for the repository's actual object
format (40 for sha1, 64 for sha256), the all-zero OID refused, and a
value of the wrong length or case for this repository's real format (or
a placeholder like `"A"`) refused even though the generic 40-or-64
structural check alone would not catch it.

**Legal owner edges** (single live owner, matching the lock-held/state-
gated discipline every other resource-transition method already uses):

| From | To | Condition |
|---|---|---|
| `absent` | `creating` | before any Git mutation; `expected_head` = the pinned materialization commit |
| `creating` | `present` | after full materialization is confirmed; `expected_head` unchanged (immutable-OID continuity) |
| `creating` | `absent` | **caller-confirmed recovery only** — this writer performs no Git/filesystem observation itself; the caller must have independently confirmed (a) the exact deterministic worktree path is absent from Git worktree registration, and (b) the filesystem leaf is confirmed absent via the existing fd-safe/no-follow reservation or worktree-cleanup observations — merely observing an empty directory is never sufficient |
| `present` | `disposing` | `expected_head` unchanged (immutable-OID continuity) |
| `disposing` | `absent` | only after confirmed removal; `expected_head` cleared to `null` |

No other edge is legal. `creating->present` and `present->disposing`
each additionally require the *exact same* `expected_head` the prior
record already carried — the materialization commit never changes once
recorded. Allowed lifecycle owner states for any worktree write:
`PREPARING`/`ACTIVE`/`CLEANING` only, identical to every other resource-
transition method's existing state gate; `RECONCILING`/`RECONCILED`/
`RECONCILIATION_FAILED` remain refused unconditionally, including an
exact no-op, since those remain reconciliation's own private write path
(not implemented by this amendment).

**Ownership and removal eligibility** (a later slice, not this one)
continue to follow this ADR's own already-accepted section 8 text
exactly, unchanged: deterministic contained path, safe filesystem
identity/type, exact `git worktree list --porcelain` registration, and
persisted non-`absent` intent. **A live `HEAD` mismatch alone is not,
and must never become, an ownership-conflict or removal-refusal
condition** — section 8's own text never mentioned `HEAD`-matching, and
the ADVANCING-window evidence above proves why it would be wrong to add
one. An earlier, non-accepted exploratory sketch (circulated only during
this amendment's own review, never published as part of this ADR) had
proposed a reconciler-table row refusing a `present`/`disposing` worktree
whenever its live `HEAD` disagreed with `expected_head`; that row does
**not** survive the ADVANCING-window analysis above and is explicitly
**not** part of this amendment — any future reconciliation slice must
derive its own rule from section 8's actual text, not from that
withdrawn sketch.

**Publication outcomes**: `WorktreePublicationFailure` carries the
identical 10-member taxonomy already established for containers/owner-
state (`NOT_INSTALLED`, `DURABILITY_UNCONFIRMED`, `CLEANUP_UNCONFIRMED`,
`STALE_EXPECTATION`, `WRONG_LOCK_SCOPE`, `ILLEGAL_TRANSITION`,
`SUBSTRATE_UNAVAILABLE`, `SCHEMA_INVALID`, `OVERSIZED`, `UNCLASSIFIED`).
`PROJECTION_DURABILITY_UNCONFIRMED` is never treated as success; explicit
`refresh()` is the sole recovery operation, never invoked automatically.

### Explicit non-claims

This amendment does **not** claim: that `workspace.GitWorktree` publishes
any worktree transition (no production integration exists); that
`reconciliation.py` observes, writes, or removes anything based on this
table (worktree removal during reconciliation remains entirely
unimplemented, unchanged from every prior slice's own scope); that
`LifecycleLease` ownership/close-timing is resolved (unchanged, still
open); that `docs/threat-model.md`'s T-E1 or T-F2 entries are newly
mitigated (both explicitly remain "not implemented"/"none yet" for
worktree removal, unaffected by a substrate with no production wiring);
or that any `RunController`, CLI, signal-handling, Docker, or checkpoint-
session behavior changed.

### Scope

**Implemented**: `src/codeagent/worktree_lifecycle.py` (new,
dependency-light leaf module: `WorktreeIntent`, `WorktreeTransition`,
`WorktreeTransitionPublisher`, `WorktreePublicationFailure`,
`WorktreePublicationError`); `src/codeagent/lifecycle_store.py` (schema
validator extended to the table above; `LifecycleWorktreePublisher`;
`SharedLifecyclePublishers`/`create_shared_lifecycle_publishers` extended
to a 4th facade sharing the existing cursor; `_LifecycleProjectionWriter.
record_worktree_transition()`).

**Explicitly not implemented by this amendment** (all later, separately-
scoped Milestone 3 work): any `workspace.py`/`GitWorktree` publication
call; any `RunController`/composition-root wiring; any
`checkpoint_session.py` change; any `reconciliation.py` change or
worktree removal; the `StateRoot.reserve_worktree_leaf()`/lifecycle-
projection crash-gap interaction (a crash between reservation creation
and the first `creating` publish leaves a reservation leaf directory
with no corresponding projection record — named, not resolved, by this
amendment); CLI, signal handling, abandonment, maintenance trace, model,
or UI work.

### Evidence

Verified locally (macOS, real Docker daemon,
`CODEAGENT_REQUIRE_DOCKER=1`): `test_worktree_lifecycle.py` (new), 27
passed; `test_lifecycle_store.py`, 256 passed (up from 210); the
established sixteen-file focused set (the fifteen-file set established
since Slice 3A-1, plus the new `test_worktree_lifecycle.py`), 1,299
passed, in both forward and reverse file order; the complete suite,
2,823 passed, 0 skipped, with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff
--check` clean; no leftover `codeagent-*` containers, extra worktrees,
`refs/codeagent` refs, lingering processes, or temp/default state roots
afterward.

Four pre-existing tests required correction, not weakening, since this
amendment's whole point is to stop categorically refusing non-absent
worktree shapes: `test_worktree_non_absent_intent_categorically_refused`
split into `test_worktree_non_absent_intent_with_valid_oid_accepted`
(now correctly asserting acceptance) and
`test_worktree_non_absent_intent_without_expected_head_refused`
(preserving the still-correct combination-rule refusal); and
`test_cleaning_to_complete_worktree_dirty_shape_is_unloadable` was
renamed `..._is_refused_by_clean_final_guard` and updated to assert
`ILLEGAL_TRANSITION` (the clean-final guard's own worktree check, now
genuinely reachable for the first time) rather than `SCHEMA_INVALID`
(which predated this amendment). `lifecycle_store.WorktreeAttribution`
— a public, non-underscored class — is kept as an exact identity alias
for `worktree_lifecycle.WorktreeTransition` (`WorktreeAttribution is
WorktreeTransition`, never a second dataclass or a wrapper), since a
repository-wide grep finding only one internal reference cannot prove
no external caller imports the name. Construction is now deliberately
**stricter** than the retired dataclass was: the old type had no
`__post_init__`, so `WorktreeAttribution(intent=PRESENT,
expected_head=None)` was previously constructible; the identical call
through the alias now raises `ValueError`, since
`WorktreeTransition.__post_init__` enforces the real combination rule.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review.

**Confirmed on GitHub-hosted Linux CI** (commit
`f41da250bb7e67ea2b42113e17e25f3ec8143436`, run
[37060981509](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37060981509),
`ubuntu-24.04` x86_64, Python 3.12, conclusion `success`, 1 job, 13
steps, every step `success`): the mandatory Docker preflight step
succeeded (Docker Engine - Community, version `28.0.4`); the pinned
verification image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER=1`
confirmed present in that step's own logged environment): `3 passed in
2.31s`, 0 skipped — this is this repository's own legacy Milestone-1
suite, exactly, and is **not** specific evidence for this amendment,
which adds no real-Docker test of its own. The separate "Run complete
test suite" step (`python -m pytest -q`, `CODEAGENT_REQUIRE_DOCKER=1`
also confirmed present in that step's own logged environment) reported
`2825 passed, 3 skipped in 48.81s`; `2825 + 3` equals the local
collected total of 2,828 reported just above. This amendment's new
worktree tests (`test_worktree_lifecycle.py` and the additions to
`test_lifecycle_store.py`) executed through this step, not the
dedicated Docker step. The run used `pytest -q`, which prints no test
identities, so these 3 skips are **not identified from this run's own
log** and are not guessed here — they are not evidence of Docker
unavailability, since the daemon, the pulled image, and
`CODEAGENT_REQUIRE_DOCKER=1` were all already confirmed for that same
step. Source inspection — not CI-log evidence — continues to identify
the same three Linux-unconditional, platform/host-specific skips named
in every earlier amendment's own evidence paragraph as the likely
candidates; this is stated separately as source-based inference, not
something this run's own log proves. The final leftover-container
check ran the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'` pattern, covering all
three CodeAgent container families, over a complete, unfiltered
`docker ps -a --format '{{.Names}}'` listing; its captured output was
empty and the step succeeded. This is implementation/automated-test
evidence only — it does not constitute or substitute for a security
review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
specifically, not a general Linux or ARM64 portability claim. It does
not claim any change to this amendment's accepted semantics, persisted-
shape table, legal owner-edge table, scope, or non-claims — all remain
exactly as stated above. `docs/threat-model.md`'s T-E1/T-F2 entries
remain unaffected.

### Correction pass (2026-10-02)

A targeted review found five issues in this amendment's first pass, all
confirmed against the actual code before any fix was made:

1. `lifecycle_store.WorktreeAttribution`'s retirement (above) was too
   aggressive for a public, non-underscored class — restored as an
   exact identity alias, with construction now deliberately stricter
   (see above) and a dedicated identity test.
2. `worktree_lifecycle.WorktreePublicationError`'s docstring said
   "Raised only by `LifecycleWorktreePublisher.publish()`," too strong
   for a public exception belonging to a structural Protocol (a test
   double or a future alternate publisher may raise it directly).
   Rewritten to mirror `CheckpointPublicationError`/
   `OwnerStatePublicationError`'s own already-correct wording exactly.
   No behavior change.
3. `_validate_worktree_edge()`'s `creating->absent` comment referenced
   `_WORKTREE_TRANSITION_EDGES`, a constant that does not exist (this
   function is an explicit conditional chain, with no separate edge-set
   constant, unlike the container table). Corrected to point to the
   function's own docstring and this amendment instead of inventing a
   redundant constant merely to satisfy the stale comment.
4. This amendment's own evidence text, and `CLAUDE.md`'s/
   `ENGINEERING_LOG.md`'s matching Milestone-3 entries, had claimed this
   slice's direct writer/publisher tests use "real Git-derived SHAs."
   Confirmed false by direct inspection: the test helpers (`_sha`,
   `_sha256`) construct object-format-shape-valid *synthetic* OIDs from
   repeated hex characters (e.g. `("1" * 40)[:40]`), never from `git
   rev-parse` or a real repository. Corrected in all three locations to
   "object-format-valid synthetic SHA-1/SHA-256 OIDs." No new real-Git
   tests were added to preserve the inaccurate phrase — none of this
   slice's own claimed behavioral properties actually require one; this
   remains, accurately, not real `GitWorktree` integration or crash-
   ordering evidence.
5. This amendment's own field-naming rationale (above, now corrected in
   place) had said retaining `expected_head` meant "there is no
   migration cost either way" — false: every existing schema-v1
   `lifecycle.json` already persists the `worktree` object with this
   exact key (in its `absent`/`null` shape), so renaming the key itself
   would require reader/writer compatibility handling or a schema-
   version decision, independent of whether any non-absent *value* has
   ever shipped. Corrected to state the precise rationale: the key
   already exists in every v1 document; keeping it avoids unnecessary
   schema-compatibility work; no migration of non-absent values is
   needed only because those shapes were previously refused outright.

Five new tests added, narrowly:

1. `test_worktree_attribution_is_exact_identity_alias_for_worktree_transition`
   — `WorktreeAttribution is WorktreeTransition` identity.
2. `test_worktree_attribution_construction_is_now_deliberately_stricter`
   — the now-stricter construction behavior.
3. `test_worktree_publisher_identity_properties_match_cursor_projection`
   — `LifecycleWorktreePublisher.lifecycle_id`/`state_root_id` matching
   the cursor's own projection identity.
4. `test_worktree_publication_error_message_never_leaks_cause_detail_or_enum_spelling`
   — the fixed production message never leaking the injected
   `LifecycleStoreError`'s own detail text or categorical enum spelling.
5. `test_worktree_publisher_wrong_type_transition_translated_without_raw_leakage`
   — a wrong-type `transition` (five representative bad values)
   translating to `WorktreePublicationError`/`ILLEGAL_TRANSITION` with
   no raw `AttributeError`/`TypeError` leakage.

No change to the accepted `expected_head` semantics, the persisted-
shape table, or the legal owner-edge table above — this pass is
corrective only. `workspace.py`, `controller.py`,
`checkpoint_session.py`, `reconciliation.py`, `StateRoot` behavior,
Docker behavior, and `docs/threat-model.md` remain untouched, as before.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`):
`test_worktree_lifecycle.py` + `test_lifecycle_store.py` together, 288
passed (up from 283 before this pass's five new tests); see
`ENGINEERING_LOG.md`'s dated correction-pass entry for the complete
focused-set and full-suite totals.

## Amendment 11 (Accepted 2026-10-02): optional `GitWorktree` worktree-transition publication — an unwired producer seam

**Status of this seam.** `GitWorktree` gains an optional, keyword-only
`worktree_publisher` parameter (default `None`). It is an **unwired
producer seam**: no production composition path constructs
`GitWorktree` at all, and none supplies a publisher (proven by AST
tests: no `GitWorktree(...)` call and no `worktree_publisher=` keyword
anywhere in `src/codeagent/`). **Lifecycle-aware production composition
must not be enabled until worktree reconciliation and removal exist.**
Today, a crash that leaves a non-`absent` worktree projection **blocks
admission** of new runs in that repository (`RECONCILIATION_BLOCKED`), and
so does an `absent` projection accompanied by a leftover reserved leaf or
Git registration — fail-closed blocking, not recovery. The reservation/projection crash gap
(below) remains open. T-E1 and T-F2 are **not** newly mitigated. The
evidence for this amendment is implementation/automated-test evidence
only, not a security review.

### 1. Identity binding

When a publisher is supplied, a reservation is required, and the
publisher's `repo_key`, `state_root_id`, and `lifecycle_id` must equal
the reservation's — checked in that fixed order, before the Git
preflight and before any filesystem access. Every refusal is a
`GitWorktreeError` with a fixed message that never echoes a value; a
publisher lacking identity attributes is refused `from None`.
`repo_key` is included because it is part of the deterministic leaf
path. `_WorktreeLeafReservation` now carries these three fields
(supplied by `StateRoot.reserve_worktree_leaf()`) as read-only
properties.

**Protocol contract change.** `worktree_lifecycle.WorktreeTransitionPublisher`
gains a `repo_key` property. The Protocol is not runtime-checked, so
existing code is source-compatible, but every conforming implementation
must now provide it. The only implementation,
`lifecycle_store.LifecycleWorktreePublisher`, derives it from the
shared cursor's current projection, like its other two identity fields.

### 2. Publication order

The three non-`absent` publications (`creating`, `present`,
`disposing`) carry the same immutable materialization (origin) commit
as `expected_head`; `absent` carries no `expected_head` (`None`).

1. `creating` — after `claim()` and the pre-Git identity check, before
   any Git mutation.
2. `present` — after materialization, post-Git verification, and
   `consume()`, **outside** the enter-time cleanup-owning block, and
   before `path`/`initial_commit` are published.
3. `disposing` — first thing in `dispose()`, before
   `git worktree remove`; published at most once (a physical retry
   after `GitWorktreeCleanupError` skips it).
4. `absent` — only after confirmed registration absence **and**
   `reservation.observe_leaf() is LeafObservation.ABSENT`.

`preserve()` publishes nothing (the projection stays `present`) and is
refused once a `disposing` publication has been attempted. A Git
failure after `creating` runs the existing exact enter-time cleanup and
leaves the projection `creating`; there is **no** `creating → absent`
recovery publication. Nothing at this layer refreshes or retries.

### 3. Failure disposition

A `WorktreePublicationError` becomes `GitWorktreeLifecycleError`
(`reason`, `transition`, one fixed message per transition), raised
`from` the original and latched on the public `lifecycle_error` field.
`cleanup_error` keeps its exact existing type and meaning.

| Failed transition | Physical state left | Disposition | `path` visible |
|---|---|---|---|
| `creating` | no Git mutation; reservation `CLAIMED`, its exit removes the empty leaf | active | no |
| `present` | **registered, materialized worktree always retained**, whatever the reason (`DURABILITY_UNCONFIRMED` cannot distinguish installed `creating` from installed `present`) | active | no |
| `disposing` | worktree intact; removal never attempted | active | yes |
| `absent` | already physically removed; never undone, never reported durably absent | disposed | no |

**Latch.** Once set, `__enter__`, `dispose`, `preserve`, and `__exit__`
check it before every idempotent early return and re-raise the same
instance (identity and `__cause__` preserved); nothing is published
again. `__exit__` raises it only when the body raised nothing — a body
exception is never masked.

### 4. `LeafObservation`

`_WorktreeLeafReservation.observe_leaf()` is observational and never
raises: `UNKNOWN` if the reservation's descriptors are already closed
(checked by flag, since closed fd numbers can be reused); fd-relative
no-follow `stat(name)` → `ENOENT` is `ABSENT`, any other `OSError` is
`UNKNOWN`; a non-directory (including a symlink) is `OTHER`; a
directory is `RESERVED_INODE` only when `samestat` with the held leaf
fd, else `OTHER`. Link count is never consulted (APFS reports
`st_nlink == 2` for an unlinked directory's held fd). It is used **only
in publisher mode**; every no-publisher path keeps its existing
`Path.exists()` observation (which can report a dangling symlink as
absent — an unchanged residual risk for a separate slice). Lifecycle-
aware disposal therefore requires the reservation context to stay open
through `dispose()`/`__exit__`; if it is closed, `absent` is not
published and `GitWorktreeCleanupError` is raised.

### 5. Crash gap — open, fail-closed

Reconciliation still refuses every non-absent worktree projection, and
an `absent` projection with a leftover leaf/registration. A crash at
any point between reserving the leaf and a confirmed `absent` therefore
blocks admission. Real SIGKILL tests prove this for a durable `creating`
(empty unregistered leaf) and a durable `present` (registered
worktree). Closing the gap needs a composition owner that controls
reservation order, a §8 refinement for an exactly-empty unregistered
reserved leaf, and worktree reconciliation removal — all later work.

### 6. Disclosed test corrections

Amendment 10's static test that forbade `workspace.py` from referring
to worktree publication is narrowed (renamed
`test_no_controller_or_reconciliation_integration_and_workspace_uses_only_the_leaf_module`):
controller/reconciliation integration stays fully forbidden, and
`workspace.py` may import only the stdlib-only `worktree_lifecycle`
leaf, never `lifecycle_store`. The Protocol-shape test, whose docstring
said "exactly" but checked a subset, is now an exact equality check.

### 7. Evidence

Local (macOS, `CODEAGENT_REQUIRE_DOCKER=1`): complete suite 2,904
passed, 0 skipped.

**Confirmed on GitHub-hosted Linux CI** (commit
`c248b6d656a6afc52ce08f140ce5f0a3b598343a`, run
[37088701609](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37088701609),
job "Test (ubuntu-24.04, Python 3.12)", runner image `ubuntu-24.04`
(Ubuntu 24.04.5 LTS) x86_64, Python 3.12.14, conclusion `success`; the
API reports 13 steps, every one `success`): the mandatory Docker
preflight succeeded (Docker Engine - Community, client and server
version `28.0.4`); the pinned verification image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and reported platform `linux/amd64`. The dedicated "Run real
Docker verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER: 1` in
that step's own logged environment): `3 passed in 2.87s` — the legacy
Milestone-1 suite only, **not** Amendment-11-specific evidence. The
separate "Run complete test suite" step ran `python -m pytest -q`
(`CODEAGENT_REQUIRE_DOCKER: 1` also present in that step's own logged
environment) and reported `2901 passed, 3 skipped in 53.72s`; this is
the step through which Amendment 11's tests (including
`tests/integration/test_worktree_publication.py`) executed. `2901 + 3`
equals the local collected total of 2,904. `pytest -q` prints no
skipped-test identities, so the 3 skips are not identified or guessed
from this log; they are not attributable to Docker, which was required
and available during that step. The final leftover-container step ran
`docker ps -a --format '{{.Names}}'` through the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'`, covering all three
CodeAgent container families; its output was empty and the step
succeeded. This is implementation/automated-test evidence only, not a
security review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
specifically, not a general Linux or ARM64 claim.

Nothing in this evidence changes the accepted API, binding, ordering,
failure table, `LeafObservation` contract, crash-gap analysis, or
scope above; T-E1/T-F2 are not newly mitigated.

## Amendment 12 (Accepted 2026-10-02): reconciling a dead `creating` worktree whose reserved leaf is empty

**Scope.** This refines §8 for exactly one dead-run shape: a worktree record
`creating(expected_head)` whose deterministic leaf
`<root>/worktrees/<repo-key>/<lifecycle-id>` is an empty, private,
unregistered directory with no Git admin entry. Automatic pre-run
reconciliation now removes that empty leaf and records the entry
`RECONCILED`. Everything else in §8 is unchanged:
- `present` and `disposing` worktrees are still `REFUSED`;
- an `absent` record with a leftover directory or registration is still
  `REFUSED`;
- `git worktree remove` and `git worktree prune` are never used by
  reconciliation;
- the live owner's worktree transition table is unchanged.

Nothing here is wired to a CLI or controller entry point. T-E1 is unchanged;
T-F2 is partially addressed at the substrate/reconciler level only and is
not mitigated end to end. The evidence below is implementation and
automated-test evidence, not a security review.

### 1. Attribution evidence

"Empty and unregistered" is not ownership proof by itself. All of the
following are required, under the A4 trust model (the same user and host are
trusted):
- **Namespace.** The path derives only from the trusted state root, the
  trusted `repo_key`, and a validated 128-bit random `lifecycle_id` (I1).
- **Durable intent.** The record for that lifecycle, loaded under the
  lifecycle lock, is exactly `creating` with an origin commit, checkpoint ref
  absent, `failure` null, and both container records absent. By Amendment
  11's ordering, `creating` is published only after the owner exclusively
  created that leaf.
- **No live owner.** The repository lock is held and the lifecycle lock was
  acquired.
- **Git holds nothing.** A bounded `git worktree list --porcelain -z` does
  not register the exact canonical path, and a bounded admin scan (§3) finds
  no admin entry for this lifecycle.
- **No container.** A fresh Docker listing shows neither deterministic
  container name (I4).
- **The leaf itself.** A no-follow, descriptor-relative open finds a real
  directory, owned by the current user, mode exactly 0700, with zero
  entries, whose name lookup and opened descriptor are the same inode.

What this cannot prove: that the inode opened is the one the dead owner
reserved (no inode is persisted).

### 2. Removal and its limits

The removal is `rmdir(name, dir_fd=parent_fd)`, authorised only after the
full evidence set and an immediate re-check of identity and emptiness through
held descriptors. `rmdir` is non-recursive and refuses a symlink, a
non-directory and a non-empty directory, but it acts on a *name* relative to
the parent descriptor: it proves neither ownership nor, when it reports
success, absence. A fresh no-follow lookup always follows it, compared with
the held leaf descriptor:
- "not found": absence confirmed;
- the same directory inode: the original is still present;
- anything else: a replacement occupies the name;
- a failed observation: unconfirmed.

A held descriptor keeps its inode from being reused, so that comparison is
reliable against inode reuse. The same-user race between the final re-check
and `rmdir` remains: a same-user process could swap a different empty
directory into our deterministic name inside the private state root, and it
would be removed. That is within A4's scope; there is no claim of protection
against a hostile same-user process.

### 3. Bounded Git admin scan

`git worktree list` does not report an admin directory under
`<common-dir>/worktrees/` whose `gitdir` file was never written, so
registration absence alone does not prove Git holds nothing for the path.
The scan:
- opens the trusted canonical common directory, then its `worktrees` child,
  each with `O_NOFOLLOW|O_DIRECTORY|O_CLOEXEC` and `_assert_cloexec`;
- treats an absent `worktrees/` as no admin entry, and a symlink or
  non-directory there as `REFUSED`;
- streams names only (never stats, opens, reads or deletes an entry), up to
  4,096 entries and 262,144 cumulative name bytes; exceeding either is
  `SUBSTRATE_UNAVAILABLE`;
- matches `^<32 lowercase hex>[0-9]*$` on bytes, where the hex part must be
  this lifecycle id. A match is `REFUSED`, including the (safe) false
  positive of an unrelated entry with that random name;
- closes each opened descriptor exactly once; a close failure dominates every
  scan result and is `SUBSTRATE_UNAVAILABLE`.

### 4. Order and outcomes

Inspection mutates nothing: the record, the checkpoint ref, the Git listing,
the admin scan, the leaf, and the Docker listing. Then:
1. if not already `RECONCILING`, write `RECONCILING` with
   `attempts_total + 1` (the worktree stays `creating`; this is the only
   increment);
2. remove the empty leaf, if one was found;
3. close the leaf descriptors — a close failure dominates and stops every
   later step;
4. repeat the Git listing and admin scan;
5. the reconciler-only write `creating → absent`, with the count unchanged;
6. `RECONCILED`.

The zero-mutation rule (§10, I6) covers inspection only. Once step 1 has run,
`REFUSED` or `SUBSTRATE_UNAVAILABLE` may follow a real removal — the
container reconciler's existing convention. A replacement entry, an
`ENOTEMPTY`, or a registration found after removal is `REFUSED`; the original
still present, or a failed record write, is `FAILED`; a failed observation or
close is `SUBSTRATE_UNAVAILABLE`. A durability-unconfirmed write may already
be installed; every pass reloads the record and follows what it finds.

Resume shapes, none incrementing the count:
- **A:** `RECONCILING` + `creating` + leaf present → re-inspect everything,
  remove if still eligible;
- **B:** `RECONCILING` + `creating` + leaf absent → no `rmdir`; publish
  `creating → absent`;
- **C:** `RECONCILING` + worktree absent → the existing absent-worktree path,
  which completes `RECONCILED` without republishing the worktree edge.

### 5. Maintenance trace

`schema_version` stays 1 (no consumer exists); the entry event's `worktree`
object gains:
- `initial_persisted_intent`: from the locked re-read; `null` if the pass
  stopped before it (never taken from the pre-lock peek);
- `leaf_outcome`: `not_applicable`, `not_inspected`, `already_absent`,
  `conflict_not_removed`, `removal_not_attempted`, `removed`,
  `absent_after_failed_rmdir`, `original_still_present`,
  `replacement_conflict`, `post_inspection_failed`, or `close_failed`;
- `removal_observation`: the removal primitive's own result, kept even when
  `close_failed` dominates;
- `absent_transition_confirmed_this_pass`: true only when this pass's
  `creating → absent` write was confirmed (false in resume case C and after a
  durability-unconfirmed write).

No paths, admin names, raw Git output or exception text are recorded.

### 6. Shared primitive and descriptors

The identity/emptiness/`rmdir`/post-observation sequence is one
module-level primitive in `state_root.py`, shared with
`_WorktreeLeafReservation`, whose externally visible messages and behaviour
are unchanged (all existing reservation and workspace tests pass
unmodified). The reconciler's leaf handle owns both descriptors only when the
leaf is an eligible empty directory, and closes them exactly once: after an
unconfirmed close it never touches those descriptor numbers again and
re-raises one latched, sanitized error whose chain is that error →
`close_confirmed`'s error → any body exception in flight. `reconciliation.py`
still contains no filesystem-removal call.

### 7. Evidence

Local (macOS, Docker already running, `CODEAGENT_REQUIRE_DOCKER=1`): 136 new
tests (50 state-root, 14 lifecycle-store, 69 reconciliation, 3 integration);
the 17-file focused set 1,516 passed in forward and reverse order; the full
suite 3,040 passed, 0 skipped. The SIGKILL tests use real processes: a dead
owner after a durable `creating` is now reconciled by the next admission, a
reconciler killed after step 1, step 2 or step 5 resumes (shapes A, B, C)
with exactly one attempt counted, and a dead owner after a durable `present`
still blocks admission.

**Confirmed on GitHub-hosted Linux CI** (commit
`b9d79bbb351d2b75dc4c95c61478a6d24cce036f`, run
[37093418065](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37093418065),
job "Test (ubuntu-24.04, Python 3.12)", runner image `ubuntu-24.04`
(Ubuntu 24.04.5 LTS) x86_64, Python 3.12.14, conclusion `success`; the API
reports 13 steps, every one `success`): the mandatory Docker preflight
succeeded (Docker Engine - Community, client and server `28.0.4`); the
pinned image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and reported platform `linux/amd64`. The dedicated "Run real
Docker verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER: 1` in
that step's own logged environment): `3 passed in 2.01s` -- the legacy
Milestone-1 suite, **not** Amendment-12-specific evidence. The separate
"Run complete test suite" step ran `python -m pytest -q`
(`CODEAGENT_REQUIRE_DOCKER: 1` also in that step's own logged
environment) and reported `3037 passed, 3 skipped in 44.12s`; `3037 + 3`
equals the local collected total of 3,040. `pytest -q` does not name the
three skipped tests, so they are not identified or guessed here; they
are not attributable to Docker, which was required and available in that
step. That Amendment 12's tests ran in this step is an inference from the
whole-suite command and the matching collected total, not from named-test
log evidence. The final leftover-container step ran `docker ps -a
--format '{{.Names}}'` through the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'`, covering all three
CodeAgent container families; its output was empty and the step
succeeded. This is implementation/automated-test evidence only, not a
security review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
specifically, not a general Linux or ARM64 claim.

The row remains unwired; `present`/`disposing` worktrees and an
`absent` record with a leftover directory still block admission; the A4
same-user race before `rmdir` remains; and this evidence adds no broader
T-F2, T-F1, or T-E1 mitigation claim.

## Amendment 13 (Accepted 2026-10-03): reconciling a dead materialized `creating`, `present` or `disposing` worktree

**Scope.** This refines §8 for dead lifecycles whose worktree record is
`creating`, `present` or `disposing` (with its origin commit) and whose
checkpoint ref, `failure`, and both container records are already absent.
Such a worktree is removed only by one bounded, hardened
`git worktree remove --force <exact canonical path>`. Unchanged:
- a `creating` record with no registration still goes to Amendment 12's row;
- locked, ambiguous, bare, or inconsistent registrations are `REFUSED`, and
  `-f -f` and `git worktree prune` are never used;
- an unregistered directory with content (for example a partial Git
  removal) is `REFUSED`; recursive deletion stays forbidden (I2);
- entries whose containers or checkpoint ref remain are not handled here;
- no live-`HEAD` comparison (Amendment 10).

Nothing is wired to a CLI or controller entry point. T-E1 is unchanged;
T-F2 stays partially addressed at the reconciler level only. Evidence is
implementation and automated-test evidence, not a security review.

### 1. Listing grammar

`git worktree list --porcelain -z` is parsed structurally: each field is
`key` or `key SP value`, NUL-terminated, and a record ends with an empty
field. Each record needs `worktree <absolute path>` first; `HEAD <oid>`
(lowercase hex of the repository's object-format length) unless `bare`;
exactly one of `branch <ref>`, `detached`, `bare`; and optional `locked` /
`prunable` with or without a reason (never retained). An unknown key, a
repeated field, a bad terminator, or any other violation is malformed. A
separate target-aware analysis counts records matching the deterministic
path (0, 1, or "many"); duplicate non-target paths are malformed, and
"many" is positive ambiguity (`REFUSED`). Malformed or failed listings are
`SUBSTRATE_UNAVAILABLE`. The legacy registered-path set uses the same parser
and rejects any duplicate path.

### 2. Attribution before removal

Removal needs exactly one non-bare, unlocked target registration, exactly
one admin name matching `<lifecycle-id>[0-9]*` (the bounded scan, now
counting), and a fresh no-follow observation of the leaf: a real directory,
owned by the current user, mode exactly 0700, containing a no-follow regular
`.git` file (`materialized`). `prunable` with a materialized leaf is
`REFUSED`. **D2:** for a `disposing` record only, the same registration and
admin evidence with the leaf confirmed `absent` also authorises the exact-path
removal (Git 2.54 then removes exactly that registration); `prunable` is
allowed but not required. The same shape under `present` or `creating` is
`REFUSED`. A `disposing` record with no registration, no admin entry, and an
absent leaf collapses to absent without running Git. The observer holds no
descriptor across the command, so it cannot tell the original directory from
a valid replacement: it reports `materialized` for either. Git's own
back-pointer check refuses a swapped path (observed directly).

Evaluation order: registration, then admin count, and the leaf observer only
when its result is needed. Registration or admin `unknown` is
`SUBSTRATE_UNAVAILABLE`; a many/bare/locked registration, a single
registration whose admin count is not exactly one, `present` with no
registration, and `disposing` with no registration but an admin entry are all
`REFUSED` without observing the leaf, so a leaf observation (or its cleanup
failure) can never override them. The observer runs only for one eligible
registration with one admin entry, or for `disposing` with neither.

### 3. The command

`run_git_bounded(root, "worktree", "remove", "--force", <path>, limit=4096,
timeout=30)`, with the hardened baseline argv and sanitized environment. Its
exit status is never evidence. A nonzero exit, a timeout, an output overflow,
or a monitoring failure (each with the child confirmed stopped) is followed by
three fresh observations. An unconfirmed child stop, an unexpected failure
reason, or a launch failure is `SUBSTRATE_UNAVAILABLE` with no observation
and no later transition.

### 4. After the command

`disposing → absent` is published only when the registration, the admin
entry, and the leaf are all confirmed absent. Otherwise: any unknown
observation is `SUBSTRATE_UNAVAILABLE`; a leaf conflict, a duplicated, bare
or newly locked registration, or inconsistent admin evidence is `REFUSED`;
the registration still present (directory present or missing) or an
unregistered materialized directory is `FAILED`. The next pass re-inspects
from scratch; an unregistered materialized directory is then `REFUSED`.

### 5. Transitions, resume, and trace

Reconciler-only edges (the live owner's table is unchanged):
`creating/present → disposing` (same origin commit), `disposing → disposing`
(same commit; a no-op that publishes nothing), and `disposing → absent`.
Order: `RECONCILING` (`attempts_total + 1` only on a fresh cycle), `→
disposing`, the command, the observations, `→ absent`, `RECONCILED`. Every
pass reloads the record; a durability-unconfirmed write may be installed and
the next pass follows what it finds. The maintenance trace (`schema_version`
1) gains `registration_pre`/`registration_post` (state, target count,
`target_bare`, `locked`, `prunable` — non-null only when meaningful),
`admin_matches_pre`/`post`, `leaf_pre`/`post`, `removal_attempt`, and
`disposing_transition_confirmed_this_pass`; no paths, reasons, Git output,
admin names, or exception text.

### 6. SIGKILL during the command

If the reconciler is killed while `git worktree remove` runs, that Git
process is orphaned; nothing prevents this or bounds its lifetime. The next
pass is still fail-closed: it re-observes everything, and because the command
only deletes, all three observations being absent is a terminal state, while
anything present yields `FAILED`, `REFUSED` or `SUBSTRATE_UNAVAILABLE` and
never `absent`. A deterministic test models this state (the stand-in command
deletes only the admin entry and leaves a detached grandchild); it is a model,
not proof of every behaviour of a real concurrent removal.

### 7. Evidence

Local (macOS, Git 2.54.0, Docker already running, `CODEAGENT_REQUIRE_DOCKER=1`):
124 new tests (13 state-root, 8 lifecycle-store, 98 reconciliation, 5
integration); the 17-file focused set 1,640 passed in forward and reverse
order; the full suite 3,164 passed, 0 skipped. Real-Git coverage includes
`creating`/`present`/`disposing` removal, SHA-256, D2, locked, symlink-swap,
missing `.git`, extra admin entries, an unregistered leftover, a symlink out
of the worktree and a hostile clean filter (both harmless), and SIGKILL tests
for a dead `present` owner and for the reconciler after each step. The
permission-based partial-removal test is conditional: it runs only when the
host's Git reproduces that behaviour (it did on this host); the portable
proof is a deterministic fault-injection test.

**Confirmed on GitHub-hosted Linux CI** (commit
`7d359ab4ae0324a7aba873ac3aac436e719bf562`, run
[37096976140](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37096976140),
triggered by a push to `main`; job "Test (ubuntu-24.04, Python 3.12)",
runner image `ubuntu-24.04` (Ubuntu 24.04.5 LTS) x86_64, Python 3.12.14;
conclusion `success`; the API reports 13 steps, every one `success`). The
checkout step printed `git version 2.55.0`. The mandatory Docker preflight
succeeded (Docker Engine - Community, client and server `28.0.4`); the
pinned image
(`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
was pulled and reported platform `linux/amd64`. The dedicated "Run real
Docker verification tests" step ran only `python -m pytest
tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER: 1` in that
step's own logged environment): `3 passed in 2.30s` -- the legacy
Milestone-1 suite only, **not** Amendment-13-specific evidence. The separate
"Run complete test suite" step ran `python -m pytest -q`
(`CODEAGENT_REQUIRE_DOCKER: 1` also in that step's own logged environment)
and reported `3161 passed, 3 skipped in 64.38s`; `3161 + 3` equals the
local collected total of 3,164. `pytest -q` does not name the three skipped
tests, so they are not identified here, and the log does not show whether
the conditional permission-based partial-removal test ran or skipped. The
skips are not attributable to Docker unavailability: Docker was required
and confirmed available in that step. The final leftover-container step ran
`docker ps -a --format '{{.Names}}'` through the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'`, covering all three
CodeAgent container families; its output was empty and the step succeeded.
This is implementation/automated-test evidence only, not a security
review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence specifically,
not a general Linux or ARM64 claim.

Remaining risks are unchanged: no production wiring calls
`prepare_lifecycle()`; entries whose containers or checkpoint ref remain,
locked/ambiguous/inconsistent registrations, partial-removal leftovers, and
an `absent` record with a leftover directory still block admission; a
SIGKILL during the Git command can orphan the Git process with no bound on
its lifetime, and the test for that state is a deterministic model, not
proof; and a valid replacement directory cannot be distinguished from the
original.

## Amendment 14 (Accepted 2026-10-03): the first lifecycle-aware internal run composition

**Scope.** Milestone 3 Slice 3C-4 adds `codeagent._lifecycle_run.run_lifecycle_aware()`,
the first code that composes the lifecycle substrate into one real run:
`prepare_lifecycle()`'s lease, one shared projection cursor with its four
publishers (`create_shared_lifecycle_publishers()`), the deterministic worktree
reservation, a publisher-mode `GitWorktree`, a lifecycle-aware `DockerVerifier`,
a publishing `CheckpointSession`, and a `RunController` with its owner-state
publisher. It settles the question Amendment 8 §6 left open: who owns the
`LifecycleLease`, and what a release failure does to the outer result. No
existing `src` module changes.

**Boundary.** No bundled CLI or production module imports or calls it
(AST-pinned). It lives in an underscore module that `codeagent/__init__.py` does
not export. Python does not enforce privacy, so a consumer that imports it leaves
the supported boundary. This amendment does not claim that only tests can reach
it.

**Gate.** No operator entry point may call this composition until all of the
following exist:
- a dead-run checkpoint-ref reconciliation row;
- a worktree-plus-container reconciliation row;
- abandonment (§11);
- ADR 0005 cancellation.

The reason: a crash in the windows that dominate a real run's duration (§10
below) blocks the repository, and no CodeAgent recovery exists for it yet.

### 1. Ownership

The composition owns the lease for the whole run. The lease holds the repository
lock, the lifecycle lock, and the state-root and run-directory descriptors; the
repository lock is therefore held across the entire composed run.

| Resource | Owner and lifetime |
|---|---|
| Writer and cursor | Scoped to the lease. |
| Reservation | Owned by the composition, and `CONSUMED` once the worktree is entered. |
| `GitWorktree` | Entered by the composition. Once `run()` starts, teardown authority passes to the controller's `_terminate()`. |
| Controller, verifier, session, applier, reader, sink | Hold nothing between calls. |

In reservation mode `GitWorktree` owns no descriptor or temporary directory of
its own (`_tempdir` is set only in tempdir mode), so not exiting it leaks
nothing.

I13 holds: `complete()` (the final projection write) runs inside
`_terminate()`, and the lifecycle lock is released afterwards, in
`lease.close()`.

### 2. Ordered body

1. `prepare_lifecycle()`. It self-cleans and raises on failure.
2. Inside one `try`:
   - open the writer;
   - create the shared publishers;
   - take the trusted source path, `initial.source_repo_path`;
   - validate the evidence root (§4);
   - reserve and enter the leaf;
   - construct and enter `GitWorktree`;
   - build every collaborator from the worktree path;
   - call `controller.run()`.
3. **H1.** The `except BaseException` handler only binds `original`. Every
   cleanup stage runs afterwards, outside any handler, so the composition never
   adds an implicit `__context__` to a stage's exception (the Amendment 9/3C-3
   precedent).
4. Take R5 if `original` is set, otherwise R4.

### 3. Cleanup rules

Each cleanup stage is attempted exactly once, in a fixed order, through
`_attempt`. `_attempt` returns either the stage's declared failure or anything
else raised, including any `BaseException`, and never raises itself. No
`finally` block wraps a stage.

- **R1, sole teardown authority.** Once `run()` returns, `_terminate()` was the
  only disposal and ref-deletion attempt. The composition never calls
  `GitWorktree.__exit__`, `dispose()` or `preserve()` again, so nothing is
  disposed a second time and nothing in Git, Docker or the filesystem is mutated
  after `RunFinished`. A latched `GitWorktreeLifecycleError` is never re-raised,
  because the controller already mapped it.
- **R2, stages.**
  - Returned path: `RESERVATION` (`reservation.__exit__(None, None, None)`),
    then `LEASE` (`lease.close()`).
  - Raise path: `WORKTREE_PHYSICAL` (`worktree.__exit__(exc)`, only if the
    worktree was entered), then `RESERVATION` (if reserved), then `LEASE`.
- **R3, declared types.**

  | Stage | Declared type |
  |---|---|
  | `RESERVATION` | `LifecycleFsError` |
  | `LEASE` | `LifecycleStoreError` |
  | Worktree recorded fields | `GitWorktreeCleanupError` (`WORKTREE_PHYSICAL`) and `GitWorktreeLifecycleError` (`WORKTREE_PUBLICATION`) |

  Anything else escaping a stage call is an unexpected failure, attributed to
  the stage whose call raised it.
- **H2.** An unexpected exception escaping `GitWorktree.__exit__` is attributed
  to `WORKTREE_PHYSICAL`; it cannot be attributed more finely from outside that
  call. `WORKTREE_PUBLICATION` is only ever the latched field.
- **Candidate order.**
  1. `WORKTREE_PHYSICAL`: the unexpected exception, then `cleanup_error`.
  2. `WORKTREE_PUBLICATION`: `lifecycle_error`.
  3. `RESERVATION`: the unexpected exception, then `cleanup_error` (or the
     declared error on the returned path).
  4. `LEASE`: the declared or unexpected exception.

  Recorded fields are read for every object that was constructed, including a
  worktree whose `__enter__` failed.
- **R8, attribution, which decides confirmation and every outcome.**
  - Immediately before an exit stage, its object's recorded fields are
    snapshotted by identity.
  - A recorded-field candidate is **O-owned** when it is unchanged by identity
    from the pre-call snapshot (or its stage never ran) and is reachable from
    the original exception. A snapshot compares identities: it cannot show
    whether code reassigned the same object, and the rule does not claim to.
  - Every other candidate is **attributed**. That includes every exception
    escaping a stage call, and any recorded field whose identity changed during
    the call.
  - `failed_stages`/`unconfirmed_stages` are the unique stages of the
    attributed candidates, in first-occurrence order.
- **D, reporting only.** D selects which instances appear in `failures`. It is
  iterative and cycle-safe:
  - it walks `__cause__` and `__context__` by object identity, with a visited-id
    set seeded from the original exception;
  - it excludes a candidate reachable from the original or from an earlier
    retained record, so the first occurrence in stage order wins;
  - it retains a later candidate that wraps an earlier one;
  - it treats a suppressed `__context__` as reachable, which is conservative.

  D never changes R8's results. A stage can therefore be unconfirmed with no
  record of its own, and a `None` error field never means confirmation.
- **R4, returned path.** Let A be the attributed candidates and F = D(A).

  | Condition | Outcome |
  |---|---|
  | No unexpected candidate | Return `LifecycleRunResult(finished, unconfirmed_stages, R, L)`. R and L come from F's retained `RESERVATION`/`LEASE` records. |
  | Exactly one candidate overall, an unexpected non-`Exception` `BaseException` | Re-raise it exactly. |
  | Otherwise | Raise `LifecycleRunCleanupError(finished, None, F, failed_stages)` from the first unexpected candidate. |

  An unexpected failure excluded by D still selects this row, so it is never
  silently swallowed.
- **R5, raise path.** With O as the original exception:
  - no attributed candidate: re-raise O exactly;
  - otherwise: raise `LifecycleRunCleanupError(None, O, D(candidates, O),
    failed_stages)` from O.

  This is the repository's cleanup-dominance convention (`_bounded_subprocess`,
  `LifecycleLease.__exit__`, `GitWorktree` enter-time cleanup), extended so
  that every unconfirmed stage remains represented in `failed_stages` and D
  retains each distinct reportable exception once. A deduplicated exception
  remains observable through the original exception or an earlier retained
  exception's chain. It includes converting a
  `KeyboardInterrupt` when cleanup is unconfirmed. On this path
  `GitWorktree.__exit__` may dispose, because no `RunFinished` was returned and
  the retry is idempotent and observation-confirmed.
- **R6.** A `BaseException` that lands after `RunFinished` is appended but
  before `run()` returns takes R5. The contract keys on `run()` returning.
- **R7, sanitization.** The message is a fixed prefix followed by
  `failed_stages` values. It never interpolates any retained exception, path,
  Git output or caller input. Retained instances are never mutated, re-chained
  or wrapped. No `ExceptionGroup` is used (Amendment 7 §4).

### 4. Evidence root

Before any reservation, the evidence root goes through the sink's own
unmodified checks:
- `_validate_no_symlink_ancestors`;
- bidirectional `_validate_containment` of the resolved root against the
  canonical source;
- the same containment check against the whole state root.

The state-root check is a superset of the deterministic worktree check, and it
keeps the artifact out of `runs/<id>/`. That matters because reconciliation's
inner-entry check refuses any unrecognized file there before it peeks at the
terminal state, so an artifact there would block even a `COMPLETE` entry. The
sink's later validation at capture is unchanged.

### 5. Types

- `CleanupStage`: `WORKTREE_PHYSICAL`, `WORKTREE_PUBLICATION`, `RESERVATION`,
  `LEASE`.
- `CleanupFailure(stage, exception, declared)`.
- `LifecycleRunResult(finished, unconfirmed_stages, reservation_release_error,
  lease_release_error)`, with `release_confirmed` as the authoritative check.
- `LifecycleRunCleanupError`, with `finished`, `original`, `failures` and
  `failed_stages`.

### 6. Failure table (what the next run sees)

| Failure | Outcome | Next run |
|---|---|---|
| Inside `prepare_lifecycle` | Raised | Ok, or blocked (the 3B-1 residual, a run directory without a projection). |
| Writer, publishers, evidence-root refusal, or reservation | O exactly, or a cleanup error from O; projection PREPARING and all absent | Reconciled. |
| `GitWorktree` init/enter | The enter error exactly, or a cleanup error from it | Reconciled (3B-1/A12/A13), or blocked if cleanup is unconfirmed. |
| Collaborator construction | Worktree disposed on R5 | Reconciled. |
| Ordinary `Exception`, or any terminal outcome from `run()` | Result; projection `COMPLETE` when teardown is confirmed | Ok. |
| Disposal failure or latched `absent` failure inside `_terminate()` | Result with `LIFECYCLE_CLEANUP_UNCONFIRMED`; never retried | A13 when no ref exists, otherwise blocked. |
| `BaseException` during `run()` | R5 | Blocked once a checkpoint ref exists (ADR 0005 gap). |

**Crash windows that block the next run:**
- between run-directory creation and the initial projection publish;
- after the leaf is reserved but before `creating`;
- a worktree together with any container;
- any non-absent checkpoint ref;
- Ctrl-C after the first patch;
- preserve paths.

**Crash windows that the next run reconciles:**
- PREPARING with everything absent;
- `creating`/`present` worktree alone;
- after ref deletion but before `COMPLETE`.

### 7. Non-claims

- **Threat model.** T-E1 is not mitigated: the repository lock covers only this
  internal path, and the legacy lifecycle-unaware path is unchanged. T-F1 and
  T-F2 are unchanged; tests pin blocking, not recovery.
- **Raise path.** Reservation and lease failures exist only in the raised error.
- **Lone `BaseException` after `RunFinished`.** When a lone `BaseException` is
  re-raised after `RunFinished`, the `RunFinished` is observable only through
  the caller's `event_log`.
- **Unguarded window.** Asynchronous delivery inside or between attempt steps
  is not guarded.
- **Same-user swaps.** These remain under A4.
- **Evidence.** This is implementation and automated-test evidence, not a
  security review.

### 8. Evidence

Local verification only: macOS, Git 2.54.0, Python 3.12.14, Docker already
running, `CODEAGENT_REQUIRE_DOCKER=1`. Nothing had been committed or pushed at
the time of this local verification. The Linux CI results, one failed run and
then one successful fix-forward run, are recorded at the end of this section.

**New test file.** `tests/integration/test_lifecycle_run.py` holds 46 named
specifications (T1–T45 plus T17b), which expand to 69 collected tests through
parametrization. The real-Docker set is exactly T30–T38, pinned by T41:

| Test | Behavior |
|---|---|
| T30 | Happy path: `COMPLETE`, every resource absent, evidence published. |
| T31 | A second run reports `SKIPPED_TERMINAL`. |
| T32 | A model error after baseline still ends `COMPLETE`. |
| T33 | A disposal failure is reconciled by A13 on the next run. |
| T34 | `KeyboardInterrupt` after a durable ref `PRESENT` blocks the next run. |
| T35 | SIGKILL after worktree `present` is reconciled. |
| T36 | SIGKILL after baseline `PRESENT` blocks. |
| T37 | SIGKILL after ref `PRESENT` blocks. |
| T38 | A construction failure is reconciled. |

The other 37 named specifications run without Docker. Every one of their
injected failures is a monkeypatched release or publication failure.

**Narrowed AST proofs.** The two `tests/unit/test_workspace.py` proofs that no
production module constructs `GitWorktree` are narrowed to "only
`_lifecycle_run.py`". This is a disclosed correction, not a weakening: the
narrower assertion is exact equality.

**Mutation checks.** Temporary mutations of the module were each caught by the
named tests and then reverted; the module is byte-identical afterwards:
- stages inside the handler (T19, T39);
- R4 deciding from D (T44, T6);
- exiting the worktree after `run()` (T1, T2);
- removing the O-owned rule (T16, T24);
- deriving `failed_stages` from D (T21, T45);
- taking R4's fields from raw candidates (T43).

**Totals.**

| Run | Result |
|---|---|
| New file plus `test_workspace.py` | 227 passed |
| 17-file focused set (1,640) plus the new file | 1,709 passed, forward and reverse |
| Full suite | 3,233 passed, 0 skipped (up from 3,164) |

No leftover CodeAgent containers, worktree registrations, `refs/codeagent` refs,
child processes, fixture directories or default state root remained.
`git diff --check` is clean.

**Linux CI result: failed.** Commit
`bd77411ee2f44bd5150202384070540c80326ed7`, run
[37138458659](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37138458659).

- **Run:** triggered by a push. Job "Test (ubuntu-24.04, Python 3.12)",
  Ubuntu 24.04.5 LTS, CPython 3.12.14. Conclusion `failure`.
- **Steps that succeeded:**
  - Docker preflight, with client and server `28.0.4`.
  - The pinned image, which reported `linux/amd64`.
  - The dedicated `python -m pytest tests/integration/test_slice_c.py -v`,
    `3 passed`.
  - The leftover-container check, whose output was empty.
- **Step that failed:** `python -m pytest -q`, run with
  `CODEAGENT_REQUIRE_DOCKER: 1`, reported `2 failed, 3228 passed, 3 skipped`.
  2 + 3,228 + 3 equals the local collected total of 3,233. `pytest -q` does not
  name the skipped tests.
- **The failing tests:** T30 and T31 both ended `PATCH_VALIDATION_FAILED`.
- **Cause:** post-patch verification ran as the fixed `--user 1000:1000`, and
  that user could not read the bind-mounted reserved leaf. The leaf is mode 0700
  and owned by the runner user. Verification therefore failed, and the repair
  iteration re-applied an already-applied patch.
- **Why local runs missed it:** macOS Docker Desktop masks mount permissions,
  so local runs passed.

This run is failed evidence, not confirmation. The fix is forward-only and is
recorded in Amendment 15.

**Linux CI after the fix-forward: success.** Amendment 15's ordinary
fast-forward commit `bdfeeece944f519fc17c6781a9e6bf773dd13667` (over
`bd77411`, with history unchanged) was confirmed by run
[37140167141](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37140167141).
The run was push-triggered and concluded `success`. The complete suite gave
`3241 passed, 3 skipped`, which equals the 3,244 collected locally, and the
T30/T31 failures above did not recur. Full details, and the limits on what the
log can show, are in Amendment 15's Evidence. This amendment's own composition
is therefore green on GitHub-hosted `ubuntu-24.04` only together with
Amendment 15.

## Amendment 15 (Accepted 2026-10-03): verification containers run as the effective host identity

**Context.** Amendment 14's Linux CI run 37138458659 failed (see Amendment 14
§8). Two invariants conflict:

- **The reserved leaf (A12/A13).** The deterministic worktree leaf is
  bind-mounted read-only as `/workspace`. It is mode exactly 0700 and owned by
  the effective host user.
- **The container identity (`DockerVerifier`).** Every verification container
  ran as a fixed `--user 1000:1000`.

On any native Linux host whose user is not uid 1000, the container cannot read
the mount.

A disposable Linux-semantics experiment (a named Docker volume) reproduced this.
uid 1000 against a 0700 leaf owned by uid 1001 failed with
`ModuleNotFoundError: No module named 'tests'`. uid 1001 against the same leaf,
and uid 1000 against a 0755 leaf, each reached only the fixture's genuine bug
assertion. The legacy path works only because its mount is a world-readable
Git-created subdirectory. The log does not print the runner's uid; that it is
not 1000 is inferred from this experiment and the failure pattern.

**Decision.**

- `DockerVerifier.__init__` computes `--user <os.geteuid()>:<os.getegid()>`
  once, through `_effective_container_user()`. Both `docker create` sites (the
  legacy path and the lifecycle-aware path) pass that value beside
  `_SECURITY_FLAGS`.
- The identity is the *effective* one because it is the identity that creates,
  and owns, the reserved leaf.
- Every other isolation flag is byte-identical, pinned by a unit test: network
  none, read-only root filesystem, tmpfs `/tmp`, memory and swap caps, CPU and
  PID limits, `--cap-drop ALL`, `no-new-privileges`.
- The 0700 leaf, its layout, and A12/A13 attribution are unchanged.
- **Fails closed, with no fallback to `1000:1000`:**
  - A platform without POSIX `os.geteuid`/`os.getegid` is refused at
    construction, before any Docker call.
  - An effective uid of 0 is refused at construction
    (`"verification containers refuse effective uid 0"`). Without that, running
    CodeAgent as root would make the formerly non-root container process root,
    which weakens the sandbox (CLAUDE.md rule 6).
  - An effective gid of 0 is refused separately
    (`"verification containers refuse effective gid 0 (the root group)"`). With a
    nonzero uid this does not make the process root, but it would give the
    process the root-group identity, which the fixed `1000:1000` never had. The
    uid check runs first.

**Consequences.**

- **Container process identity.** The process runs with the host user's
  numeric uid and gid. Docker user-namespace remapping is not assumed, so a
  container escape would act as the host user. Under the fixed `1000:1000` it
  would instead have acted as whatever account owns uid 1000 on that host, which
  on a multi-user machine may be a different real user. This changes who the
  container's identity maps to on the host. It is not claimed to be a stronger
  boundary.
- **Mounts and writes.** The only bind mount stays read-only. Writable space is
  still only the tmpfs.
- **S4 spike evidence.** Its recorded production configuration
  (`spikes/s4/S4_RESULT.md`) and its spike helpers
  (`flags_without("--user")`) describe the pre-Amendment-15 tuple. They are
  historical and left unchanged. The non-root property they tested still holds,
  but rerunning S4 would need a spike-side update. That is a named residual. The
  S4 workflows are manual-only and never part of push CI.
- **Unsupported invocations.** Windows hosts, effective uid 0, and effective
  gid 0 are refused rather than run.

**Evidence.**

Local verification: macOS, Docker already running,
`CODEAGENT_REQUIRE_DOCKER=1`.

**Confirmed on GitHub-hosted Linux CI.** Commit
`bdfeeece944f519fc17c6781a9e6bf773dd13667`, run
[37140167141](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37140167141).

- **Run.** Push-triggered, status `completed`, conclusion `success`. The API
  reports 13 steps, every one `success`.
- **Environment.** Job "Test (ubuntu-24.04, Python 3.12)": runner image
  `ubuntu-24.04` (Ubuntu 24.04.5 LTS) x86_64, CPython 3.12.14. The Docker
  preflight reported client and server `28.0.4`.
- **Image.** The pinned
  `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`,
  which reported `linux/amd64`.
- **Dedicated Docker step.** It ran
  `python -m pytest tests/integration/test_slice_c.py -v`, with
  `CODEAGENT_REQUIRE_DOCKER: 1` in that step's environment, and gave
  `4 passed in 2.73s`. Its verbose output names
  `test_real_docker_reads_a_private_0700_mount_as_the_effective_host_identity`
  as `PASSED`. That is direct log evidence that the legacy-path regression
  passed on Linux.
- **Complete suite.** It ran `python -m pytest -q`, with
  `CODEAGENT_REQUIRE_DOCKER: 1` in that step's own environment, and gave
  `3241 passed, 3 skipped in 82.07s`. 3,241 + 3 = 3,244, which matches the
  local collected total. The T30/T31 failures of run 37138458659 did not recur.
- **Limits of the suite evidence.** `pytest -q` prints no test names, so the
  three skips are not identified, and there is no named-test log evidence for
  T30, T31, or T46. What the log supports:
  - no test failed;
  - Docker was required and available in that step;
  - all three tests are collected from `tests/integration/test_lifecycle_run.py`.

  That each one actually ran, rather than skipped, is an inference.
- **Leftover-container check.** It applied the anchored
  `grep -E '^codeagent-(verify-|baseline-|verification-)'` to a full
  `docker ps -a --format '{{.Names}}'` listing. That covers the legacy
  `codeagent-verify-*` family and both deterministic lifecycle families. The
  output was empty and the step succeeded.

**Residual risks.**

- **Runner uid not in the log.** The runner's uid is not printed in either run's
  log. The uid mismatch is supported by the source and by the experiment above,
  not by direct log evidence.
- **S4 helpers.** S4's historical spike helpers still assume the old fixed-user
  tuple.
- **Escape mapping.** A container escape now maps to the invoking host user
  rather than the fixed uid 1000.
- **Crash and reconciliation gaps.** The existing gaps (Amendment 14 §6) remain.
- **Scope of the evidence.** This covers GitHub-hosted `ubuntu-24.04` x86_64
  only. It is automated-test evidence, not a security review.

The local evidence follows.

- **New unit tests** in `tests/unit/test_executor.py`, 9 cases:
  - the exact remaining flag tuple;
  - the real effective identity;
  - both create paths passing a distinctive fake effective identity;
  - an unsupported platform for each missing function;
  - three uid-0/gid-0 cases, each pinning its exact message.
- **Two new real-Docker regressions,** each with a 0700 mount root:
  - `test_slice_c.py::test_real_docker_reads_a_private_0700_mount_as_the_effective_host_identity`,
    on the legacy path. It runs in CI's dedicated Docker step.
  - `test_lifecycle_run.py::test_t46_post_patch_verification_reads_the_private_0700_leaf`,
    which uses the composition. It records leaf mode 0700 at the durable
    `present` publish, and post-patch verification must pass.

  Each container command asserts `id -u:id -g` equals the effective host
  identity before importing or running the fixture tests. That makes both tests
  load-bearing even where mount permissions are masked.
- **Mutation check.** With the identity temporarily forced back to `1000:1000`,
  both regressions failed locally (`TEST_FAILURE`, and `BUDGET_EXCEEDED`). T30
  still passed locally under that mutation, which is exactly why the original
  defect escaped local verification. The executor was then restored
  byte-for-byte.
- **Totals.**

  | Run | Result |
  |---|---|
  | `test_executor.py` | 142 passed |
  | 17-file focused set plus `test_lifecycle_run.py` and `test_slice_c.py` | 1,723 passed, forward and reverse |
  | Full suite | 3,244 passed, 0 skipped (up from 3,233) |

  No leftover resources remained.

This is implementation and test evidence, not a security review.

## Amendment 16 (Accepted 2026-10-03): reconciling a dead checkpoint ref whose worktree and containers are already absent

**Scope.** Milestone 3 Slice 3C-5. This amendment implements §8's
checkpoint-ref reconciliation table for one shape only:

- the state is `PREPARING`, `ACTIVE`, `CLEANING` or `RECONCILING`;
- the checkpoint-ref record is `creating`, `present`, `advancing` or `removing`;
- the worktree record is `absent`;
- both container records are `absent`;
- `failure` is null.

The shape check is `lifecycle_store.is_projection_checkpoint_ref_reconciliation_shape`.

**Not covered.** A ref alongside a worktree record (§9 and I5 require the
worktree first) or a container record. Nothing is wired to a CLI or controller
entry point.

### 1. Deletion candidates

`lifecycle_store.checkpoint_ref_deletion_candidates` is the single source of
§8's table, shared by the reconciler and its writer.

| Persisted intent | Candidates (role) |
|---|---|
| `creating` | `proposed_new_sha` (`proposed`) |
| `present` | `accepted_sha` (`accepted`) |
| `advancing` | `accepted_sha` (`accepted`), then `proposed_new_sha` (`proposed`) |
| `removing` | `accepted_sha` (`accepted`) |

The comparison value is always the observed live SHA, and only when it is a
candidate.

### 2. Order

Every inspection happens before any mutation, in §9's order.

**Inspections:**

1. **Containers.** A fresh container listing must show both deterministic names
   absent.
2. **Worktree, which must be confirmed absent (I5).**
   - The exact path must be unregistered in a fresh Git listing.
   - The bounded Git admin-entry scan must find none.
   - The deterministic leaf name must be observed absent by Amendment 13's
     descriptor-relative, no-follow observer
     (`StateRoot.observe_materialized_worktree_leaf`). Only a genuinely missing
     name counts as absence; any other inspection error is
     `SUBSTRATE_UNAVAILABLE`, never absence.
3. **Object format.** The discovered object format must equal the trusted
   identity's.
4. **The owned ref.** It is observed without following a symbolic ref.

**Mutations:**

1. Enter `RECONCILING`. `attempts_total` is incremented only when this is a
   fresh cycle.
2. Publish the reconciler-owned write-ahead record `removing(observed)`. This
   is a no-op when the record is already `removing` at that SHA.
3. Delete with exactly one `CheckpointRef.delete(expected_oid=observed)`. This
   is the existing compare-and-swap `update-ref --stdin` transaction with
   no-deref, which re-observes while holding Git's lock and has hooks disabled.
4. Publish the reconciler-owned `absent`, only after the delete returns
   normally.
5. Publish `RECONCILED`, last.

If the ref was already absent at observation, step 1 is followed directly by
steps 4 and 5, with no Git mutation.

**Reconciler-only edges** (`_publish_reconciler_checkpoint_ref_transition`;
the live owner's table is unchanged):

- `creating(P) → removing(P)`;
- `present(A) → removing(A)`;
- `advancing(A,B) → removing(A | B)`;
- `removing(F) → removing(F)`, a no-op that publishes nothing;
- any of the four non-absent intents `→ absent`.

All of these require `RECONCILING` and an unchanged `attempts_total`.

### 3. Outcomes

| Observation or result | Outcome | Mutation |
|---|---|---|
| A container name is present | `REFUSED` | none |
| The container listing failed | `SUBSTRATE_UNAVAILABLE` | none |
| The worktree is registered, an admin entry exists, or a leaf entry exists | `REFUSED` | none |
| The worktree listing, admin scan, or leaf inspection failed | `SUBSTRATE_UNAVAILABLE` | none |
| The object format disagrees | `REFUSED` | none |
| The ref is symbolic (dangling or resolvable) | `REFUSED` | none |
| The ref is an unrelated direct SHA | `REFUSED` | none |
| The ref observation failed, timed out, or was ambiguous | `SUBSTRATE_UNAVAILABLE` | none |
| The ref is absent | collapse to `absent`, then `RECONCILED` | projection only |
| The ref is at a candidate SHA | delete, then `RECONCILED` | ref deleted |
| Delete `UNCHANGED` (including a compare-and-swap rejection) | `FAILED`; `removing(observed)` stays installed | none confirmed |
| Delete `UNEXPECTED` (the ref moved or vanished during the call) | `REFUSED` | none |
| Delete `SYMBOLIC` | `REFUSED` | none |
| Delete `UNKNOWN`, `TRANSACTION_CLEANUP_UNCONFIRMED`, or an error claiming `APPLIED` | `SUBSTRATE_UNAVAILABLE` | unknown |
| Any projection write failed or was unconfirmed | `FAILED`, with no later mutation | as installed |

### 4. Resume

Every pass reloads the projection and inspects from scratch.

- **Installed `removing(X)`, ref still at X.** The pass deletes without a
  second write-ahead record and without incrementing.
- **Installed `removing(X)`, ref absent.** It collapses to `absent`.
- **Unconfirmed write-ahead record.** Either record that may be installed
  leads to the same SHA.
- **Unconfirmed final `RECONCILED`.** It is already clean final, so it is
  `SKIPPED_TERMINAL` (I11).

### 5. Disclosed corrections

- **Dangling symbolic ref in `CheckpointRef.observe()`.** It reported a
  *dangling* symbolic ref as absent, because `git for-each-ref` silently skips
  broken refs (confirmed on Git 2.54). When the listing is empty it now probes
  `git symbolic-ref --quiet <ref>`, using the same hardened, time-bounded Git
  runner:

  | `symbolic-ref --quiet` exit | Result |
  |---|---|
  | 0 | `SYMBOLIC_REF` |
  | 1 | absent |
  | anything else | `OBSERVATION_FAILED` |

  This also closes a real owner-path hole. `create()`'s pre-check saw a
  dangling symbolic ref as absent, and `update-ref --no-deref` would then
  rewrite the owned name. ADR 0003 Amendment 1 already requires a symbolic ref
  to be refused.
- **`reconciliation._observe_checkpoint_ref_absent`.** It now maps a symbolic
  ref to `REFUSED`, per §5, instead of `SUBSTRATE_UNAVAILABLE`. Other
  observation failures stay `SUBSTRATE_UNAVAILABLE`.
- **Joint-review correction: the leaf check.** The first implementation
  checked the leaf with `os.path.lexists()`, which returns `False` on *any*
  `OSError`. With the leaf present but its parent unreadable (`EACCES`), it
  reported absence, and the row deleted the ref, contrary to I5. A temporary
  mutation that restored `lexists` produced `RECONCILED`. The row now uses the
  descriptor-relative observer, and a load-bearing regression pins
  `SUBSTRATE_UNAVAILABLE` with zero mutation.
- **T34 in `tests/integration/test_lifecycle_run.py`.** It previously pinned
  the Ctrl-C-after-patch shape as `BLOCKED`. It now asserts `RECONCILED`,
  because that shape (worktree disposed on the raise path, containers absent,
  ref `present`) is exactly this row.

### 6. Maintenance trace

The trace's `checkpoint_ref` object gains categorical fields only; there is no
SHA anywhere, and `schema_version` stays 1:

- `initial_persisted_intent`;
- `observation_pre`, one of `absent`, `candidate_accepted`,
  `candidate_proposed`, `unexpected`, `symbolic`, `ambiguous`, `unknown`;
- `candidate_role`;
- `removal_attempt`, one of `not_attempted`, `applied`, `unchanged`,
  `unexpected`, `symbolic`, `unknown`;
- `removing_transition_confirmed_this_pass`;
- `absent_transition_confirmed_this_pass`.

### 7. Non-claims

- **Shapes that still block admission.** A ref alongside a worktree (T37's
  SIGKILL shape) or a live container still blocks admission (T36), as do
  abandonment-only cases.
- **Unreachable commits.** Deleting the ref leaves its checkpoint commits
  unreachable, and Git's garbage collection may remove them.
- **Unbounded output.** `observe()` output is bounded only by matching one
  exact name.
- **Same-user races (A4).** These are not closed.
  - **Observed at a candidate SHA.** If the ref moves between the observation
    and the delete, the compare-and-swap refuses it and the moved ref survives.
  - **Observed absent.** No compare-and-swap is involved. A ref created by
    another same-user process after that observation is not detected, and the
    record still collapses to `absent`.
  - **Within the observation itself.** It is two Git commands (`for-each-ref`,
    then `symbolic-ref --quiet`), so a change between them can go unseen.
  - **Owner mutations** stay protected by Git's own compare-and-swap.
  - **Probe resources.** The probe runs through the primitive's existing
    hardened Git runner: no `GIT_*` environment, hooks disabled, a structured
    argv, a 30-second timeout. Its output is captured whole, as `for-each-ref`'s
    already was, rather than through the byte-bounded subprocess runner.
- **Threats.** T-E1, T-F1 and T-F2 are not newly mitigated. T-M1 gains
  reconciler-level coverage for these shapes only.
- **Evidence.** This is implementation and automated-test evidence, not a
  security review.

### 8. Evidence

Local verification only: macOS, Git 2.54.0, Docker already running (it was not
started or restarted), `CODEAGENT_REQUIRE_DOCKER=1`. Linux CI is pending.

**New tests: 125.**
- `test_checkpoint_ref.py`, 6: dangling refs on SHA-1 and SHA-256; mutations
  refusing a dangling ref and leaving it untouched; a truly absent ref; a
  failed probe; the probe running only on an empty listing.
- `test_lifecycle_store.py`, 54: the shape truth table; candidates; every legal
  and illegal reconciler edge at both SHA lengths; the no-op; the state and
  attempt preconditions; publication-failure classification; the unchanged
  owner table.
- `test_reconciliation.py`, 64:
  - every intent × live value, both `advancing` outcomes, and dangling and
    resolvable symbolic refs;
  - a real SHA-256 repository;
  - an ambiguous observation and a failed observation;
  - an object-format mismatch;
  - every delete `MutationOutcome`, including unconfirmed transaction cleanup;
  - real compare-and-swap races (moved and deleted);
  - a held lifecycle lock;
  - blockers, with zero mutation;
  - ineligible shapes;
  - every write failing or unconfirmed at each of the 4 writes, plus resume;
  - two real reconciler SIGKILLs (after the write-ahead `removing`, and after
    the delete);
  - byte-identical unrelated refs;
  - the observer correction;
  - an unreadable leaf parent, never treated as absence (joint-review
    regression).
- `test_lifecycle_run.py`, 1: T47, an owner SIGKILL in teardown after the
  worktree `absent` is published, which is then reconciled (real Docker). T34
  was corrected. T36 and T37 still block.

**Mutation checks.** Ten distinct mutations, each caught by its named tests:

- **Wrong deletion candidates:**
  - `advancing` dropping `proposed`;
  - the writer accepting any SHA (run twice: once before and once after a
    test-helper rename).
- **The compare-and-swap replaced by a plain delete.** The moved-ref race test
  catches it: a moved ref would have been deleted.
- **Symbolic handling:**
  - the probe removed;
  - the row treating a symbolic ref as absent;
  - the observer correction reverted.
- **Unsafe ordering:**
  - deleting before the write-ahead record;
  - skipping the worktree check;
  - skipping the container check;
  - the leaf observed with `os.path.lexists()` (joint review: this produced
    `RECONCILED`, deleting the ref).

The sources were restored byte-for-byte afterwards.

**Totals.**

| Run | Result |
|---|---|
| Targeted four files (`test_checkpoint_ref`, `test_lifecycle_store`, `test_reconciliation`, `test_lifecycle_run`) | 908 passed, forward and reverse |
| Focused Milestone 3 set (17 files plus `test_lifecycle_run.py` and `test_slice_c.py`) | 1,848 passed, forward and reverse |
| Full suite | 3,369 passed, 0 skipped (up from 3,244) |

No leftover containers, worktrees, `refs/codeagent` refs, Git admin entries,
processes, fixture directories, Docker volumes or default state root remained.
