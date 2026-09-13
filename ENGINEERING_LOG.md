# CodeAgent — Engineering Log

Per `docs/CODEAGENT_LLM_HANDOFF.md`: one entry per meaningful session,
recording intended outcome, decisions made (with rejected alternatives
where a real choice existed), evidence/verification, and the next
smallest step. Kept terse — this is a decision trail, not a narrative.
Formal, durable architectural decisions are promoted to `docs/adr/`;
this log covers the full granularity of trade-offs behind them.

---

## Session: Milestone 0 — domain state machine (initial)

**Outcome**: `src/codeagent/domain.py` + exhaustive tests.

- Decided the state machine (`INIT→BASELINE→EXPLORE→PLAN→APPROVAL→
  EXECUTE→VERIFY→DONE`) and tool-legality-per-state as a pure,
  side-effect-free module — no budgets/counters here, so the state
  machine can be exhaustively tested independently of controller logic
  that doesn't exist yet.
- Initially included `run_process` as a model-callable tool during
  `EXPLORE`. On review, removed it entirely before any process
  dispatcher/policy existed to bound it — recorded as ADR 0001, since
  it narrows the model's capability versus the original design docs.
- Verification: 217 tests passing at this point (later revised).

## Session: Milestone 0 — domain corrections (approval loop, budget split, enum stability)

**Outcome**: added `PLAN_REVISION_REQUESTED`, `TransitionResult`,
split `BUDGET_EXCEEDED` into three specific terminal reasons.

- Added a second approval outcome (revision-requested, distinct from
  reject) — creates a second uncapped repair-style loop alongside the
  verify-failure loop; both caps are explicitly deferred to
  `budgets.py` (unbuilt), not enforced in `domain.py`.
- Split `BUDGET_EXCEEDED` into `REPAIR_ITERATIONS_EXHAUSTED`/
  `COST_LIMIT_EXCEEDED`/`WALL_CLOCK_EXCEEDED` to satisfy the completion
  criteria's cost/latency reporting requirement.
- **Reverted in the next session** — see below. Recorded here so the
  reasoning for *both* directions is visible, not just the final state.

## Session: Milestone 0 — event schemas (initial) + budget-taxonomy revert

**Outcome**: `src/codeagent/events.py` (first pass), domain.py's
budget split reverted.

- Reverted the 3-way budget-trigger split back to one generic
  `BUDGET_EXCEEDED` trigger/terminal-reason, adding `BudgetKind` (6
  members) instead. Rationale: the *state machine* only needs to know
  a budget ended the run; which budget is an event-payload concern
  (`BudgetExceeded.kind`), not a state-machine concern. Kept the
  domain layer's vocabulary minimal.
- Added `ApprovalDecision`/`ApprovalMode` as dedicated enums rather
  than overloading `Trigger` for approval outcomes — rejected reusing
  `Trigger` because it would accept all 14 members and reject most at
  construction time; a narrower type is a static-typing win, not just
  a runtime check.
- Verification: after the revert, re-ran the full domain suite to
  confirm exhaustive coverage survived intact (209/209), not just that
  it compiled — this was an explicit ask, and matters because a revert
  is exactly the kind of change that silently drops test coverage if
  you're not careful.

## Session: Milestone 0 — event-schema correction pass (exit codes, canonical JSON, budgets, common validation)

**Outcome**: exit-code-per-outcome rules, canonical JSON for tool
arguments, `BudgetExceeded.projected_value`, stronger common
validation (finite floats, aware timestamps, nonempty-if-present IDs).

- Defined a project-specific canonical JSON form (sorted keys, compact
  separators, Unicode preserved, duplicate keys and NaN/Infinity
  rejected) for `redacted_arguments_json` — explicit that this makes
  two independently-produced JSON strings comparable byte-for-byte in
  the log; it is *not* a redaction implementation, and the log says so
  in three places (module docstring, field comment, this entry).
- Added `projected_value` to `BudgetExceeded` to support proactive
  budget enforcement (block before overspending, not just report
  after) — `observed_value` (actual) and `projected_value` (if the
  denied operation proceeded) are now both meaningful, independently
  validated fields.
- Verification: 394 tests passing, plus a manual smoke script
  exercising every new invariant *before* the corresponding test file
  was written, specifically so the tests were checked against
  confirmed behavior rather than written on faith.

## Session: Milestone 0 — error taxonomy (design review, then implementation)

**Outcome**: `src/codeagent/errors.py`, `ModelRequestFailed` event,
per-event error-field validation matrices.

- First taxonomy draft used `ErrorCategory` (RECOVERABLE_OPERATIONAL /
  TERMINAL_OPERATIONAL) baked onto each `ErrorCode`. **Rejected on
  review** — this contradicts the stated principle that retry/
  terminate depends on controller state, budgets, and attempt count,
  none of which the taxonomy has visibility into. Replaced with
  `ErrorDomain` (purely descriptive: what kind of thing failed, no
  disposition implied).
- Compared three ways to represent "the model provider never returned
  a response at all": (a) make `ModelResponseReceived.response_id`
  optional — rejected, would let one event mean two different things;
  (b) a generic `OperationFailed` event — rejected, breaks from the
  codebase's one-dataclass-per-EventType style and re-derives
  per-operation correlation-ID shape that the existing event pairs
  already give for free; (c) a new sibling event, `ModelRequestFailed`,
  correlated by `request_id` only — **chosen**.
- Decided not to add `error_id` to `OperationalError` — nothing in the
  current design needs one failure referenced from two events or
  deduplicated; existing correlation IDs + event sequence suffice.
  Explicit trigger for revisiting: the day a real cross-event reference
  need appears.
- Kept `UNCLASSIFIED_FAILURE` but bounded it to exactly one attachment
  point (`RunFinished` only) so it can't become a dumping ground — every
  other event requires a precise, domain-specific code because what
  happened is already known at the point that event is recorded.
- Verification: 537 tests, plus a manual smoke script per invariant
  before the test file, same discipline as the prior session.

## Session: Milestone 0 — narrow error-taxonomy correction pass

**Outcome**: `ToolCompleted`'s error-code legality now depends on
`tool`, not just `success`; corrected an overclaim in a docstring;
fixed a mismatched test name.

- `PATCH_VALIDATION_FAILED`/`PATCH_APPLICATION_FAILED` are only valid
  when `tool is ToolName.APPLY_PATCH` — a `read_file` failure can never
  legitimately claim a patch-specific code. Added an exhaustive
  (tool × code) partition test (75 cases) rather than trusting hand-picked
  examples.
- Removed an unsupported "exactly one, never neither" cardinality claim
  from `ModelRequestFailed`'s docstring — that discipline belongs to a
  controller/EventSink that doesn't exist yet; a started request can
  legitimately have no terminal event at all after a crash. Don't claim
  more than what's actually enforced.
- A test named for one thing was actually only checking `hasattr`, not
  the real import graph. Renamed it to match what it proves, and added
  a second, genuine import-graph check using `ast`/`inspect` (stdlib
  only, no new dependency) to confirm `domain.py` doesn't import
  `errors.py`.
- Verification: 677 tests passing.

## Session: Milestone 0 — threat model (draft)

**Outcome**: `docs/threat-model.md`, ~45 threat records across 14
categories, 9 explicit author trust-boundary decisions recorded inline.

- Corrected a claim that had been repeated informally across prior
  reports/commit messages: "the original repository is never touched."
  This is imprecise — Git worktrees **share** administrative metadata
  with the repo they're created from. The accurate claim, now the
  standing one: CodeAgent does not intentionally modify the original
  checkout's *working-copy files*; shared Git metadata is accessed only
  through controlled worktree operations. Use this framing in any
  future README/ADR/completion-criterion language, not the stronger
  one.
- Flagged 4 residual risks as genuinely requiring author sign-off
  rather than folding them into "accepted" silently: no redaction
  exists yet (live gap, not hypothetical); the dependency-prep phase's
  network window is irreducible while third-party dependency
  installation is supported; `approval_mode=none` is a real, accepted
  safety reduction, not a softened one; container escape is the single
  largest unmitigated residual risk in the document and should be
  stated as such, not hedged.
- Verification: N/A (documentation step, no code/tests touched this
  session per explicit instruction).

## Session: push to origin/main

**Outcome**: first commit since the repo's prior spike commit,
containing all of the above (domain/events/errors + tests + corrected
planning docs + ADR 0001), pushed to `origin/main` (`528ea8d`).

- Decided one combined commit rather than several, per explicit
  request, with a commit message written for both a technical and a
  non-technical reader (plain-language framing per section, technical
  specifics retained) — a deliberate choice given this is a portfolio
  project where the commit history itself is part of the evidence a
  reviewer sees.
- Gap identified in this same session (see the message that prompted
  this log's creation): several of the decisions above existed only in
  conversation, not in a durable file, until now. This log exists to
  close that gap going forward, not just retroactively.

## Session: threat model — ADR promotion + revision pass

**Outcome**: ADR 0002 written; `docs/threat-model.md` revised with four
resolved author decisions and several corrections.

- Promoted the budget-taxonomy design (ADR 0002) to a formal ADR; kept
  the `ModelRequestFailed` design at log-level only, per explicit
  author choice — not every compared-alternatives decision warrants a
  file, even a good one.
- Resolved the four flagged residual risks as **accepted, with
  conditions** rather than left open: redaction (T-G1) reframed as a
  hard security gate, not a scheduling note; the dependency-prep
  network window (T-H1) accepted with six explicit conditions
  (opt-in, ephemeral, isolated, auditable, sequenced before
  verification); `approval_mode=none` (T-I1) accepted only for curated
  CI/benchmark use, with a new sibling threat (T-I4) added specifically
  to block repository-supplied config from silently selecting it;
  container-escape (T-K1) reaffirmed in plain language, explicitly not
  to be softened in future public-facing docs.
- Corrected an overclaim: "pinned = trusted" for Docker images/
  dependencies is wrong — pinning gives reproducibility and
  drift-detection, not vetted trust. Fixed in the trusted/untrusted
  table and T-D1.
- Corrected another overclaim, systemically: ~15 "residual risk: none
  expected once implemented" lines overstated what a planned-but-
  unbuilt control could honestly promise. Replaced with language
  acknowledging implementation defects, undiscovered vulnerabilities,
  and configuration drift as ongoing possibilities, not resolved to
  zero by the mere existence of a plan.
- Added an executive summary and reframed O1-O7 as target objectives
  for the eventual system, not guarantees the current (mostly
  unbuilt) system already provides — the original draft risked
  reading as more complete than the codebase actually is.
- No new architecturally-consequential decision (beyond the two ADR
  candidates already triaged) surfaced in this pass — the changes are
  corrections and resolutions of the prior draft, not new designs.

## Session: Milestone 0 closure

**Outcome**: threat model and ADR 0002 accepted; Milestone 0's guide-defined
deliverables (domain types, state-transition table, event schemas, error
taxonomy, threat model, ADRs for consequential choices) are all present and
verified. 677 tests passing, `git diff --check` clean.

- Corrected ADR 0002's first consequence, which had wrongly claimed adding a
  budget dimension is an `events.py`/`budgets.py`-only change — `BudgetKind`
  actually lives in `domain.py`, so extending it (and its pinned-value
  tests) is part of that change too. The state-transition topology is still
  untouched, which was the actual point of the ADR.
- Clarified in `CLAUDE.md` that the current explicit user request always
  outranks the repository's documented authority order — that order only
  resolves conflicts among the documents themselves.
- Replaced "further ADRs are still owed" framing with a documentation
  ladder (ADR / ENGINEERING_LOG / code comment / issue register) so future
  sessions size the record to the decision instead of treating ADR count as
  a checklist to fill.

## Session: Milestone 1 slice A — controller, plus correction pass

**Outcome**: `src/codeagent/controller.py` (RunController + Protocols),
`tests/support/fakes.py` (test doubles moved out of production code),
`tests/integration/test_controller.py`. 697 tests passing.

- First version of the controller used a single `attempt` counter for
  both plan revisions and verification repairs, and indexed the fake
  verifier's outcome list by that same counter. Review (by the user,
  reading actual behavior rather than trusting passing tests) found
  this let a plan revision silently consume repair budget and skip a
  verification outcome — the 685 tests passing at the time didn't cover
  it because no test exercised revision and repair in the same run.
  Fixed by separating four previously-conflated counters: the event
  `iteration` index (pass_index), approval-visit index, verification-
  attempt index, and two real counts (repair_iterations_used,
  plan_revisions_used) checked against independent budgets.
- Same review found an unbounded loop: a fake approval provider that
  always returns REVISION_REQUESTED ran forever, since no plan-revision
  budget existed. Added `max_plan_revisions` with the same
  "count-used-vs-max" pattern as repair iterations, terminating via
  `BudgetExceeded(kind=PLAN_REVISIONS)`.
- **error_id reversal (author decision, evidence-driven, not
  preemptive)**: `errors.py`'s original design explicitly deferred
  `OperationalError.error_id` until "a real cross-event reference need
  appears." The controller's patch-failure path produced exactly that
  need — one failure occurrence represented by both a `ToolCompleted`
  and the `RunFinished` that cites it, with no way to prove they're the
  same occurrence. Added `error_id: str` (validated nonempty) to
  `OperationalError`; the controller generates one id per failure and
  reuses the same object on every event representing it. No ADR — this
  is exactly the kind of decision the deferral's own stated
  reconsideration condition anticipated, not a new architectural
  question.
- Decoupled production orchestration from test doubles: `RunController`
  now depends only on four narrow Protocols (`ModelClient`,
  `ApprovalProvider`, `Verifier`, `PatchApplier`) plus `Clock`; concrete
  fakes (including a new `SteppingClock` for reproducible timestamps
  without real time or sleeping) moved to `tests/support/fakes.py`.
  `FixtureScenario`/`FixturePlan` were removed in favor of a smaller
  `RunConfig` (real run parameters only) plus the fakes' own
  constructor arguments (scripted decisions), since conflating the two
  was part of what made the counter bug hard to see.
- Added `PolicyDecisionRecorded` (request→policy→completion ordering)
  and `CheckpointCreated` (preceding every `PatchApplied`, chained via
  `parent_checkpoint_id`) — the prior version's audit trace was
  incomplete in exactly the way a real reviewer would notice first.
- This is still explicitly "Milestone 1, slice A," not Milestone 1
  completion — no real worktree, executor, or model integration exists;
  see controller.py's module docstring.

## Session: Milestone 1 slice B — real worktree, real patch, real checkpoint

**Outcome**: `src/codeagent/workspace.py` (GitWorktree), `src/codeagent/
patch.py` (GitPatchApplier), a version-controlled `tests/fixtures/
retry_worker` fixture materialized into a real throwaway Git repo per
test, and 20 new focused tests. 717 tests passing.

- `PatchApplier.apply()` changed from `(iteration, plan) -> bool` to
  `(run_id, iteration) -> PatchResult` — a structured result carrying
  real success/failure, the specific error code, actual changed paths,
  a real diff byte count, and a real commit hash. `RunController` was
  already written to only ever report what a `PatchResult` gave it, so
  this was a clean swap, not a `RunController` rewrite.
- Decision: a checkpoint's `checkpoint_id` *is* its real Git commit
  hash, not a separately invented id. Considered a synthetic id
  alongside the commit hash instead; rejected — two identifiers for the
  same thing invites drift, and the commit hash already is a stable,
  unique, verifiable identity. Not promoted to an ADR: real but narrow,
  reversible if a future milestone needs a synthetic id for reasons
  that don't exist yet (e.g. content-addressing before a commit
  exists).
- `RunConfig` gained `repository_path` and `initial_checkpoint_id` so a
  real run can report its real worktree path and chain its first
  checkpoint to the worktree's actual starting commit — previously
  `repository_path` was a hardcoded placeholder string and the first
  checkpoint's parent was always `None`.
- Timing: replaced the fixed `_tick(0.01)` fabrication for the
  apply_patch step specifically with real `Clock.monotonic()`-measured
  elapsed time, since that step can now do real I/O. Other steps
  (model/approval/verifier) stay on the renamed `_fake_tick()` — they
  still have no real operation to time; inventing "real-looking"
  numbers for fake work would be its own dishonesty.
- Fixed the `tests.support` import path to be `tests.support.fakes`
  (via `tests/__init__.py` + `pythonpath = ["src", "."]`) instead of a
  bare top-level `support` module — the prior name was one `sys.path`
  collision away from silently importing the wrong package.
- Corrected `errors.py`'s wording: it said "no controller has been
  built," which stopped being true at slice A. The real gap is a
  *considered* retry/abort/escalation policy and real integrations, not
  the controller's existence.
- Validated by hand before writing formal tests (per this project's
  established discipline): the full real flow (fixture repo → worktree
  → patch → commit → original-checkout diff → cleanup), every
  validation-failure path (absolute path, `..`, missing file, ambiguous
  match, symlink escape), a path containing spaces, and a source repo
  with pre-existing uncommitted/untracked changes — all confirmed
  working via a throwaway script before any test asserted it.

## Session: Milestone 1 slice B — correction pass (not yet committed)

**Outcome**: `workspace.py`/`patch.py`/`controller.py` hardened against
10 gaps the 717-test slice-B suite didn't cover, found by the user
reading the actual code rather than trusting green tests. 731 tests
passing. Held uncommitted for review before this becomes part of
slice B's commit — the corrections aren't optional polish, they're
what makes the "one controlled patch" claim actually true.

- **Author decision, recorded not re-litigated**: `GitPatchApplier`
  now enforces exactly one `PatchOperation` at construction
  (`len(operations) != 1` raises). Multiple operations against the
  same file each started from the original content and could silently
  overwrite each other — real multi-file transactional patching stays
  Milestone 2 / the atomic-patch spike's job, not something to
  half-build here under time pressure.
- `git add -A` → `git add -- <validated target>`: staging everything
  in the worktree could have swept an unrelated change (from a prior
  failed attempt, a concurrent process) into the checkpoint commit.
- Sanitization: `OperationalError.message` no longer includes raw git
  stderr (replaced with fixed, per-step categorical messages: "failed
  to stage the patched file", "failed to commit the patch", etc.) or
  absolute worktree paths; caller-supplied path values are stripped of
  non-printable characters and length-bounded before inclusion.
  Verified by a test that forces a git failure and asserts the
  worktree's real absolute path and the word "fatal:" never appear in
  the persisted message.
- `GitWorktree.__exit__` was claiming success it hadn't earned: a
  failed `git worktree remove` was silently ignored (`check=False` with
  no return-code check). Now: physically remove the directory as a
  fallback, run `git worktree prune` to clean the now-stale
  registration, and only if that still doesn't resolve it, raise
  `GitWorktreeCleanupError` — but never when an exception is already
  propagating from the `with`-block (that would replace/mask the real
  failure); the cleanup outcome is always recorded on
  `self.cleanup_error` regardless of whether it's raised. Temp-dir
  removal moved into a `finally` so it happens even if the git-level
  recovery logic itself throws.
- Temp-directory prefix is now a constant (`codeagent-worktree-`), not
  `f"...{run_id}..."` — an unsanitized, unbounded caller-supplied
  string had no business being a filesystem path component.
- `PatchResult.__post_init__` now enforces its own success/failure
  shape (nonempty commit hash + positive counts on success; all-zero/
  `None` on failure) — this used to only be true by convention.
- Added a controller-level fail-closed check: a patch reporting changed
  files outside `PlanProposal.proposed_file_paths` aborts the run
  (`INTERNAL_INVARIANT_VIOLATION`) rather than proceeding to
  verification. Documented explicitly where this runs relative to the
  mutation: *after* it, not before — true prevention would require
  passing approved paths into the `PatchApplier` before it acts, which
  needs a richer interface than this slice builds. What this check
  actually guarantees: an out-of-scope change is never treated as
  legitimate, and no further budget is spent verifying it.
- The original-checkout-protection claim was narrower than what was
  actually being tested: prior tests checked one fixture file plus HEAD
  plus status. Added a test comparing a full sha256 manifest of every
  file in the source repo, a full `git for-each-ref` inventory, and the
  worktree-list snapshot before creation vs. after cleanup — the claim
  is now backed by what's actually inventoried and compared, not
  asserted past what was checked.
- No new ADR: none of the ten corrections is a durable decision with a
  credible alternative someone would reasonably choose differently —
  they're bug fixes and hardening of the slice-B design already
  recorded in the prior session's entry.

## Session: Milestone 1 slice B — second correction pass (not yet committed)

**Outcome**: approved-path enforcement is now genuinely preventive, not
just a postcondition; several remaining honesty/robustness gaps closed.
735 tests passing.

- `PatchApplier.apply()` gained a third parameter, `approved_paths`,
  checked by `GitPatchApplier` *before* any validation, read, write, or
  git command — proven with a test asserting byte-identical content, an
  unchanged HEAD, and empty `git status` after an unapproved-target
  attempt. The controller's existing post-mutation check is now
  documented and named as defense in depth only — its docstring no
  longer calls it "fail-closed," since by the time it runs the mutation
  it's checking for would already have happened.
- Found and fixed a real bug while adding the exact-match worktree-
  registration parser (correction 4): comparing an unresolved temp path
  (`/var/...`) against git's own porcelain output (which resolves
  `/var` → `/private/var` on macOS) never matched, so the two cleanup-
  failure tests from the *previous* correction pass were silently
  passing for the wrong reason — `_registration_status` was returning
  "not registered" via a false negative, not via a real check. Fixed by
  resolving the path before comparison; both tests now fail loudly if
  the detection logic regresses.
- `_registration_status` returns a tri-state (`True`/`False`/`None`)
  instead of a bool: a failed `git worktree list` is `None` (unknown),
  never coerced to `False` (confirmed absent) — an unknown cleanup
  state now blocks a false "cleanup succeeded" claim.
- `GitWorktree.__enter__` rejects re-entry of an already-active
  instance, and now catches `OSError` alongside `CalledProcessError`
  (matching `__exit__`'s existing tempdir-cleanup-in-`finally`
  guarantee against the same class of failure).
- `GitPatchApplier` catches `OSError` alongside `CalledProcessError`
  around every git subprocess stage (add/diff/commit/rev-parse), not
  just some of them.
- Documented explicitly, not just implicitly: a git failure *after* a
  successful file write (during add/diff/commit/rev-parse) leaves that
  write uncommitted in the worktree until the surrounding `GitWorktree`
  tears the whole thing down. No in-place rollback within one `apply()`
  call — real transactional rollback is Milestone 2 work, not
  something this narrow slice claims to provide.
- No new ADR: `approved_paths` becoming a real parameter closes a gap
  already named and reasoned about in the prior session's entry — it
  isn't a new decision with alternatives, it's finishing the one
  already made.

**Tracked limitation, accepted for now**: `GitWorktree.__exit__`'s
`git worktree remove`/`prune` calls run through `_run_git(..., check=False)`,
which still lets an OS-level failure to even *launch* git (e.g. the
binary missing or unexecutable, raising `OSError`/`FileNotFoundError`
before any `CompletedProcess` exists) propagate out of `__exit__`
unnormalized — not caught and turned into `GitWorktreeCleanupError`
the way a nonzero exit code is. `__enter__`'s equivalent call is
already hardened against this (this session's correction 3); `__exit__`
is not, and Slice B accepts that gap rather than fixing it
speculatively. Validate and close it during the Stage-2 interruption/
orphan-cleanup spike, where this class of failure is the actual subject
under test.

## Note: minor documentation inaccuracy, tracked not fixed by rewriting history

Commit `528ea8d`'s message says "the six tools the model is allowed to
request" — `domain.ToolName` has exactly five (`list_directory`,
`read_file`, `search_text`, `propose_plan`, `apply_patch`; the sixth,
`run_process`, was removed per ADR 0001 before that commit). No tracked
markdown doc repeats the error (checked). Not fixed by amending and
force-pushing that already-pushed commit — that rewrites shared history
for a wording-only fix in a message, not the code. If this project ever
writes public-facing prose (README, portfolio writeup) that references
tool count, say five, not six.
