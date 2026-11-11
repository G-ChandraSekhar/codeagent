# ADR 0003: Recover partial multi-file patches by replacing the worktree, not by per-file rollback

Status: Accepted. Amended 2026-09-15 by Amendment 1 (Accepted): durable
checkpoint reachability through hidden Git refs. Amended 2026-09-17 by
Amendment 2 (Accepted): lazy checkpoint establishment, durable evidence
capture, gated teardown ordering, and terminal precedence — see the end
of this document. Everything above "Amendment 1" is the original
decision, unchanged; Amendment 1 is unchanged by Amendment 2 except
where Amendment 2 explicitly narrows its point 4 ("establish, then
rely") ordering.

Amendment 3 (ledger D5, slice D5-C: the four-operation transactional patch
protocol, N1 and N2): **Accepted 2026-11-10, after joint review; not implemented.** (Historical:
Proposed 2026-11-09.) It also holds the D5-C decision ownership map. See "Amendment 3"
at the end of this document.

## Context

Stage 2 spike S3 (`spikes/s3/spike_s3.py`, full evidence and analysis
in `spikes/s3/S3_RESULT.md`) ran three independent experiments against
disposable scratch Git worktrees to answer whether CodeAgent's future
multi-file patch mechanism needs one atomicity guarantee or several.
It found three genuinely different guarantees, not one:

- **Prevalidation atomicity is real.** A 3-operation proposal with one
  validation-detectable error (op 3's expected text absent from its
  target file) was rejected in full before any file was touched — the
  worktree was byte-for-byte, HEAD-for-HEAD identical before and after
  rejection.
- **A handled mid-application failure produced a genuinely observable
  partial state before recovery ran.** After op 1's write landed and a
  deterministic caught exception stood in for a real mid-apply error,
  the worktree was already inconsistent — one file patched, two not,
  an uncommitted `M a.py` in `git status` — for the entire window
  before any recovery code executed. An in-place rollback
  (`git checkout -- a.py`) then did restore the worktree exactly to
  its pre-apply state in this run, but this restoration was
  demonstrated only for **one previously-tracked, previously-clean
  file** — new-file, deletion, rename, and rollback-command-failure
  cases were never exercised.
- **A hard SIGKILL produced a partial worktree that was distinguishable
  from both "fully original" and "fully applied," but only by
  convention.** One of three files ended up patched, the other two
  untouched, no commit was ever made. Detecting this relies entirely
  on a caller knowing the rule "a completed apply always ends in a new
  commit and a clean tree" — nothing in the naive apply path writes an
  explicit transaction marker, lock file, or atomic rename to make
  that structurally true.

These three findings do not support one combined "patch atomicity"
claim. They support three separate, narrower claims, and the design
this ADR records treats them as such.

## Decision

For Milestone 2's future multi-file patch mechanism:

1. **Validate the complete multi-operation proposal before any write.**
   Every operation is checked against the current worktree state
   up front; if any operation fails validation, nothing in the
   proposal is applied. (This is S3 experiment 1's demonstrated
   guarantee, carried forward unchanged.)
2. **If a handled failure occurs after mutation has begun, invalidate
   the entire disposable worktree.** Do not attempt to reason about
   which files were touched and revert them individually as the
   primary correctness mechanism.
3. **Per-file in-place rollback (`git checkout --`) is not the
   correctness foundation**, even though S3 experiment 2 showed it can
   work for the single-tracked-clean-file case it was tested against.
   It is not adopted as the mechanism v1 relies on for correctness.
4. **After a handled failure, always confirm disposal of the
   contaminated worktree** — regardless of what happens next. Disposal
   is not conditional on whether the run will continue.
5. **Recreate a clean worktree from the last accepted checkpoint only
   if the run will continue** (repair loop or retry). If the run is
   terminating permanently (e.g. plan rejected, budget exceeded,
   unrecoverable error), there is no next attempt to recreate a
   worktree for, and none is created.
6. **Require expected HEAD and a clean working tree/index at two
   specific points only: immediately before beginning a new patch
   attempt, and immediately before resuming after an interruption.**
   This is an entry/resume-time gate, not a continuous invariant — a
   patch attempt that is actively in progress is *expected* to leave
   the tree dirty partway through (op 1 written, op 2 not yet) and
   that is normal, not a violation. The check exists to answer one
   question at one specific moment ("is it safe to start or resume
   here?"), not to assert anything about the tree's state while a
   transaction is actively running.

   At that moment, a dirty tree, an unexpected HEAD, or any other sign
   of an incomplete transaction is treated as **interrupted or
   contaminated** — such a worktree must never be resumed or trusted,
   only discarded (per point 4) and, if the run continues, recreated
   (per point 5).
7. **If disposal cannot be confirmed, or — when recreation is
   attempted — recreation cannot be confirmed, the run terminates with
   a structured operational error** rather than continuing on an
   unverified worktree.
8. **True crash-consistent filesystem mutation is explicitly not
   guaranteed in v1.** The claim this design makes is **recovery plus
   interruption detection/rejection at the controller boundary** — not
   all-or-nothing visibility of the write operations themselves during
   the window they're happening. S3 experiment 3's own name was
   corrected from "crash consistency" to "interruption detection" for
   exactly this reason (see `S3_RESULT.md`), and that correction is
   adopted here as the accurate description of what this design
   provides.

This is spike S3's own option **B2 ("abandon-and-recreate")** combined
with option **C ("checked interruption-detection convention")**, as
laid out in `S3_RESULT.md`'s decision table — accepted here as the v1
direction.

## Consequences

- **Simpler reasoning for add/update/delete/rename than individual
  inverse operations.** Discarding and recreating a worktree has one
  failure shape (disposal/recreation itself failing) instead of a
  distinct inverse operation to design, implement, and validate for
  every operation kind a proposal might contain.
- **This design relies on worktree disposal and recreation themselves
  being reliable.** Neither was stress-tested by S3 — both are new
  obligations, not confirmed guarantees.
- **Uncheckpointed work inside an invalidated worktree is lost by
  design**, not preserved and reconciled. This is an accepted
  trade-off, not an oversight: it is simpler and more auditable than
  attempting partial reconciliation, at the cost of discarding
  in-progress work that was never checkpointed.
- **Milestone 2 must add dedicated failure-path tests** for: worktree
  disposal failure, worktree recreation failure, proposals that add
  files, proposals that delete files, proposals that rename files, and
  a worktree found in an unexpected dirty/index state at resume time.
  None of these are covered by S3's evidence — S3 exercised only
  content-replacement operations on already-tracked, already-clean
  files.
- **A hard crash may leave partial, uncommitted state on disk until
  the recovery/checkpoint-verification step described above later
  finds and rejects or discards it.** There is a real window, of
  unbounded duration until the next resume/verification check, during
  which contaminated state exists on disk. This design detects and
  discards that state on the next trusted access; it does not prevent
  the state from existing.
- **This must never be marketed or documented as true crash
  consistency.** Any future documentation, ADR, or product-facing
  claim must describe this as recovery plus interruption detection at
  the controller boundary, matching the corrected terminology in
  `spikes/s3/S3_RESULT.md`.

## Rejected alternatives

- **A — prevalidation only, no recovery, no interruption handling.**
  Insufficient on its own: a handled failure or a hard interruption
  would leave an undocumented, silently partial worktree with no
  mechanism to detect or recover from it.
- **B1 — per-file `git checkout --` rollback as the primary
  correctness strategy.** Demonstrated to work for one
  previously-tracked, previously-clean file, but not selected as the
  primary mechanism: it has more distinct failure modes to validate
  (new files with nothing to check out to, a rollback command that
  itself fails or is itself interrupted) than B2's single
  discard-and-recreate primitive, and none of those modes were
  evidenced by S3.
- **D — true filesystem crash consistency** (write-to-scratch-then-
  atomic-rename per file, single all-or-nothing commit). Would be the
  only option actually justifying the name "crash consistency," but
  is deferred beyond v1 due to its implementation complexity (scratch
  file management, `os.replace` ordering, new failure modes of its
  own) and because it is out of this milestone's scope. Revisit only
  if a concrete operational scenario (long-running unattended runs,
  unreliable host infrastructure) makes surviving a host-process crash
  mid-patch a realistic requirement rather than a theoretical one.

## Evidence

`spikes/s3/S3_RESULT.md`, and its retained machine-readable evidence
(`spikes/s3/result1.json`, `result2.json`, `result3.json`,
`summary.json`, `host.json`, `baseline-worktrees.txt`,
`final-worktrees.txt`, `run.log`) — one retained run, all three
experiments' classifications preserved unchanged by this decision.

---

## Amendment 1 (Accepted 2026-09-15): durable checkpoint reachability through hidden Git refs

Implementation status: **not implemented.** Nothing in this amendment
exists in production code yet.

### Context

The original decision recreates a worktree "from the last accepted
checkpoint" (points 5 and 6) but does not say what keeps that
checkpoint commit reachable once the contaminated worktree is
discarded. In current production code (`src/codeagent/patch.py`,
commit `567db62`) a checkpoint is an ordinary commit on the disposable
worktree's detached `HEAD`; no ref points at it. Git keeps commits
reachable from every worktree's `HEAD`, so a checkpoint is protected
only while its worktree exists. After the worktree is removed, the
commit is unreachable and can be pruned by `git gc` — recreation would
depend on garbage-collection timing, not on a structural guarantee.

`src/codeagent/controller.py` currently emits
`ToolCompleted(success=True)`, `CheckpointCreated`, and `PatchApplied`
as soon as the patch applier returns a commit hash, and runs its
approved-path postcondition afterwards. This amendment therefore also
defines when a checkpoint counts as **accepted**, and when the patch
tool call counts as successful.

This finding comes from reading the code, not from spike evidence:
neither S3 nor S5 exercised refs, and S5's retained evidence does not
cover this mechanism. ADR 0004 (Accepted) already names this ref as an
owned resource and defines its dead-run reconciliation.

### Decision

1. **One hidden ref per lifecycle.** Each lifecycle owns exactly one
   ref: `refs/codeagent/runs/<lifecycle_id>/checkpoint`.
2. **Identity.** `lifecycle_id` is ADR 0004's internal, validated
   128-bit lowercase-hex identity (`^[0-9a-f]{32}$`). The public
   `run_id` is unchanged and never appears in the ref name. The ref
   name is always recomputed from the validated `lifecycle_id`; it is
   never read from a stored path, a worktree's `.git` file, or a ref
   listing.
3. **The ref points to the latest accepted checkpoint** and changes
   only through compare-and-swap:
   - **Create** against the zero OID:
     `git update-ref --no-deref <ref> <initial-sha> <zero-oid>`.
   - **Advance** with the previously recorded checkpoint SHA as the
     expected old value:
     `git update-ref --no-deref <ref> <new-sha> <expected-old-sha>`.
   - **Delete** against the expected final SHA:
     `git update-ref --no-deref -d <ref> <expected-final-sha>`.
   - Every invocation uses structured argv (never a shell) and runs
     against the trusted source repository
     (`git -C <trusted source repository> ...`), never through a
     worktree's `.git` file.
   - Before each operation the ref is observed exactly: a symbolic ref,
     a value other than the one expected, or a failed observation fails
     closed. A failed compare-and-swap is never retried with a refreshed
     expected value.
   - **No SHA-1 assumption.** The trusted repository's Git object
     format is determined and validated first (ADR 0004 §4). The zero
     OID is the all-zero object ID of that format. Every SHA used or
     recorded is validated against that format before use.
4. **Establish, then rely.** The initial ref is created at the
   worktree's starting commit (today's `RunConfig.initial_checkpoint_id`)
   after the worktree is confirmed and before any patch attempt, so
   recreation never depends on an unpinned commit. A newly created patch
   commit is **not accepted** until the ref has advanced to it
   successfully. Every acceptance check (patch validation, the commit
   itself, and the approved-path postcondition) completes before the
   advance. A commit whose checks or ref advance fail is a handled
   failure under original points 2, 4, and 5, never an accepted
   checkpoint.

   **Patch event ordering (decided).** One `apply_patch` call is one
   transaction: validation, mutation, commit, approved-path
   postcondition, and checkpoint-ref advance.
   - `ToolCompleted(success=True)` is delayed until that whole
     transaction is accepted, and is followed by `CheckpointCreated` and
     then `PatchApplied`.
   - A failure after mutation began (commit failure, approved-path
     violation, or failed ref advance) emits
     `ToolCompleted(success=False)` with that occurrence's single
     structured error, and **no** `CheckpointCreated` or `PatchApplied`.
   - **One failure occurrence has exactly one `OperationalError`.**
     Every event representing that occurrence — `ToolCompleted`,
     `RunFinished`, and any other — carries the identical `error_id`,
     `ErrorCode`, and sanitized message. An occurrence is never
     translated into different codes on different events.
   - The code is the single most precise taxonomy code for the actual
     failure. `PATCH_APPLICATION_FAILED` is used only when it truthfully
     describes the failure.
   - An approved-path postcondition failure may keep
     `INTERNAL_INVARIANT_VIOLATION` if that remains the chosen precise
     code. `ToolCompleted`'s `apply_patch` error-code legality must then
     permit it: today's contract does not.
   - A checkpoint-ref observation, update, or confirmation failure
     receives one precise code selected during implementation, and that
     same error propagates through every event representing the
     occurrence. A terminal ref-deletion failure is ADR 0004 §13's
     cleanup-unconfirmed occurrence and likewise carries exactly one
     code.
   - Where the current `ToolCompleted` validator does not permit the
     precise code, Milestone 2 extends that validator's `apply_patch`
     error-code legality, rather than translating the occurrence into a
     second code. Exact new enum names, if any are required, are fixed
     during implementation and exhaustively pinned in the `errors` and
     `events` tests.
5. **Interrupted acceptance.** An interruption after a commit is created
   but before the ref advances leaves that commit unaccepted.
   - **Handled failure inside a live run:** any continuation recreates
     from the previously pinned checkpoint.
   - **Process death:** v1 never resumes a run whose owner died
     (original point 6, ADR 0004). ADR 0004 dead-run reconciliation
     deletes the ref and never uses the unaccepted commit.
   - **Durable write-ahead (ADR 0004 §5 and §8, Milestone 3):** before
     each create, advance, or delete, the lifecycle projection records a
     `checkpoint_ref` transition record (`intent`, `accepted_sha`,
     `expected_old_sha`, `proposed_new_sha`). It collapses to the stable
     `present` or `absent` state only after confirmed success. A dead
     run's exact ref may be deleted only when its live value is absent
     or equals a valid candidate for the persisted intent; a symbolic or
     unexpected value is refused.
6. **Intermediate discard-and-recreate (live run).**
   1. Confirm no container for the lifecycle exists.
   2. Discard the contaminated worktree and confirm disposal.
   3. **Keep the checkpoint ref.**
   4. Only if the run continues, recreate the worktree from the pinned
      checkpoint (the ref's current value, which must equal the SHA the
      run expects).

   The entry/resume gate from original point 6 checks that the
   worktree's `HEAD` equals that pinned value. If the ref itself is
   missing, symbolic, or unexpectedly changed, the run is not recreated
   from it: it terminates with a structured error.
7. **Terminal teardown, and ADR 0004 dead-run reconciliation.**
   1. Export the required diff and report evidence first (the diff is
      computed from the starting commit to the final accepted
      checkpoint).
   2. Confirm no container for the lifecycle exists.
   3. Remove the worktree and confirm it is gone.
   4. Compare-and-swap delete the checkpoint ref and confirm it is
      absent.

   Each step must be confirmed before the next begins. A deletion that
   fails or cannot be confirmed prevents a clean terminal result: under
   ADR 0004 §13 the run ends as `UNRECOVERABLE_ERROR`.
8. **No sweeping.** CodeAgent never sweeps or bulk-deletes
   `refs/codeagent/`. Discovery may report unexpected refs under that
   namespace; mutation targets only the exact recomputed ref of the
   lifecycle being torn down or reconciled.
9. **Hidden refs intentionally write shared Git metadata.**
   - Ordinary branch pushes do not include refs under
     `refs/codeagent/`.
   - `git push --mirror`, and other operations that copy all refs
     (such as a mirror clone of the repository), can include them —
     along with the checkpoint commits, which contain the agent's
     changes.
   - **Namespace:** `refs/codeagent/runs/`. **Lifetime:** from creation
     at the starting commit (point 4) until terminal teardown or
     dead-run reconciliation deletes it. It may persist longer only
     while an entry awaits reconciliation, or behind an
     `ABANDONED_UNRESOLVED` entry (ADR 0004).
   - A pre-existing ref at creation time, or a ref found changed
     unexpectedly, is refused and never overwritten or deleted by that
     operation.
   - Cleanup failures remain structured operational errors, and the
     lifecycle stays reconcilable under ADR 0004.
   - If `core.logAllRefUpdates=always` is configured, Git may keep a
     reflog for the ref; deleting the ref removes its reflog.
   - Git may store the ref as a loose ref or in `packed-refs`; the
     compare-and-swap operations are unaffected by which.
10. **Reachability of earlier checkpoints.** Each accepted checkpoint's
    parent is the previous accepted checkpoint: every patch attempt
    starts from a worktree whose `HEAD` equals the pinned value (point
    6). The current ref therefore keeps the whole chain back to the
    starting commit reachable. v1 never rewrites history (no amend,
    rebase, or reset of accepted checkpoints), so earlier accepted
    checkpoints stay reachable through that chain.

### Relationship to the original decision

- Original points 1–8 are unchanged. This amendment makes "last
  accepted checkpoint" (points 5 and 6) precise: it is the value of the
  lifecycle's checkpoint ref, and nothing else.
- The original guarantee boundary (point 8) is unchanged: recovery plus
  interruption detection, never crash consistency. Pinning makes
  recreation independent of garbage-collection timing; it does not make
  commit creation or ref updates atomic with respect to process death.

### Milestone boundaries

**Milestone 2 (this ADR's implementation):**
- `lifecycle_id` validation and ref-name derivation.
- Compare-and-swap create, advance, and delete, with symbolic-ref and
  pre-existing-ref refusal.
- The acceptance ordering in point 4.
- In-process discard-and-recreate from the pinned ref.
- Owner-performed terminal teardown, including ref deletion.
- The tests below.
- Within Milestone 2 the `checkpoint_ref` transition record (ADR 0004
  §5) is held only in memory by the running process.

**Later, ADR 0004 (Milestone 3 lifecycle work):**
- The durable write-ahead record of ref intent (point 5).
- Repository and lifecycle locks.
- Dead-run reconciliation of orphaned refs.
- Report-only discovery of unexpected `refs/codeagent/` refs.
- Maintenance events, startup blocking, and abandonment.

**Interim limitation until Milestone 3:** a process crash during
Milestone 2 can leave an orphaned checkpoint ref — like today's
worktrees and containers — that nothing reconciles automatically. It
retains its checkpoint objects until removed by hand.

### Consequences

- Worktree recreation no longer depends on unreachable objects
  surviving `git gc`.
- CodeAgent writes one additional ref per active lifecycle into the
  operator's repository (threat model T-M1). It is invisible to
  ordinary branch and tag listings and pushes, but visible to
  `git for-each-ref`, all-refs views such as `git log --all`, and
  mirror-style copies.
- Checkpoint objects are retained for the ref's lifetime, so an orphaned
  ref also keeps its chain's objects until it is reconciled.
- A pre-existing or unexpectedly changed ref stops the run fail-closed
  rather than being overwritten.
- `ToolCompleted(success=True)`, `CheckpointCreated`, and `PatchApplied`
  all move after the ref advance; the approved-path postcondition moves
  before acceptance.
- The current patch applier and controller must change in Milestone 2.
  `ToolCompleted`'s `apply_patch` error-code legality will be extended
  wherever the single precise code for an occurrence is not yet
  permitted. Any new enum names are fixed at implementation and pinned
  exhaustively by tests; one occurrence never produces two codes.
- ADR 0004 defines the matching durable `checkpoint_ref` transition
  record, object-format validation, and dead-run reconciliation rules.

### Required implementation tests

None of these exist yet.

- A pinned checkpoint survives reflog expiry plus garbage collection:
  - remove its worktree;
  - run `git reflog expire --expire=now --all` then `git gc --prune=now`;
  - `git cat-file -e <sha>` still succeeds, and recreation from the ref
    succeeds.
- Negative control: under the same procedure, an unpinned detached
  checkpoint whose worktree has been removed becomes unavailable
  (`git cat-file -e` fails).
- Compare-and-swap create refuses a pre-existing ref.
- Compare-and-swap advance refuses an unexpected old SHA.
- Compare-and-swap delete refuses an unexpected current value.
- A symbolic ref at the ref name is refused, for create, advance, and
  delete alike.
- A handled mid-patch failure, including a failed advance after a
  successful commit, retains the ref at the previous checkpoint, emits
  `ToolCompleted(success=False)` with a structured error and no
  `CheckpointCreated`/`PatchApplied` for the unaccepted commit, and
  recreates from the pinned checkpoint.
- A successful patch emits `ToolCompleted(success=True)` only after the
  approved-path postcondition and the ref advance, followed by
  `CheckpointCreated` and then `PatchApplied`.
- Object-format handling works in SHA-1 repositories and, where the
  installed Git supports `--object-format=sha256`, in SHA-256
  repositories: the correct zero OID, and rejection of wrong-length
  SHAs. An unsupported environment is reported as a skipped test with
  its reason, never a silent pass.
- Terminal teardown deletes only the exact lifecycle ref; other refs
  under `refs/codeagent/` are untouched.
- A failed or unconfirmed ref deletion prevents a clean terminal result.
- Error identity is preserved across events. Each of the following
  asserts that every event representing the occurrence carries the
  same `error_id`, the same `ErrorCode`, and the same sanitized message:
  - an approved-path postcondition failure (`ToolCompleted(success=False)`
    and `RunFinished`);
  - a checkpoint-ref create failure and a checkpoint-ref advance failure
    (`ToolCompleted(success=False)` and `RunFinished`);
  - a terminal checkpoint-ref cleanup failure. Terminal teardown is not
    a tool call, so no `ToolCompleted` represents it; the test asserts
    that `RunFinished` and every other event that legitimately
    references the occurrence carry the same error, and that no event
    reports a different code for it.
- The original checkout and its shared Git metadata follow the
  shared-metadata invariant:
  - the original checkout's working-copy files, branch `HEAD`, index,
    and ordinary refs are unchanged, with refs compared by value (not
    by `packed-refs` file bytes);
  - while the lifecycle is active, the only permitted administrative
    entries are the exact owned worktree registration and the exact
    hidden checkpoint ref;
  - Git's shared object database necessarily gains commit, tree, and
    blob objects and is not expected to stay byte-identical;
  - after teardown, the owned worktree registration and the checkpoint
    ref are both absent;
  - unrelated worktree registrations and refs, including other
    `refs/codeagent/` refs, are unchanged throughout.

### Rejected alternatives

- **Rely on unreachable-object retention.** Recreation would depend on
  `gc.pruneExpire`, operator-run `git gc`, and timing, and could fail
  without warning.
- **A branch (`refs/heads/codeagent/...`) or a tag.** Appears in
  ordinary branch and tag listings, and is pushed by `--all` or
  `--tags` or matching configuration.
- **Per-worktree refs (`refs/worktree/...`).** Deleted with the
  worktree — exactly when the checkpoint is needed.
- **A bundle or patch file in the run directory.** Recreation would
  depend on reapplying content correctly, and the durable run directory
  does not exist until Milestone 3.
- **One ref per accepted checkpoint.** Unnecessary: the parent chain
  already keeps earlier checkpoints reachable, and more refs mean more
  cleanup.
- **Unconditional `update-ref` without compare-and-swap.** Could
  silently overwrite a pre-existing or concurrently changed ref.

### Evidence

Code reading of `src/codeagent/patch.py`, `src/codeagent/controller.py`,
and `src/codeagent/workspace.py` at commit `567db62`. No spike evidence
exists for this mechanism; the required tests above are the evidence
obligation.

---

## Amendment 2 (Accepted 2026-09-17): lazy checkpoint establishment, durable evidence capture, gated teardown, and terminal precedence

Implementation status: **implemented (2026-09-17), locally verified
on macOS with a real Docker daemon, and confirmed on Linux CI (commit
`fafefbe`, run [35306212472](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35306212472),
success: 1863 passed, 1 intentional Darwin-only skip, all 3 real-Docker
tests passed, no leftover verification containers).**
`src/codeagent/evidence.py` (`FilesystemEvidenceSink`), the
`Workspace`/`CheckpointSessionLike` integration in
`src/codeagent/controller.py` (`RunController._terminate`,
`_dispatch_apply_patch`'s ADR 0003 Amendment 1/2 ordering,
`_map_checkpoint_error`), `initial_commit`/`entry_gate`/`dispose`/
`preserve` in `src/codeagent/workspace.py`, and the structured
`ContainerCleanupStatus` threading in `src/codeagent/executor.py` now
exist and are exercised by real, non-mocked tests: `tests/unit/
test_evidence.py` (37 tests, current; including real hostile
external-diff/textconv/clean/process filter positive controls),
`tests/unit/test_workspace.py`'s 2B-2 additions, `tests/unit/
test_executor.py`'s cleanup-status matrix, `tests/integration/
test_controller.py`'s fake-collaborator integration, and
`tests/integration/test_slice_b.py`/`test_slice_c.py`'s real
worktree + real checkpoint-ref + real evidence-artifact + (for
slice C) real Docker end-to-end runs — the latter confirming the
checkpoint ref and worktree are both genuinely absent, the evidence
artifact is genuinely published with correct framing/hash, and no
container is left behind. See `ENGINEERING_LOG.md`'s 2B-2
implementation entry for the exact file list, defect list, and what
remains unverified (SHA-256 object format, and the broader
G-list of exhaustive scenarios this pass did not each get a dedicated
test for).

**Correction pass (2026-09-17), narrowing this amendment's "every
parent path component is validated symlink-free" wording below** — in
two stages:

1. A first implementation attempt used an "already exists on disk"
   boundary to decide which components to check, exempting components
   above the deepest pre-existing ancestor. This was found, by a real
   fixture, to have two defects — it falsely rejected legitimate paths
   under macOS's own ambient `/tmp`/`/var`/`/etc` symlinks, and, more
   seriously, it silently stopped validating *any* intermediate
   component once the full output path already existed on disk (e.g. a
   shared output directory reused across runs), letting a hostile
   symlink planted at an already-materialized intermediate component go
   completely unchecked. Fixed by checking every component
   unconditionally, existing or not, exempting only a small, fixed,
   named allowlist (`/tmp`, `/var`, `/etc`).
2. That named allowlist was itself found too permissive: it trusted
   those three names on **every** platform by name alone, with no
   verification of what they actually resolve to. The accepted,
   final policy — **`_is_verified_ambient_symlink` in
   `src/codeagent/evidence.py`** — narrows this twice further: the
   exception is consulted only when the running platform is actually
   verified macOS/Darwin (`_current_platform_is_darwin`, since Linux
   does not make these three paths symlinks at all and trusting the
   bare names there would accept a hostile symlink with no ambient
   meaning), and even then a component is exempted only if its fully
   resolved real target exactly matches the recorded expectation
   (`_MACOS_AMBIENT_SYMLINK_TARGETS`: `/tmp` → `/private/tmp`, `/var` →
   `/private/var`, `/etc` → `/private/etc`, confirmed against a real
   macOS host via `os.readlink`). A symlink merely named `/tmp`, `/var`,
   or `/etc` but redirected elsewhere, or any of the three on a
   non-Darwin platform, is refused exactly like any other hostile
   symlink component.

The accepted policy is therefore: every component of the
caller-specified output path is checked for being a symlink
**unconditionally**, regardless of whether it or the full path already
exists, except for this narrow, platform-gated, target-verified
exception for the three named macOS paths. This is a narrowing of the
literal "every parent path component" wording to "every component
except this platform-gated, target-verified named exception," not a
reversal of the containment or symlink-refusal requirement itself. See
`tests/unit/test_evidence.py::test_intermediate_symlink_is_refused_even_when_the_final_directory_already_exists`
for the first reproducing fixture, and
`test_ambient_symlink_with_mismatched_target_is_refused_even_on_darwin`/
`test_ambient_symlink_is_refused_on_a_non_darwin_platform` for the
second. This status note does not otherwise revise anything in this
amendment's decision text below, which remains the accepted design as
written.

Original planning-time status, preserved for the historical record (now
superseded by the implementation status above): this amendment records
author decisions reached through a multi-round planning-only
architecture review for Milestone 2 Slice 2B-2 (integrating
`checkpoint_ref.py`/`checkpoint_session.py` into `controller.py`/
`workspace.py`/`patch.py`). Every empirical claim below was verified by
real, positive-controlled Git probes in scratch directories during that
review, not asserted from documentation. At the time this amendment was
accepted, nothing in it existed in production code yet; Slice 2B-1
(`checkpoint_ref.py`'s `MutationOutcome`, `checkpoint_session.py`) was
implemented and unwired, exactly as recorded in `ENGINEERING_LOG.md`.

### 1. Lazy checkpoint establishment (narrows Amendment 1 point 4)

Amendment 1 point 4 said the initial ref is created "after the worktree
is confirmed and before any patch attempt." This is narrowed to an
exact moment: the initial ref is created **inside the first
`apply_patch` transaction**, after that call's `ToolRequested` and
`PolicyDecisionRecorded` have been emitted, after `workspace`'s entry
gate has freshly confirmed `HEAD == workspace.initial_commit` plus a
clean staged index, a clean working tree, and no untracked paths — and
strictly before `PatchApplier.apply()` runs or any worktree mutation
begins.

- `workspace.initial_commit` is the **sole authority** for the pinned
  starting checkpoint SHA. `RunConfig.initial_checkpoint_id` is removed
  as caller-controlled input; no such field exists in 2B-2.
- **A run that never issues `apply_patch` never creates a checkpoint
  ref.** A run that is rejected, revised, or ends in `EXPLORE`/`PLAN`/
  `APPROVAL` without an accepted patch has no ref to advance, preserve,
  or delete — terminal teardown's ref-deletion step (Amendment 1 point
  7.4) is then a no-op, not a failure.

### 2. Durable evidence artifact

Before a disposable worktree may be discarded and recreated from the
last accepted checkpoint on a handled failure, or destroyed at terminal
teardown, a **durable, self-describing evidence artifact** must exist
outside both the disposable worktree and the trusted source repository:

- **Format**: one binary-safe framed file — a bounded metadata header
  (schema version, lifecycle id, capture status, payload encoding,
  bytes written, completeness, and the payload's SHA-256, all embedded
  in the artifact's own bytes, never relying on the in-memory
  `EvidenceCaptured` event surviving process exit) followed by the raw
  diff payload. Diff output is treated as arbitrary binary bytes end to
  end — never decoded or re-encoded.
- **Directory/file permissions**: the output directory is owner-only,
  exact mode `0700`; the artifact file is exact mode `0600`, verified
  by `fchmod` plus an explicit mode check, independent of umask.
- **Containment**: the output directory's canonical (fully resolved)
  path is proven outside both the source repository and the disposable
  worktree using **component-aware comparison** (resolved path
  components / `commonpath`), never string-prefix comparison; every
  parent path component is validated symlink-free, not only the final
  directory.
- **Publication is atomic and never replaces an existing artifact**:
  written to a same-directory temporary file, fsynced, then published
  by a same-directory hard link under the final lifecycle-derived name,
  with the parent directory fsynced afterward. An artifact already
  present under that name is a **structured collision refusal**, not an
  overwrite.
- **Post-link ambiguity never deletes or overwrites a published
  artifact.** If the hard link itself succeeds but a subsequent
  temp-file unlink or directory fsync cannot be confirmed, the
  published artifact is left exactly as published; the ambiguity is
  reported (`EVIDENCE_DURABILITY_UNCONFIRMED`) but never used to justify
  removing or replacing real, durable evidence.
- **Hard bound, current engine**: capture is capped at 1 MiB for
  today's single-file text-replacement patch engine. Reaching the bound
  stops capture, terminates and reaps the Git child, and marks the
  artifact **incomplete** — capture never continues draining a stream
  solely to compute an exact total byte count or a full-stream hash;
  those fields are absent, never fabricated, on an incomplete artifact.
  **This bound must be re-derived, not assumed, before any multi-file
  or add-file patch capability exists** — it is sized for the current
  engine only.
- **Incompleteness always prevents a clean result.** A truncated
  preview is persisted only when unambiguously labeled incomplete in
  its own header, and its existence never substitutes for a complete
  capture.
- **Untracked paths make v1 evidence incomplete.** Because today's
  patch engine cannot create files, any untracked path detected at
  evidence-capture time (via read-only `git status`, never by mutating
  the index) means evidence is incomplete for that run. This is a
  narrow, current-engine-specific rule, not a general claim about
  future add-file support.
- **Evidence collection is strictly observational.** It never runs
  `git add`, `git add --intent-to-add`, `update-index`, `checkout`, or
  any other command that mutates the index or working tree — including
  to work around the untracked-path blindness above.

### 3. The exact observational command, and what it does and does not prove

```
git diff --binary --full-index --find-renames --no-ext-diff --no-textconv <initial_commit> --
```
run under ADR 0006's hardened baseline, plus one freshly enumerated
filter-neutralization set (`_git_safety.enumerate_filter_neutralization`)
reused, unchanged, for both this call and the companion bounded
`git status` call used to detect untracked paths (see ADR 0006
amendment below for why both need it).

This single-endpoint invocation is empirically proven (this review) to
combine committed, staged, and unstaged tracked changes in one call.
**It is not claimed to correctly capture add, delete, rename, or binary
patch operations** — today's `GitPatchApplier` supports only single-file
text replacement on an already-tracked file and cannot produce any of
those shapes, so the command's `--find-renames`/`--binary` flags are
correctly formed for a future engine but are **not exercised or proven**
by any test in this slice. A future multi-file/add/delete/rename/binary
patch engine must independently validate this command's behavior for
those operation kinds before relying on this design's completeness
claim.

### 4. Gated teardown ordering

Evidence capture is always attempted first and never blocks any
subsequent cleanup step, regardless of its own outcome:

1. **Attempt evidence capture.** (Always; outcome recorded, never
   gates what follows.)
2. **If verifier/container cleanup is `UNCONFIRMED`**: call
   `workspace.preserve()` (preserving the worktree) and skip
   checkpoint-session deletion; this branch skips steps 3–4 entirely.
   Any checkpoint ref already created (section 1) therefore remains,
   since nothing asked it to be deleted. **If no `apply_patch` call
   ever occurred in this run, no ref was ever created (section 1), and
   this step neither invents one nor claims one exists** — "preserved"
   describes the worktree and, where a ref exists, that ref; it is not
   a claim that a ref is always present.
3. **Otherwise, exact worktree disposal** via `workspace.dispose()` — no
   `shutil.rmtree` and no `git worktree prune` fallback anywhere in this
   path.
4. **If disposal itself cannot be confirmed**: skip checkpoint-ref
   deletion — an unconfirmed worktree may still reference the ref's
   commit, so deleting the ref first would be premature.
5. **Otherwise, checkpoint-ref deletion** via the existing
   compare-and-swap delete (Amendment 1 point 3).
6. **`RunFinished`**, carrying whichever terminal error the precedence
   below selects.

### 5. Terminal precedence

```
a. any verifier/container, worktree, or checkpoint-ref cleanup step unconfirmed → LIFECYCLE_CLEANUP_UNCONFIRMED
b. evidence incomplete                                                          → EVIDENCE_INCOMPLETE
c. evidence durability unconfirmed (post-link ambiguity)                        → EVIDENCE_DURABILITY_UNCONFIRMED
d. evidence artifact collision                                                  → EVIDENCE_ARTIFACT_COLLISION
e. evidence capture failure                                                     → EVIDENCE_CAPTURE_FAILED
f. otherwise                                                                    → the run's original result stands
```
Evidence failure or incompleteness of any kind never blocks the
teardown sequence in section 4 from running to whatever point it can
reach; it only ever prevents `RunFinished` from reporting a clean
result.

### 6. Error taxonomy

A new `ErrorDomain.EVIDENCE` with four distinct codes —
`EVIDENCE_CAPTURE_FAILED`, `EVIDENCE_INCOMPLETE`,
`EVIDENCE_ARTIFACT_COLLISION`, `EVIDENCE_DURABILITY_UNCONFIRMED` — and a
new `ErrorDomain.LIFECYCLE` covering checkpoint-ref and workspace
cleanup-confirmation failures (`LIFECYCLE_CLEANUP_UNCONFIRMED`). Exact
enum member names beyond these are fixed at implementation time and
pinned exhaustively by `errors.py`/`events.py` tests, per this
project's existing errors-taxonomy discipline.

### 7. Structured Docker/container cleanup status

`ContainerCleanupStatus` (`NOT_APPLICABLE` / `CONFIRMED_ABSENT` /
`UNCONFIRMED`) replaces any boolean or defaulted cleanup flag on
`VerificationResult` and its mirrored events — **no production default
value anywhere**; every construction site supplies it explicitly.
`NOT_APPLICABLE` applies **only** when execution fails before a
`docker create` invocation is ever issued. Once that invocation begins,
every subsequent outcome — a nonzero exit, a timeout, an ambiguous
inspect result, or an OOM kill — requires an exact cleanup/absence
confirmation using the already-generated container name before the
result is reported, and resolves to `CONFIRMED_ABSENT` or `UNCONFIRMED`
accordingly, never assumed.

### Milestone boundary

Everything in this amendment is Milestone 2 Slice 2B-2 scope. ADR 0004's
durable lifecycle store, repository/lifecycle locks, startup
reconciliation, abandonment, and container labels/attribution remain
Milestone 3, unaffected by this amendment.

### Evidence

Real, positive-controlled Git probes run in scratch directories
(outside this repository) during the 2B-2 architecture review:
single-endpoint `git diff` combining committed/staged/unstaged changes;
`--no-ext-diff`/`--no-textconv` independently suppressing external-diff
and textconv execution; a hostile `clean` filter and a hostile
`.process` filter both executing during plain `git diff` despite all
four proposed flags, and both suppressed once
`enumerate_filter_neutralization`'s overrides are applied to the same
call; and observational `git diff` being completely blind to untracked
files under every flag combination tested, with `git status
--porcelain=v1` the only read-only detector. No code exists yet; the
file/test plan recorded in `ENGINEERING_LOG.md`'s 2B-2 planning entry is
the implementation obligation this amendment creates.

## Amendment 3 (Accepted 2026-11-10): the four-operation transactional patch protocol, N1 and N2 (D5-C)

Status: **Accepted 2026-11-10, after joint review; not implemented.**
(Historical: "Proposed (2026-11-09). Not accepted and not implemented.")

What remains unimplemented:
- **D5-C** does not exist in code: no four-operation protocol, no index-first
  transaction, no exact evidence preflight, no read-only Git runner, no private
  Git sessions, no `patch_contract`.
- **D5** stays **In progress, not Done**.
- **D6**, operator reachability, **Gate α** and **v1** remain unimplemented and
  unclaimed.

This amendment and its three companions were accepted on 2026-11-10. A separate
documentation reconciliation, accepted at joint review on 2026-11-10, has now
applied the accepted estimates to `docs/V1_COMPLETION_LEDGER.md`. This changes
documentation status only. It does not mean D5-C is implemented or that D5 is
Done. (Historical: before that reconciliation this said "The ledger's estimates
stay unchanged until a separate documentation update is reviewed", and earlier
"until this amendment and its three companions are accepted and a separate
documentation update is reviewed". Both were true then.)

### A3.1 Source and ownership

**Source.** The D5-C plan revision chain, accepted at joint review on
2026-11-09. The governing plan is **revision 12**:
- the plan is
  `/Users/chandrasekhar/code/codeagent-plans/D5_C_transactional_patch_protocol_plan_rev12.md`;
- its accepted content has SHA-256
  `241c11f9b2169a5dba080fbdf82cd90e2c4e115f08ab80a56513678346812566`;
- the file now hashes
  `de42aaa199a1fbf200a68623c5405f9f190e2e57a7903607d6a002b55a0b38d8`, because
  its status line alone changed to Accepted.

Revision 7
(`/Users/chandrasekhar/code/codeagent-plans/D5_C_transactional_patch_protocol_plan_rev7.md`)
keeps its accepted-content SHA-256,
`4ef87d2d4134fb8fc3fe0a1d522cbec403be4851c667b2f9c4c1c3cf31592adb` (the file
hashes `b075e072d4d7eac0550ddc8e43eba35d20a3405fc37e0a3178b7ef106e452d92`).
Revision 6
(`/Users/chandrasekhar/code/codeagent-plans/D5_C_transactional_patch_protocol_plan_rev6.md`)
keeps its accepted-content SHA-256,
`0a17f54c6073e46c3d7825dcc8bf540df67d3f08cb93f82266d579963fc2c76b`; that file
now hashes `6bc188a04046c0427e206fa4c900fabaa501a8fa959e83993945fc4164bfa9e6`
after its status-line change.

**Revision 13** (accepted at joint review 2026-11-10):
`/Users/chandrasekhar/code/codeagent-plans/D5_C_transactional_patch_protocol_plan_rev13.md`,
accepted-content SHA-256 `b39295869f8ed3388acd35b24519c81823de2cb6548738cc0e958a5ef671c744`; the file now
hashes `4eb60305bb78a53dd423ef3eb466bca770d46af3ad28f21560f98d23478eba21` after its status-line change. It
records two joint decisions of 2026-11-10, both reflected here: Phase V
routing of the new refusal classes (§A3.2) and the hotfix harness as a
required prior harness (§A3.9).

**Mandatory pre-D5-C hotfix (a satisfied prerequisite, not part of D5-C).**
ADR 0006 Amendment 5 (Accepted 2026-11-09):
- committed as `067fca0767f140a0e1f4a20afbe4b3eeae0a80e8`;
- Linux-CI-confirmed in run 42908777385;
- its evidence reconciliation is commit
  `6091f3b0b330c9c2540e11863b449f00cb1f70d9`.

Its gate is satisfied. D5-C remains unimplemented.

**Order of governance:**
1. Revisions 8–12 govern wherever they supersede revisions 1–7, and each
   later revision governs the one before it (12 → 11 → 10 → 9 → 8 → 7).
2. Revision 7 governs revision 6.
3. Revision 6 governs revision 5.
4. Revision 5 governs revision 4.
5. Revision 4 carries revision 3's scratch-ownership and cleanup-precedence
   design and revision 2's protocol, except where a later revision replaced a
   section.

This amendment and its three companions restate that design **as one decision
set**. Where a companion owns a decision, this amendment only cross-references
it.

**Ownership map.** This is the single authoritative assignment: each decision is
defined in exactly one place.

| Decision | Authoritative definition |
|---|---|
| Transaction phases, the mutation boundary, the meaning of "transactional", zero-change refusals | **ADR 0003 Amendment 3** §A3.2 |
| Index-first Phase M order (N1); the untracked-path rule's re-derived rationale; the ignored-target refusal | **ADR 0003 Amendment 3** §A3.3 |
| Phase C structural verification | **ADR 0003 Amendment 3** §A3.4 |
| N2: evidence bounds, the exact isolated preflight's semantics, the partial-state residual | **ADR 0003 Amendment 3** §A3.5 |
| Satisfaction of the Amendment 2 §3 command-validation obligation | **ADR 0003 Amendment 3** §A3.6 |
| The byte-level correction of "strictly observational" | **ADR 0003 Amendment 3** §A3.7 |
| Aggregate D5-C verification and mutation obligations, prior-harness reruns, the planning estimate | **ADR 0003 Amendment 3** §A3.9 |
| The closed, typed Git capabilities `ReadOnlyGit` (RO-01…RO-21), `PreflightGit` (PF-01…PF-07) and `MutationGit` (W-01…W-12), their operand validators, forbidden-form contract and separation; the internal filter facts, rendering and canonical snapshot; opener-bound authority, the identity registry, session/repository binding, freshness, the eight-step verification order, the refusal exception contract (`GitCapabilityRefused`) and interrupt propagation; `FILTER_CONFIGURATION_CHANGED`; entry-gate and evidence migration (D-17b); the `check-ignore` exception (RO-20); descriptor-relative filesystem mutation; repository-control files | **ADR 0006 Amendment 6** |
| The `=` driver-name refusal (`FILTER_DRIVER_NAME_UNRENDERABLE`) and today's entry-gate neutralization; the recreation launch pin of 22 (bound recorded in ADR 0004 §21.3a) | **ADR 0006 Amendment 5** (the implemented mandatory hotfix; D5-C reuses, never redefines) |
| The operations schema and limits; static and cross-operation rules; repository-state rules; canonical forms and digests (`codeagent.change.v2`, `codeagent.plan.v2`, `codeagent.patch_policy.v1`); redaction; `PatchPolicy` and the test/verification-configuration policy; the syntax guard; `patch_contract` and `ApprovedChange` authority; the T11 classification row; new `ArgumentRejection` members; `test_file_paths` | **ADR 0007 Amendment 3** |
| Private Git sessions as an owned sub-resource (location, derivation, creation, state machine, removal coverage); the scratch cleanup error, code, record and outcome table; terminal tier a2 | **ADR 0004 Amendment 22** |

### A3.2 Phases, the mutation boundary and what "transactional" means

**One `apply_patch` transaction** applies one approved `ApprovedChange`
(ADR 0007 Amendment 3) in four phases:

- **Phase S (static):** ADR 0007 Amendment 3's static and cross-operation
  rules. It is enforced at `propose_plan` and repeated by the applier.
- **Phase V (pre-write):**
  1. `verify_approved_change`;
  2. preflight and object format;
  3. a clean staged baseline and object availability;
  4. every repository-state rule for every operation;
  5. the intended blob ids, via `hash-object --no-filters --stdin` **without**
     `-w`;
  6. the syntax guard;
  7. the trusted-policy recheck;
  8. the exact evidence preflight (§A3.5).

  Every Git call uses ADR 0006 Amendment 6's `ReadOnlyGit`, and every
  filesystem read is descriptor-relative. Every private Git session opened in
  Phase V is **closed and confirmed absent before the boundary** (ADR 0004
  Amendment 22).
- **The mutation boundary** is the first Phase M write to the worktree's real
  object store, index or files: the first real `hash-object -w`.
  - The preflight's writes go only to its private session (ADR 0004 Amendment
    22) and are not a boundary crossing.
  - A failure before the boundary uses a pre-write code. A failure at or after
    it is `PATCH_APPLICATION_FAILED`, never `PATCH_VALIDATION_FAILED`.
  - **Phase V failure map for the new refusal classes** (owned here; revision
    13 Decision 1):

    | Raised in Phase V | Translation | Route |
    |---|---|---|
    | `GitSafetyError(FILTER_DRIVER_NAME_UNRENDERABLE)` | `PATCH_UNSUPPORTED_GIT_SUBSTRATE`, fixed sanitized text | T2: terminate; never `PATCH_FAILED` feedback, C28, C29, C30, repair continuation or recreation |
    | `GitSafetyError(FILTER_CONFIGURATION_CHANGED)` | the same | T2, the same |
    | `GitCapabilityRefused` | none: no `PatchResult`, never T1 or T2 | T8: the exact exception propagates to the existing unexpected-exception boundary, giving one failed `ToolCompleted` (`TOOL_EXECUTION_FAILED`, the fixed text "tool failed unexpectedly") and then `UNCLASSIFIED_FAILURE`; no capability, session, configuration, path or internal-exception text in any event; no C28, C29, C30, feedback, recreation, verification or later Git launch |

    - **Why:** the first two describe an unusable or unstable Git substrate.
      `GitCapabilityRefused` is an internal authority violation, and calling it
      a substrate failure would hide a defect. None is model-correctable
      feedback.
    - **Unchanged:** Gate zero, cleanup dominance (ADR 0004 Amendment 22 rows
      #3 and #4) and interrupt precedence (ADR 0005 Amendment 1).
    - **Constraint:** `GitCapabilityRefused` is not a `GitSafetyError`, and no
      applier handler that translates `GitSafetyError` may catch it.
    - **Superseded hotfix behaviour:** this replaces today's hotfix behaviour,
      under which the pre-write check maps an unrenderable name to
      `PATCH_VALIDATION_FAILED` (T1). The hotfix's
      `test_target_attribute_check_refuses_unrenderable_driver` is migrated to
      the T2 expectation, not weakened (revision 13 contradiction 56).
    - **Tests** (revision 13 §2):
      - `test_phase_v_filter_substrate_refusals_terminate_t2[FILTER_DRIVER_NAME_UNRENDERABLE|FILTER_CONFIGURATION_CHANGED]`;
      - `test_phase_v_capability_refusal_propagates_t8`;
      - `test_phase_v_refusal_cleanup_unconfirmed_dominates[unrenderable|config-changed|capability-refused]`.
- **Phase M:** §A3.3.
- **Phase C:** §A3.4.

**"Transactional" means:**
- whole-proposal validation before the boundary (point 1, unchanged);
- a refusal before the boundary changes nothing (below);
- after the boundary, any handled failure contaminates the worktree, which is
  recreated (ADR 0004 Amendments 20–21) or the run terminates;
- a checkpoint is accepted only after Phase C and a ref advance.

**It does not mean** filesystem atomicity, invisible intermediate writes,
crash consistency, or per-file rollback (points 3 and 8, unchanged).

**Zero-change refusal.** For every pre-boundary refusal, the following are
byte-identical before and after:
- the worktree's tree, bytes and mtimes;
- the worktree index's bytes and mtime;
- the admin directory's entries;
- the common directory's object listing;
- the refs;
- `lifecycle.json`;
- no session leftover.

### A3.3 N1: index-first Phase M

**Order.** Operations are canonical (ADR 0007 Amendment 3), and paths are
disjoint and prefix-free, so the final tree is order-independent. The steps run
in canonical order:
1. For `add` and `rename`'s `to`, install the index entry first: `hash-object
   -w --no-filters --stdin` for `add`, or the HEAD oid for `rename`, then
   `update-index --add --cacheinfo <mode>,<oid>,<path>`.
2. Write the bytes of `add`, `update` and `rename` `to` (exclusive or
   truncating, descriptor-relative; ADR 0006 Amendment 6), creating missing
   parents as 0755.
3. Unlink `delete` and `rename` `from` from disk, then remove emptied parents
   bottom-up, never the root.
4. Remove `delete` and `rename` `from` from the index.
5. Stage each `update` path with its intended blob (`hash-object -w
   --no-filters`, then `update-index --cacheinfo`). A descriptor re-read proves
   the disk matches.

**Invariant.** At every instant:
- each engine-created path is already in the index;
- each removed path stays in the index until it is gone from disk.

The engine therefore never leaves an engine-created path untracked.

**The untracked-path rule (Amendment 2 §2)** is kept **verbatim in force**. Its
rationale is re-derived: it is no longer "today's patch engine cannot create
files", but "the engine never leaves an engine-created path untracked; any
untracked path is foreign contamination and makes evidence incomplete".

**Ignored targets.** An `add` or `rename` `to` whose path is gitignored is
refused in Phase V (`PATCH_VALIDATION_FAILED`). Under index-first such a path
would be tracked, so this is defence in depth. The control experiment showed a
*naive* ignored write is invisible to `status` and evidence. ADR 0006 Amendment
6 governs how the check runs. (Historical: this read "Amendment 5" after the
mechanical renumbering, which missed it because the reference wrapped across a
line; see §A3.11.)

**Evidence** (planning; Git 2.54.0, SHA-1; temporary repositories outside this
repository; the raw results are `n1_results.json`, SHA-256
`d88a84cf8be8164aa8cdeba6d2c0e0678968fbed9e9e73a18391c8212ae13f88`). There were
21 scenarios: 19 index-first and 2 controls.
- **Index-first:** all 54 partial states (add, update, delete and rename,
  across text, empty, non-UTF-8 binary, executable, small and empty renames,
  and ignored-directory cases) showed no `??`.
  - The evidence diff, applied to a base checkout, reproduced the exact on-disk
    paths, bytes and exec bits.
  - A fresh process read every state identically.
  - An `index.lock` changed nothing.
  - `git worktree remove --force` succeeded.
- **Controls:** a naive add showed `??`; a naive ignored add was completely
  invisible.

### A3.4 Phase C structural verification

1. A bounded `diff --cached --name-status -z --no-renames HEAD` must equal the
   expected status set exactly.
2. `diff_bytes` is computed through a **bounded** runner, replacing today's
   unbounded capture.
3. `write-tree`, then `commit`. The commit's exit code is not trusted, as
   today.
4. HEAD must move to a commit with exactly the expected parent and tree.
5. `diff-tree -r -z --no-renames <parent> <new>` must equal the expected set.
6. Per path: an added or updated path has the intended mode, oid and exact
   bytes; a deleted path or rename `from` is absent; a rename `to` has the
   source's mode and oid.

The filter re-check (ADR 0006) runs before `write-tree` and before `commit`, for
every path of the proposal. Any failure is `PATCH_APPLICATION_FAILED`.

### A3.5 N2: evidence bounds by exact isolated preflight

- **The bounds are unchanged.** `MAX_DIFF_BYTES` = 1 MiB applies to the raw
  stdout of the Amendment 2 §3 evidence diff, per artifact: each recreation
  member, against the last accepted checkpoint, and the terminal artifact,
  against the run origin H0. `MAX_STATUS_BYTES` = 1 MiB applies to the status
  listing. `model_protocol.MAX_RESPONSE_BYTES` is a separate model-turn bound.
- **The closed-form bound is withdrawn.** It did not cover `--binary`
  payloads, path quoting, function-name hunk headers, repository `diff.*` and
  `core.bigFileThreshold` configuration, `diff` attributes, rename versus
  fallback, or cumulative terminal scope.
- **The preflight.**
  1. Build the proposed tree *T* in a private session: a private index and
     object directory, with the real object store as a read-only alternate
     (ADR 0004 Amendment 22 for the session; ADR 0006 Amendment 6's
     `PreflightGit` for the environment and commands). Run it **in the
     worktree**, so the repository's own config and attributes apply.
  2. Run the evidence command tree-to-tree twice: `<accepted> <T>` (member)
     and `<H0> <T>` (terminal). Each read is bounded to `MAX_DIFF_BYTES + 1`.
  3. If either exceeds 1 MiB, refuse with `PATCH_VALIDATION_FAILED` (T1; the
     model may shrink the proposal).
  4. A preflight **infrastructure** failure, or a refused session open, is
     `PATCH_UNSUPPORTED_GIT_SUBSTRATE` (T2).
  5. A session **cleanup** failure follows ADR 0004 Amendment 22, never T2.
- **Equivalence evidence** (planning; `n2_results.json`, SHA-256
  `dd24624cb2a0540ab7e14f8cd1b053f1227abda2f20b9ad29635f1951b42a52b`). The
  preflight bytes equalled the real post-apply evidence bytes in all 28 cases,
  including hostile `diff.*` configuration, `core.bigFileThreshold`, `-diff`
  attributes, a non-ASCII path, cumulative passes, SHA-256, and 8 adversarial
  sizes (the largest single file 201,965 bytes). The real index, objects, refs
  and files were unchanged.
- **Residual (fails closed).** A *partial* Phase M state may produce a
  recreation member larger than the final state's. If it exceeds 1 MiB, Phase E
  is incomplete, recreation is refused, and the run terminates. Tree-to-tree
  equivalence with the worktree diff is empirical: it is pinned by tests, not
  proved.

### A3.6 The Amendment 2 §3 command-validation obligation

Amendment 2 §3 required the evidence command to be independently validated for
add, delete, rename and binary shapes before the completeness claim relies on
them. §A3.3's matrix and §A3.5's equivalence suite are that validation, **on
Git 2.54.0 only**. Both are ported into required tests, and they must pass on
CI's Git (2.56.0) before the claim holds there.

### A3.7 "Strictly observational", at byte level

Porcelain `git diff <rev> --`, which is this ADR's evidence command, and `git
diff --quiet HEAD --` rewrite the index's **stat cache** on a stat-dirty index,
even with `GIT_OPTIONAL_LOCKS=0`. Entry contents and blob ids are unchanged.
Amendment 2's "strictly observational" was therefore semantically true but not
byte-exact. ADR 0006 Amendment 6 migrates evidence capture and the entry gate to
`ReadOnlyGit`, which makes it byte-exact for the real index.

### A3.8 Terminal precedence

Amendment 2 §5's list gains tier **a2**, directly below a
(`LIFECYCLE_CLEANUP_UNCONFIRMED`) and above b (`EVIDENCE_INCOMPLETE`). Its
definition, code and rules are owned by **ADR 0004 Amendment 22**; this section
only records that the list is amended there.

### A3.9 Aggregate verification, mutation obligations and estimate

**Required verification** (each companion lists its own suites):
- the N1 partial-state suite;
- the preflight-equivalence suite;
- the read-only proof suite;
- the scratch suite, with real reconciliation and abandonment;
- the four-operation end-to-end tests: a scripted model on real Git, and the
  same on real Docker;
- fault injection at every Phase M/C boundary, preflight step and session
  cleanup;
- real-SIGKILL next-admission tests;
- the contract and binding tests;
- forward and reverse affected sets;
- the full suite with `CODEAGENT_REQUIRE_DOCKER=1`.

CI's verbose step adds the four-operation real-Docker node, the N1 partial-state
suite and the scratch suite.

**D5-C mutation catalog: 228 variants.** Each is a distinct source edit with a
named test and assertion, under the strict assertion-only rule.

| Group | IDs | Count | Subject (owning ADR) |
|---|---|---|---|
| S | S-01…S-18 | 18 | static schema (ADR 0007 Amendment 3) |
| D | D-01…D-27 | 27 | canonical forms, digests, redaction (ADR 0007 Amendment 3) |
| P | P-01…P-26 | 26 | policy, policy validation, trusted-input checks (ADR 0007 Amendment 3) |
| A | A-01…A-18 | 18 | Phase V repository-state checks (ADR 0007 Amendment 3 rules, run in this ADR's Phase V) |
| R | R-01…R-11, R-13 | 12 | read-only proof and capability placement (ADR 0006 Amendment 6); R-12 retired into Q-08 |
| X | X-01…X-07, X-09, X-10 | 9 | exact preflight (this amendment §A3.5) |
| M | M-01…M-17 | 17 | Phase M/C (this amendment §A3.3–§A3.4) |
| C | C-01…C-10 | 10 | controller integration (ADR 0007 Amendment 3); C-09 and C-10 test this amendment's Phase V routing (§A3.2) |
| K | K-01…K-22 | 22 | `ApprovedChange` authority (ADR 0007 Amendment 3) |
| G | G-01…G-24 | 24 | private sessions and cleanup (ADR 0004 Amendment 22) |
| Q | Q-01…Q-45 | 45 | closed typed Git capabilities, filter facts and snapshot, authority, verification order, refusal and interrupt semantics (ADR 0006 Amendment 6) |
| **Total** | | **228** | |

- **Field-binding inventory:** the 24 canonical fields are dispositioned in ADR
  0007 Amendment 3. `risk_notes` is covered by D4b's MR3, re-anchored and **not**
  counted in the 228.
- **Check:** 18 + 27 + 26 + 18 + 12 + 9 + 17 + 10 + 22 + 24 + 45 = 228.
  (Historical: 206 before revision 8, with Q at 25; revisions 8–12 added
  Q-26…Q-45 and re-pointed Q-24 and Q-26, giving 226; revision 13 added C-09
  and C-10.)
- **The two new routing variants** (revision 13). A mechanical overlap check
  against C-04, C-05, R-13, G-09, Q-33 and Q-45 found no existing cover, so
  nothing was re-pointed or reused:
  - **C-09:** a Phase V substrate refusal (`FILTER_DRIVER_NAME_UNRENDERABLE` or
    `FILTER_CONFIGURATION_CHANGED`) mapped to T1 instead of T2. Caught by the
    parameterized substrate-routing test: the code is
    `PATCH_UNSUPPORTED_GIT_SUBSTRATE`, with no feedback and no C28.
  - **C-10:** `GitCapabilityRefused` converted into a `PatchResult`
    (canonically T2) instead of propagating to T8. Caught by the T8 test: one
    `TOOL_EXECUTION_FAILED` `ToolCompleted`, then `UNCLASSIFIED_FAILURE`.
- **Prior-harness reruns: 232 required** (separate from the 228 new D5-C
  variants).
  - **The six earlier prior harnesses: 226 reruns.**
    - They contain 227 recorded entries at `5422046`, every one invalidated by
      the required `tests/support/fakes.py` migration.
    - B14 is obsolete, so **226** earlier reruns are required and runnable.
  - **Plus the 6 mandatory-hotfix entries,** H-1…H-6 (ADR 0006 Amendment 5
    §A5.6), required by revision 13 Decision 2:
    - All six run, even where a D5-C mutation covers a related property; prior
      regression evidence and new D5-C mutation evidence are separate
      obligations.
    - None is retired for overlap.
    - A migrated anchor is re-anchored with its original defect and
      designated assertion preserved.
    - The sequence is clean baseline → H-1…H-6 → clean baseline, with
      assertion-only counting and SHA-256-verified restoration.
  - **Predicted re-anchors: 23 + up to 6.**
    - The 23: A14; M14, M15 and M22–M24; S7-M28, M29, M32 and M33; and 13 D4b
      entries including MR3.
    - Up to 6 hotfix entries: H-1, H-5 and H-6 to `_enumerate_filter_facts`;
      H-2, H-3 and H-4 to the migrated entry gate.
    - That prediction is final only after implementation. Any further anchor
      miss is classified, never dropped.
  - **Arithmetic:** 227 − 1 + 6 = 232.
  - (Historical: 226 before revision 13.)
- **Machine time:** approximately **12.5–22.5 hours**.
  - New variants: 228 × 2–4 min, about 7.6–15.2 h.
  - Prior reruns: 232 × 1.33–1.86 min (revision 12's 5–7 h basis for 226),
    about 5.1–7.2 h.
  - (Historical: 12–21 h at 206; 12.5–22 h at 226 with 226 reruns.)

**Planning estimate** (from accepted revisions 12 and 13; low–medium
confidence; applied to the ledger by the documentation reconciliation accepted
2026-11-10):

| | Hours |
|---|---|
| D5-C only | **178–292** (replacing the provisional 45–80) |
| Mandatory hotfix (ADR 0006 Amendment 5; landed, planned separately) | **10–18** |
| D5, combined | **317–519** |
| Remaining D5–D10 | **480–859** |
| First operator run | **516–857** |
| Gate α | **583–992** |
| Public v1 | **645–1,131** |

**Arithmetic:**
- **D5-C:** revision 12's 175–286.5, plus revision 13's increment of +3 / +5.5:
  - routing code +0.5 / +1;
  - routing tests and the hotfix C4 test migration +1.5 / +2.5;
  - C-09/C-10 runs and hotfix-harness re-anchoring +0.5 / +1.5;
  - documentation +0.5 / +0.5.

  That gives 175 + 3 = 178 and 286.5 + 5.5 = 292.
- **D5, combined:** low 174 − 45 + 178 + 10 = 317; high 289 − 80 + 292 + 18 =
  519.
- **Remaining D5–D10:** 337 − 174 + 317 = 480; 629 − 289 + 519 = 859.
- **First operator run:** 373 − 174 + 317 = 516; 627 − 289 + 519 = 857.
- **Gate α:** 440 − 174 + 317 = 583; 762 − 289 + 519 = 992.
- **Public v1:** 502 − 174 + 317 = 645; 901 − 289 + 519 = 1,131.
- **Cross-check:** 165 + 480 = 645; 272 + 859 = 1,131.
- **Unchanged:** Gate O 86–140 h and completed D1–D4 165–272 h.
- (Historical: before revision 13, these combined figures were D5-C
  175–286.5, D5 314–513.5, remaining 477–853.5, first operator run 513–851.5,
  Gate α 580–986.5 and public v1 642–1,125.5.)

Revision 12's figures, D5 304–495.5 h and its dependent totals (remaining
467–835.5, first operator run 503–833.5, Gate α 570–968.5, public v1
632–1,107.5), were **D5-C-only** implied figures. They excluded the separately
planned hotfix. Now that the hotfix has landed, combined figures supersede them
for proposed aggregate planning, and revision 13's figures above are the
current ones. (Historical: before revisions
8–12 this table read D5-C 167.5–275 and D5 296.5–484.)

These estimates **have been applied** to `docs/V1_COMPLETION_LEDGER.md` by the
separate documentation reconciliation, accepted at joint review on 2026-11-10.
The amendments themselves were also accepted on 2026-11-10. This changes
documentation status only. It does not mean D5-C is implemented or that D5 is
Done. (Historical: before that reconciliation this said "These are not applied
to `docs/V1_COMPLETION_LEDGER.md` until a separate ledger documentation update
is reviewed", and earlier "until these amendments are accepted and that
documentation update is separately reviewed". Both were true then.)
Re-estimation is mandatory after acceptance, if CI's Git disagrees with the
planning evidence, or if the `patch.py` rewrite exceeds 22 h or the session
layer exceeds 7 h.

### A3.10 Non-claims and residuals

**Non-claims:**
- no filesystem atomicity, crash consistency or invisible intermediate writes;
- no per-file rollback;
- no weakening detection for allowed test updates (ADR 0007 Amendment 3);
- no live model and no operator path;
- no D5 completion, D6, operator-readiness, Gate α, `COMPLETE_FOR_V1` or v1
  claim;
- no threat-model conclusion changes.

**Residuals:**
- the N1, equivalence and scratch evidence is Git 2.54-only until CI;
- tree-diff equivalence is empirical;
- the partial-state member may overflow (fails closed);
- the split-index refusal (ADR 0006 Amendment 6);
- the A4 same-user race (narrowed by descriptor-relative mutation, not
  closed);
- `ApprovedChange` authority is repository-enforced, not a language boundary
  (ADR 0007 Amendment 3);
- an abandoned entry retains its private session (ADR 0004 Amendment 22);
- the pre-existing D3 intermittent test;
- about 12.5–22.5 h of mutation machine time (228 new variants and 232 prior
  reruns).

The planning evidence files live in a session scratch directory outside this
repository. Their hashes identify them, but the implementation tests are the
authoritative proof.

### A3.11 Contradictions recorded during the draft revisions, and their resolution

1. **Phase V routing of the new refusal classes** (plan contradiction 54).
   Revisions 8–12 assigned no Phase V route to `FILTER_CONFIGURATION_CHANGED`,
   `GitCapabilityRefused` or `FILTER_DRIVER_NAME_UNRENDERABLE`. **Resolved** by
   revision 13 Decision 1 and §A3.2's failure map: the two substrate reasons go
   to T2, and `GitCapabilityRefused` to T8. (Historical: this item was
   recorded as open, needing a joint decision.)
2. **Hotfix harness reruns** (plan contradiction 55). The 226-rerun count
   predated the hotfix's six-mutation harness. **Resolved** by revision 13
   Decision 2: prior reruns are 232 (§A3.9). (Historical: this item was
   recorded as open, needing a joint decision.)
3. **The hotfix C4 expectation** (plan contradiction 56). Today's hotfix test
   asserts T1 for a pre-write unrenderable name, while Decision 1 routes it to
   T2 under D5-C. **Resolved:** the test is migrated, not weakened, and
   Amendment 5's committed text is unchanged.
4. **A stale wrapped reference.** The mechanical Amendment 5→6 renumbering
   missed §A3.3's "ADR 0006 Amendment\n5", because it wrapped across a line. It
   is corrected above. A whole-file multi-line search found no other instance.
5. **The test-involvement wording.** The queued correction to ADR 0007
   Amendment 3 §A3.5 comes from revision 7 §6, not revision 8, and it is already
   present in that draft. No change was needed.
