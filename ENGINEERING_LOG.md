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

---

## Session: Milestone 1 slice C — real Docker verification, real E2E, report

**Outcome**: `src/codeagent/executor.py` (`DockerVerifier`), a
`VerificationResult` frozen dataclass replacing the old
`SUPPORTED_VERIFICATION_OUTCOMES`/`baseline_outcome`-on-`RunConfig`
scheme, `src/codeagent/report.py`, and one real end-to-end
demonstration: temporary Git worktree → real failing baseline in
Docker → fake model plan → fake approval → real controlled patch and
checkpoint → real passing verification in Docker → typed events →
text/JSON report → unconditional cleanup. Model and approval remain
fake; everything else in this flow is real.

- Two rounds of user-authored design correction were incorporated
  before any implementation: (1) stdlib `unittest` instead of pytest
  inside the container (no dependency install), read-only worktree
  mount, generated container name with guaranteed `docker rm -f` in a
  `finally`, `VerificationResult` as a frozen dataclass rather than a
  Protocol; (2) replacing `docker run --rm` with an inspectable
  lifecycle — `create` → `start --attach` (streamed, bounded output) →
  `inspect` for the container's own recorded exit state → `rm --force`
  in `finally` → a second `inspect` to *confirm* absence — because a
  cleanup attempt is not a cleanup guarantee. A Docker CLI client's own
  exit code is never used to classify PASSED/TEST_FAILURE; only the
  container's inspected `State.Status`/`State.ExitCode` are trusted.
  If absence can't be confirmed after cleanup, the outcome is always
  `ENVIRONMENT_FAILURE`, overriding what would otherwise have been a
  passing result.
- `VerificationResult.__post_init__` reuses `events.py`'s own
  `_validate_exit_code_for_outcome`/`_validate_error_for_verification_outcome`
  directly, so its validation cannot silently drift from
  `BaselineRecorded`/`VerificationCompleted`'s — parity by
  construction, not by two independently written rule sets.
  `tests/unit/test_verification_result_parity.py` sweeps every
  (outcome, exit_code, error) combination against both and asserts
  they always agree, specifically to catch a future regression where
  someone inlines a second copy of the rules.
- Controller disposition, corrected per the second round: a baseline
  or final-verification outcome that isn't PASSED/TEST_FAILURE (i.e.
  TIMEOUT/ENVIRONMENT_FAILURE/COMMAND_START_FAILURE) always aborts via
  `UNRECOVERABLE_ERROR` and does **not** consume repair-iteration
  budget — only a genuine TEST_FAILURE enters the normal repair loop.
- Bounded output collection (`_BoundedCollector`) drains stdout/stderr
  continuously through daemon threads while the container runs,
  capping each stream at 64 KiB with a deterministic truncation
  marker and safe (never-raising) UTF-8 decoding — not
  capture-then-truncate after the fact, which would defeat the point
  of a bound under adversarial output volume.
- Found and fixed a real bug during manual verification, not by
  inspection: `DockerVerifier` failed with `ENVIRONMENT_FAILURE`
  against a relative worktree path — Docker requires bind-mount
  sources to be absolute. Fixed by resolving the path in `__init__`;
  re-verified both the relative-path call and the full real E2E flow
  afterward.
- Found and fixed a second real bug, this time in the test suite
  itself: adding `tests/fixtures/retry_worker/tests/__init__.py` (a
  package marker needed so the fixture's own
  `python3 -m unittest tests.test_worker` resolves inside the
  container) made pytest collect that file *on the host* too, where
  its bare `tests` package name collides with and shadows the real
  top-level `tests` package, breaking every test that does
  `from tests.support import ...`. Fixed with
  `norecursedirs = ["tests/fixtures"]` in `pyproject.toml` — the
  fixture's `tests` package is only ever meant to run inside the
  verification container, never collected by the host's pytest.
- Rejected: leaving `RunConfig.baseline_outcome`'s validation in place
  after removing the field — the "unsupported baseline outcome"
  concern moved to `FakeVerifier`'s own construction-time validation
  instead, since it was always a synthetic-harness restriction
  (`FakeVerifier` only fabricates PASSED/TEST_FAILURE; a real executor
  like `DockerVerifier` is not restricted this way), not a domain rule
  belonging on `RunConfig`.
- Explicitly provisional, unchanged from the correction rounds: the
  configured resource limits (`--memory`, `--cpus`, `--pids-limit`,
  `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--network none`) have not been adversarially tested, and behavior
  under a host-process kill mid-verification is untested — both remain
  the Stage-2 isolation/interruption spikes' job. Only exercised on
  macOS/arm64 (Docker Desktop); no Linux-parity claim is made.
- **Verification performed**: `tests/unit/test_executor.py` (14 tests,
  mocked `docker` CLI boundary — OSError launch failures at both
  `create` and `start`, nonzero `create`, timeout, unparseable
  `inspect` output, PASSED, TEST_FAILURE, unconfirmed-cleanup override,
  bounded-collector truncation/UTF-8 safety);
  `tests/unit/test_verification_result_parity.py` (61 parametrized
  cases); `tests/unit/test_report.py` (6 tests, built from real
  controller runs against fakes, not hand-assembled events);
  `tests/integration/test_slice_c.py` (3 tests against a real local
  Docker daemon — full E2E, baseline-only, and a cleanup-after-
  container-level-failure check — skipped, not weakened, on a machine
  without Docker). Full suite: 819 passed. `git diff --check`: clean.
  Manually confirmed via `docker ps -a` before and after both the ad
  hoc smoke run and the formal integration-test run: no leftover
  `codeagent-verify-*` containers in either case.
- No new ADR: the corrected Docker lifecycle and outcome-classification
  rules were fully specified by the user before implementation began:
  translating an already-made decision into code isn't a new
  consequential decision with credible alternatives left undiscussed.

**Not yet done**: `report.py` has no persistence (JSONL) and no CLI
entry point — out of slice C's stated scope. The Stage-2 isolation and
interruption spikes remain unrun; nothing in this slice's sandboxing
should be read as passing them in advance.

---

## Session: Milestone 1 slice C — correction pass (pre-review)

**Outcome**: seven real defects in the uncommitted slice C
implementation, found by user-directed review before first commit, all
fixed and re-verified against real Docker. No scope change; still
Milestone 1 slice C.

- **Real bug (unsafe cleanup confirmation)**: `_cleanup()` treated any
  nonzero `docker inspect <name>` as proof of absence — but a broken
  Docker daemon/client also returns nonzero, which would have been
  misread as "confirmed removed." Fixed by confirming via
  `docker ps -a --format "{{.Names}}"` and an exact string match
  against the generated name; a nonzero listing, an OSError launching
  it, or the name still present are all "unconfirmed," never
  "confirmed absent." Covered by five new `_cleanup()`-level tests
  (absence, still-present, nonzero listing, launch OSError, similarly-
  named-but-distinct containers — the last specifically to prove the
  match is exact, not substring/prefix).
- **Real bug (cleanup could be skipped by an early return)**:
  `_execute()` returned directly from inside the create/start
  exception handlers in some paths, which meant cleanup's ordering
  relative to the returned outcome wasn't structurally guaranteed —
  correct by the specific cases tested, not by construction. Restructured
  into `_attempt()` (never raises; converts every failure into a
  returned provisional outcome) plus `_execute()` (always calls cleanup
  on the provisional result, unconditionally, before constructing the
  final `VerificationResult`). Added a combined test: `start` fails to
  launch *and* cleanup is unconfirmed → final outcome is
  ENVIRONMENT_FAILURE, not COMMAND_START_FAILURE — proving the
  cleanup-unconfirmed override applies regardless of which provisional
  outcome preceded it.
- **Design gap (two sources of truth for the verify command)**:
  `RunConfig.verify_command` and `DockerVerifier`'s own configured
  command could disagree, and the event trace recorded the former even
  though the latter is what actually ran. Fixed by making `Verifier` a
  read-only `command` property — the single source of truth — removing
  `RunConfig.verify_command` entirely, and recording
  `verifier.command` on `RunStarted`/`BaselineRecorded`/
  `VerificationCompleted`. `FakeVerifier` gained the same property
  (defaulting to `tests.support.fakes.FIXTURE_VERIFY_COMMAND`, the same
  value as `executor.DEFAULT_COMMAND`). New test asserts the event
  trace always records exactly what `verifier.command` exposes.
- **Layering violation**: `controller.VerificationResult` was calling
  two underscore-prefixed events.py functions directly — production
  code reaching into another module's private internals. Fixed by
  adding `events.validate_verification_outcome_shape()` as the one
  public entry point, used by `BaselineRecorded`, `VerificationCompleted`,
  and `VerificationResult` alike; parity is unchanged (still by
  construction, same shared function) and a new test monkeypatches the
  public function to always raise, proving `VerificationResult` really
  calls it rather than a same-named local copy.
- **Real bug (fabricated real-world duration)**: the slice C
  integration tests used `SteppingClock` (a fixed 1ms-per-call fake)
  for a *real* Docker execution, which would have reported a fabricated
  sub-millisecond duration for work that actually takes over a second.
  Fixed by switching `tests/integration/test_slice_c.py` to
  `SystemClock` for the verifier and controller, asserting durations
  are finite and non-negative rather than pinning an exact value.
  `SteppingClock` remains correct and unchanged for the deterministic
  unit/controller tests, which have no real operation to time.
- **Hardened `report.build_report()`** (previously accepted any
  nonempty list with a `RunFinished` in it, including mixed run_ids,
  reordered/gapped sequences, and events after the finish): now
  requires a single run_id, list-order-contiguous sequence numbers,
  exactly one `RunStarted`, exactly one `RunFinished`, and that
  `RunFinished` is the final event. Multiple `PatchApplied` events (a
  repair loop) now aggregate `changed_paths` as a deduplicated,
  first-seen-order union, with `checkpoint_commit` taken from the
  *latest* `PatchApplied`, not the first. Eight new failure-path tests
  built by mutating one real, valid trace via `dataclasses.replace`
  (mixed run_id, sequence gap, reordering, missing start, missing
  finish, duplicate finish, trailing event after finish) plus one
  multi-patch aggregation test.
- **Hardened `DockerVerifier.__init__`**: now rejects a missing or
  non-directory worktree, an empty image, an image not pinned by
  digest (`"@sha256:" not in image`), an empty command or a command
  containing an empty element, and a non-finite or non-positive
  `timeout_seconds` — all previously unchecked at construction time.
  Nine new construction tests, one per rejected case plus one
  confirming the pinned default image still passes.
- **Verification performed**: full suite 845 passed (was 819; +26 net
  new tests across the corrections above, no test count regression).
  `tests/integration/test_slice_c.py`'s 3 real-Docker tests re-run and
  passed individually and as part of the full suite. `py_compile` on
  every touched file. `git diff --check` clean. `docker ps -a` showed
  no `codeagent-verify-*` containers before or after every run in this
  session.
- No new ADR: every correction here is a bug fix or a tightening of an
  already-agreed design (single-source-of-truth command, public
  validator, stricter construction/report validation) — none introduces
  a new consequential decision with credible alternatives left
  undiscussed.

**Still nothing committed or pushed** — this correction pass, like the
slice C implementation it corrects, remains uncommitted pending
explicit user go-ahead.

---

## Session: Milestone 1 completion — real read-before-plan

**Outcome**: closed a real acceptance-criterion gap identified by
review, not by inspection of my own prior work: the implementation
guide's exact Milestone 1 requirement is "Using a deterministic fake
model and a real fixture repository, perform a full
read-plan-approve-patch-verify-report flow," and the controller never
performed or recorded a repository read — plan-approve-patch-verify-
report only. Fixed with the smallest slice that satisfies the wording,
explicitly not Milestone 2's general repository-read/multi-tool
system.

- New `src/codeagent/controller.py` additions: `ReadResult` (frozen
  dataclass, same verbatim-report discipline as `PatchResult`/
  `VerificationResult`), `RepositoryReader` Protocol (`read_file`
  only — no listing/pagination/search), and `ModelClient` changed from
  one-shot `propose_plan()` to two-step `request_read_path()` then
  `propose_plan(read_result)`. Documented explicitly on both new
  Protocols: this is not the future live-provider shape (arbitrary,
  budget-aware multi-tool calls in any order) — it exists only to make
  today's deterministic fake model genuinely evidence-driven.
- New `src/codeagent/reader.py`: `WorktreeFileReader`, the real
  implementation — one UTF-8 file, `MAX_READ_BYTES = 64 KiB`, rejects
  absolute paths, `..`, and symlink escape (mirrors
  `codeagent.patch.GitPatchApplier`'s path-validation discipline,
  deliberately duplicated rather than shared — sharing would have
  meant widening patch.py's private surface for a few dozen lines).
  Every failure returns a structured `ReadResult`, never a propagated
  filesystem exception.
- `RunController` gained a required `reader: RepositoryReader`
  constructor parameter and now dispatches READ_FILE in EXPLORE (full
  ToolRequested/PolicyDecisionRecorded/ToolCompleted audit trail)
  *before* the existing PROPOSE_PLAN dispatch, passing the real
  `ReadResult` into `propose_plan`. `PlanProposed.evidence_refs` now
  carries the read's `tool_call_id` — a real cross-reference the schema
  already supported but nothing populated until now. Persisted
  `ToolCompleted.result_summary` is bounded metadata only ("read N
  byte(s) from `path`") — never raw file content; verified by a test
  that scans every string field of every event in a full run for a
  planted secret marker.
  A failed read aborts the run as UNRECOVERABLE_ERROR without
  consuming repair/revision budget — same placeholder-disposition
  shape as a failed patch (`_dispatch_apply_patch`), not a considered
  retry policy.
- `tests/support/fakes.py`: `FakeModel` updated to the two-step
  Protocol (defaults its read path to the plan's own first proposed
  file, records `last_read_result` for inspection); new
  `FakeRepositoryReader`; new `MarkerGatedFakeModel` — a test-only
  model whose `propose_plan` raises unless it actually received the
  expected marker in real read content, used specifically to prove the
  controller passes genuine evidence through rather than the model
  proposing independently of the read it triggered (with its own
  "does this gate actually fail" sanity test, so the proof isn't
  vacuous).
- `tests/integration/test_slice_c.py`'s real Docker E2E test now
  demonstrates the complete real sequence end to end: real worktree →
  real READ_FILE → real content compared byte-for-byte against the
  original checkout → `MarkerGatedFakeModel` gated on the fixture's
  actual `# BUG:` comment → fake approval → real patch + checkpoint →
  real Docker verification → report, with read-before-plan sequence
  ordering asserted by event sequence number, and the original
  checkout/Docker-cleanup guarantees re-verified intact.
- New `tests/unit/test_reader.py` (17 tests): real file read from a
  real fixture repo; absolute path, `..`, symlink escape, and a
  nonexistent nested path all rejected without ever touching or
  leaking the host content they'd otherwise expose (asserted directly:
  a planted "secret" file's content never appears in the error
  message); oversized (over `MAX_READ_BYTES`) and non-UTF-8 files fail
  structurally, not via a propagated exception; `ReadResult`
  construction invariants.
- Small corrections bundled into the same pass (item 7 of this
  session's review): `test_slice_c.py`'s `_no_stray_containers` now
  requires `docker ps` itself to have exited 0 before trusting empty
  stdout as "confirmed clean" (a failed listing's empty output would
  otherwise look identical to genuine cleanliness); `DockerVerifier`'s
  digest-pin check now requires the complete shape (`@sha256:` plus
  exactly 64 hex characters via a regex, anchored at the string's end)
  instead of `"@sha256:" in image` substring presence, which would
  have accepted a truncated or malformed digest; four stale
  `"pytest tests/test_worker.py"` `verification_intent` strings
  (left over from before the fixture's real command became
  unittest-based) replaced with `"python3 -B -m unittest
  tests.test_worker"`.
- **Verification performed**: full suite 871 passed (was 849 before
  this slice's tests; +22 net new tests: 17 in test_reader.py, 5 new
  read-flow tests in test_controller.py, offset by 0 removed — the two
  pre-existing tests that needed updating for the new event count/
  constructor signature were fixed in place, not replaced).
  `tests/integration/test_slice_c.py`'s 3 real-Docker tests re-run and
  passed individually and in the full suite. `py_compile` clean on
  every touched file. `git diff --check` clean. `docker ps -a` showed
  no `codeagent-verify-*` containers after every run.
- Two real bugs found while wiring this in (not by inspection —  by
  running the new real E2E assertions and watching them fail): (1) the
  existing `test_every_tool_dispatch_emits_policy_decision_between_
  request_and_completion` test asserted exactly 2 ToolRequested events
  by a stale hardcoded count; fixed to 3 (read_file, propose_plan,
  apply_patch). (2) The real E2E test's first attempt at proving
  "real content reached the model" compared the model's captured
  `ReadResult.content` against the worktree's file *after* `controller.
  run()` returned — by then the patch had already been applied, so it
  compared post-patch content against a read that happened pre-patch,
  and failed. Fixed by comparing against `before_content`, captured
  from the original checkout before the worktree or any patch existed
  — the actually-correct expected value, since the read happens in
  EXPLORE, strictly before EXECUTE applies the patch.
- No new ADR: `ModelClient`'s two-step shape and `RepositoryReader`'s
  narrow surface are both explicitly documented as provisional
  stand-ins for later, undesigned work (the real multi-tool model loop,
  Milestone 2's general repository toolkit) — not durable decisions
  with credible alternatives being chosen between now.

**Still nothing committed or pushed** — remains uncommitted pending
explicit user go-ahead, per this project's git safety rule.

---

## Session: pre-commit correction pass — reader bound, report position check

**Outcome**: four correctness/documentation fixes on the still-uncommitted
Milestone 1 work, found by review. No ADR — correctness fixes and
documentation precision, not new decisions.

- `WorktreeFileReader.read_file` no longer `stat()`s then
  `read_bytes()`s the whole file: it opens in binary mode and reads at
  most `MAX_READ_BYTES + 1` bytes, rejecting on the extra byte. The old
  stat-then-read-all pattern would have read and held an arbitrarily
  large file in memory if it grew between the stat and the read;
  bounding the read call itself removes that. Proven by a new test that
  monkeypatches the file object's `read` to record the requested size
  against a file 5x the limit, asserting exactly one `read(MAX_READ_BYTES
  + 1)` call — not merely that a big file is rejected, but that the
  mechanism itself is bounded.
- Documented the remaining TOCTOU race honestly in reader.py's module
  docstring: path validation and the actual open are still two separate
  filesystem operations, so a concurrent replacement of a path
  component between them is a real, unclosed race. Assigned to
  Milestone 2 (descriptor-relative/openat-style resolution); explicitly
  not attempted here. No documentation now claims this reader is
  TOCTOU-safe.
- `report.build_report()` now requires `RunStarted` to be the *first*
  event, not merely present exactly once — a trace with one RunStarted
  event sitting in the middle previously passed validation. New test
  moves RunStarted to the midpoint of an otherwise-valid trace,
  re-sequences by list order, and proves rejection.
- Fixed a misleadingly-named reader test:
  `test_rejects_resolved_outside_worktree_path_without_reading_host_content`
  set up an unused "secret file" and claimed to test containment escape
  via resolution, but a bare nonexistent nested path never exercises
  escape at all (no `..`, no symlink — `Path.resolve()` has nothing to
  escape through). Renamed to
  `test_rejects_nonexistent_nested_target_path` with a docstring
  stating plainly what it does and doesn't prove, pointing at the real
  symlink-escape test as the actual containment-boundary evidence.
- **Verification performed**: `py_compile` clean on every touched file
  (`reader.py`, `report.py`, `test_reader.py`, `test_report.py`).
  Focused reader tests: 18 passed. Focused report tests: 15 passed.
  `tests/integration/test_slice_c.py` against a real local Docker
  daemon: all 3 executed (none skipped) and passed. Full suite: 873
  passed (was 871; +2 net new tests). `docker ps -a --filter
  name=codeagent-verify`: empty. `git diff --check`: clean.
- No new ADR.

**Still nothing committed or pushed.**

---

## Session: narrow Linux CI slice

**Outcome**: `.github/workflows/ci.yml` — one job running the full
suite, including real Docker verification, on GitHub-hosted
`ubuntu-24.04` (x86_64) + Python 3.12. Evidence for that platform only
— not a general Linux or ARM64 claim.

- `permissions: contents: read`; `timeout-minutes: 20`.
- `actions/checkout`/`actions/setup-python` pinned to commit SHAs
  (resolved via `gh api`, not guessed), each with a `# vX.Y.Z` comment.
- `pytest==9.1.1` pinned via a new `[project.optional-dependencies]
  test` group in `pyproject.toml` — the test runner's version is
  pinned; this is not a full reproducible-install/lockfile guarantee.
- `docker info` runs as a mandatory preflight before anything
  Docker-dependent. The pinned verification image is pulled and its
  platform checked equals `linux/amd64` (confirmed via `docker
  manifest inspect` that the digest is a multi-arch index containing
  that platform).
- New `CODEAGENT_REQUIRE_DOCKER=1` mechanism in `test_slice_c.py`:
  fails instead of skipping when Docker is unavailable, used only in
  CI. Final `if: always()` step lists `codeagent-verify` containers
  and fails on any leftover, without deleting anything.
- **Verification**: `actionlint` clean; full local suite 873 passed;
  the 3 real-Docker tests executed (not skipped) under
  `CODEAGENT_REQUIRE_DOCKER=1`; no leftover containers; `git diff
  --check` clean. Not yet run on GitHub Actions itself.
- No new ADR.

**Nothing committed or pushed yet.**
