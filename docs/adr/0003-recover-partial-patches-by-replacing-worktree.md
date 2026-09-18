# ADR 0003: Recover partial multi-file patches by replacing the worktree, not by per-file rollback

Status: Accepted. Amended 2026-09-15 by Amendment 1 (Accepted): durable
checkpoint reachability through hidden Git refs. Amended 2026-09-17 by
Amendment 2 (Accepted): lazy checkpoint establishment, durable evidence
capture, gated teardown ordering, and terminal precedence — see the end
of this document. Everything above "Amendment 1" is the original
decision, unchanged; Amendment 1 is unchanged by Amendment 2 except
where Amendment 2 explicitly narrows its point 4 ("establish, then
rely") ordering.

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

Implementation status: **not implemented.** This amendment records
author decisions reached through a multi-round planning-only
architecture review for Milestone 2 Slice 2B-2 (integrating
`checkpoint_ref.py`/`checkpoint_session.py` into `controller.py`/
`workspace.py`/`patch.py`). Every empirical claim below was verified by
real, positive-controlled Git probes in scratch directories during that
review, not asserted from documentation. Nothing in this amendment
exists in production code yet; Slice 2B-1 (`checkpoint_ref.py`'s
`MutationOutcome`, `checkpoint_session.py`) remains implemented and
unwired, exactly as recorded in `ENGINEERING_LOG.md`.

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
