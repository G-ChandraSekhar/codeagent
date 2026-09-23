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
suite step passed 2,177 with 3 skipped (those 3 are exactly the
real-Docker tests already exercised for real in the dedicated step,
not a Docker-unavailability skip), and no leftover
`codeagent-verify` containers remained afterward. This is
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 claim. Slice 3A-2 now implements the lifecycle-lock wrapper and
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
complete-suite step passed 2,177 with 3 skipped (exactly the
real-Docker tests already exercised for real in the dedicated step
immediately before it, not a Docker-unavailability skip); no leftover
`codeagent-verify` containers remained afterward. This is
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
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
