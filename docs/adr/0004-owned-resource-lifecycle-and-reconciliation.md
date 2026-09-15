# ADR 0004: Owned-resource lifecycle, registry, attribution, cleanup, and reconciliation

Status: Accepted (2026-09-15)

Implementation status: **not implemented.** No mechanism in this ADR
exists in production code yet. See "Implementation order" below.

## Context

Every CodeAgent run creates host resources that can outlive the process
that created them: a disposable Git worktree, verification containers,
and — once ADR 0003's pending amendment adopts it — a hidden checkpoint
ref. An uncatchable SIGKILL or a crash prevents any in-process cleanup
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
  directory path, and the `st_dev`/`st_ino` of the **opened** Git
  common directory.
- Creation: only when `repos/<repo-key>/` and `worktrees/<repo-key>/`
  contain no state; created with `O_EXCL`, `fsync`ed.
- Validation on every use: the key, canonical path, `st_dev`, and
  `st_ino` must all match the freshly opened common directory. Any
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
  `checkpoint_ref {intent, sha}`; `failure {phase, detail}` or null
  (fixed, sanitized strings); `reconciliation {attempts_total,
  recent_failures}`.
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
| ID null, `creating` | Name present, all four labels match | Owned: remove (observed ID recorded in the maintenance trace first) |
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

Checkpoint ref `refs/codeagent/runs/<lifecycle-id>/checkpoint` (the
reachability semantics belong to ADR 0003's pending amendment; the
attribution and reconciliation rules below apply once it is adopted):

- Ref missing: confirmed absent.
- Ref value equals the recorded SHA: delete with compare-and-swap
  (`git update-ref --no-deref -d <ref> <expected>`) and confirm missing.
- Any other value, or a symbolic ref: `REFUSED`.
- Intent `absent` while the ref exists: `REFUSED`.
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
  dead-run reconciliation, with both locks held, the worktree
  confirmed absent, and a compare-and-swap on the recorded SHA.
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
container, worktree, and ref tables; a canary suite (unlabeled and
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

Nothing here is implemented. Milestone 2 comes first (its first
documentation task is ADR 0003's checkpoint-ref amendment); the
mechanisms in this ADR are implemented afterwards as Milestone 3
lifecycle work. See `CLAUDE.md` and `ENGINEERING_LOG.md`.
