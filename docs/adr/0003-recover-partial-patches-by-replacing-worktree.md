# ADR 0003: Recover partial multi-file patches by replacing the worktree, not by per-file rollback

Status: Accepted

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
