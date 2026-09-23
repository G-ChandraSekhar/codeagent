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
  --check` clean.
- No new ADR.

**Committed as `ci: validate Docker flow on Ubuntu`; pushed to
`origin/main`. First real GitHub Actions run — commit `a845cb3`, run
[34734760525](https://github.com/G-ChandraSekhar/codeagent/actions/runs/34734760525)
— concluded `success`: Docker preflight passed (Docker Engine
28.0.4); pulled verification image confirmed `linux/amd64`; the 3 real
Docker tests passed, 0 skipped; full suite 873 passed; final
leftover-container check found none. This is the first real evidence
of this project running anywhere other than the author's macOS/arm64
machine.**

---

## Session: S3 spike — multi-file patch atomicity — decision accepted as ADR 0003

**Outcome**: Stage-2 spike S3 (`spikes/s3/`) found three genuinely
different guarantees, not one: (1) prevalidation atomicity is real —
an invalid proposal is rejected byte-for-byte unchanged before any
write; (2) a handled mid-application failure leaves a genuinely
observable partial state before any rollback runs, and in-place `git
checkout` rollback was demonstrated only for one tracked,
previously-clean file; (3) a SIGKILL leaves a partial worktree
detectable only by convention (no lock file, no atomic rename, no
transaction marker backs it) — correctly named "interruption
detection," not "crash consistency."

**Decision**: author accepted **B2 + C** for v1 — recorded as
`docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`
(Accepted). On a handled failure, discard and recreate the disposable
worktree from the last accepted checkpoint rather than per-file
rollback (**B1 rejected**: more untested failure modes — new files,
a rollback command that itself fails — than B2's single primitive).
Gate new/resumed patch attempts on expected HEAD + a clean tree/index.
True crash-consistent filesystem mutation (**option D**) is explicitly
deferred beyond v1 as out of scope/complexity, not guaranteed.

- **Verification**: 15 spike-specific tests passed; main suite
  unaffected at 873 passed; `git diff --check` clean; scratch root
  removal verified.
- **Remaining Milestone 2 obligations** (no implementation yet):
  failure-path tests for disposal/recreation failure, file
  add/delete/rename, and unexpected dirty/index states at resume —
  none covered by S3's evidence. Full narrative in the ADR and
  `spikes/s3/S3_RESULT.md`.

---

## Session: S4 spike — executor/container isolation, evidence on both platforms

**Outcome**: Stage-2 spike S4 (`spikes/s4/`) behaviorally validated the
actual production `DEFAULT_IMAGE`/`_SECURITY_FLAGS` (imported, never
retyped) — real syscalls, real escalation attempts, real cgroup
accounting, negative controls throughout — on macOS/Docker Desktop
first, then on Linux/x86_64 via a new manual
`.github/workflows/s4-linux-evidence.yml` workflow. 12–13 of 14 checks
`PASS` on both platforms with a validated control; two things are
genuinely open and not fixed (`src/codeagent` untouched throughout):
(1) `--memory` doesn't cap the combined memory+swap allowance
(reproduced on both platforms — same `MemorySwap`/`Memory` values),
and `DockerVerifier` can't distinguish an OOM kill from an ordinary
test failure (strongly inferred, not directly proven, on both); (2)
Linux-only: a CAP_SYS_ADMIN negative control was blocked by an
unidentified runner restriction (errno 13, `INCONCLUSIVE` — not
attributed to seccomp/AppArmor without further evidence, never weakened
to force a pass).

**A real harness bug was found and fixed before the Linux evidence
could be trusted**: `tempfile.mkdtemp()`'s default `0700` mode, owned
by the runner's own UID (`1001`), blocked the container's configured
UID `1000` from even traversing its own intended-readable test
fixture — reported as `mounts_and_host_isolation: FAIL`, not a real
isolation finding. Fixed by chmod-ing only that one fixture (not
scratch directories generally, not the deliberately-restrictive
secret-canary directory), replacing one combined assertion with four
independently-reported probe fields, and adding a
`classify_mounts_check()` function (unit-tested) that keeps a real
security finding `FAIL` while treating an inaccessible fixture or
`docker exec` infra failure as `TECHNICAL_FAILURE`, never a false
security `FAIL`. Both the failed and corrected Linux runs are retained
under `spikes/s4/evidence/linux-x86_64/run-<id>/`, each with its own
`RUN_INFO.json` recording the workflow URL and source commit.

Also fixed in this pass: the workflow's own leftover-container check
used to pipe `docker ps -a` straight into `grep ... || true`, which
would have silently swallowed a *failed* listing (daemon down) as if
it were a confirmed-empty one; the listing is now captured as its own
command so its failure propagates, with only `grep`'s "no match" exit
code suppressed. Verified against three stubbed cases (empty listing,
failed listing, matching leftover) before pushing.

- **Verification**: 28 spike-specific tests pass (both locally and in
  CI, both platforms); main suite unaffected at 873; `actionlint`
  clean on the workflow; cleanup independently confirmed empty on both
  platforms (Class A prefix-delta and Class B manifest tracking both
  clean).
- **Remaining author decisions** (none made yet): whether v1 needs an
  explicit `--memory-swap` limit; how `DockerVerifier` should classify
  an OOM kill; whether to investigate the Linux CAP_SYS_ADMIN
  restriction further. S5 (interruption without orphaned containers)
  remains unstarted. No ADR yet — these are still open questions, not
  decisions.

---

## Session: Milestone 3 — executor hardening from S4 evidence (memory/swap, OOM classification)

**Outcome**: both S4-derived open questions are now resolved in
`src/codeagent/executor.py` (no ADR — routine security-config
tightening plus a mechanical taxonomy addition). S4's retained
evidence observed `Memory=512 MiB` / `MemorySwap=1024 MiB` (~512 MiB of
extra swap) on both macOS Docker Desktop and native Linux Docker
Engine; `_SECURITY_FLAGS` now adds `--memory-swap 512m` to close that.
Final-state inspection now parses `docker inspect --format
'{{json .State}}'` (`Status`/`ExitCode`/`OOMKilled`, strictly typed,
fail-closed to the existing `EXECUTOR_ENVIRONMENT_FAILURE` on any
malformed field) instead of a fragile tab-separated format. A
confirmed `OOMKilled` now takes precedence over `PASSED`/`TEST_FAILURE`,
producing `ENVIRONMENT_FAILURE` with the new
`ErrorCode.EXECUTOR_OOM_KILLED` and the observed exit code preserved —
only a fixed sanitized message is persisted, never the raw inspect
payload. `events.py` now accepts either `EXECUTOR_ENVIRONMENT_FAILURE`
or `EXECUTOR_OOM_KILLED` for `ENVIRONMENT_FAILURE`; every other
outcome/code pairing stays fail-closed. The unconditional
cleanup-confirmation override is unchanged and still takes precedence
over a would-be OOM result. Linux CAP_SYS_ADMIN stays `INCONCLUSIVE` —
no capability/seccomp/AppArmor/runtime change.

- **Verification**: full suite passing (unit + integration, including
  a new controller test proving OOM propagates to `UNRECOVERABLE_ERROR`
  without entering the repair loop or emitting `BudgetExceeded`); the 3
  real-Docker `test_slice_c.py` tests pass against the new flag and
  inspection format; `git diff --check` clean; no leftover
  CodeAgent-owned containers.
- **Remaining risk**: `--memory-swap` itself has not been re-verified
  with new adversarial S4-style evidence on either platform (S4's
  bundle is retained, not rerun). Linux CAP_SYS_ADMIN attribution and
  S5 (interruption without orphaned containers) remain open.

---

## Session (2026-09-13): S4 post-hardening follow-up validated on both platforms

Closes the Milestone 3 entry's re-verification gap above:
`--memory-swap`/OOM classification are now confirmed with real
adversarial Docker evidence on **both** macOS/arm64 and Linux/x86_64
(`s4-m3-followup-linux-evidence.yml` run `34769425851` attempt `1`),
via `spikes/s4/spike_s4_m3_followup.py` against the real unmodified
`DockerVerifier`. All three checks passed identically on both:
`HostConfig.Memory=536870912`/`MemorySwap=536870912` (exactly 512 MiB,
no extra swap, on both a hand-controlled twin and the real production
container); a genuine 1900 MB allocation → `ENVIRONMENT_FAILURE`/
`EXECUTOR_OOM_KILLED`, exit `137` preserved, fixed sanitized message;
an ordinary `sys.exit(1)` → `TEST_FAILURE`, no error. Cleanup clean on
both. Evidence:
`spikes/s4/evidence/macos-docker-desktop-arm64/run-m3-followup-20260913T160240Z-523c01b6/`
and
`spikes/s4/evidence/linux-x86_64/run-m3-followup-34769425851-attempt-1/`
(SHA-256-verified copy of the workflow artifact); full narrative in
`spikes/s4/S4_RESULT.md`.

- **Still open**: Linux `cap_sys_admin` remains `INCONCLUSIVE`
  (unrelated). S5 remains unstarted. No ADR (closes an already-recorded
  question with evidence, not a new decision).

---

## Session (2026-09-13): factual status reconciliation (S5, test count, Milestone 1 commit status)

Documentation-only correction, not a new decision — no ADR. `CLAUDE.md`,
`docs/STAGE2_EVIDENCE_PLAN.md`, and `docs/threat-model.md` (T-F1/T-F2)
had drifted from actual repository state: slice C's pre-commit
correction pass and the read-completion slice were both described as
"still uncommitted" despite having been committed since (Milestone 1 is
and remains complete); the current-status test count read `873` where
`931` is what the full suite now actually reports; and S5 (interruption
without orphaned containers) was still described as wholly "unstarted"
even though its macOS/arm64 spike evidence — six scenarios total (five
lifecycle outcomes plus fresh-process reconciliation), followed by a
separate idempotency check, against a real Docker daemon and a real
throwaway worktree — is complete and
committed at `d2a6f639826bb1368cc88a6233394b0c6b0ca7da`
(`spikes/s5/S5_RESULT.md`, status DRAFT). Corrected all three files to
state plainly: S5 macOS evidence exists and demonstrates the expected
SIGKILL orphan plus successful reconciliation, in throwaway spike
scaffolding only; Linux/x86-64 repetition is planned but not yet run;
no S5 mechanism (labeling, manifest/registry, locking, cancellation,
startup reconciliation) is implemented in production; and no candidate
architecture decision from the spike has been accepted. Neither T-F1
nor T-F2 is marked mitigated or resolved. `docs/STAGE2_EVIDENCE_PLAN.md`'s
original S5 experiment plan/hypothesis text is left as originally
written, with only a short factual status note added above it.

- **Verification**: `git status` confirmed clean at
  `d2a6f639826bb1368cc88a6233394b0c6b0ca7da` before editing; full suite
  re-run and confirmed at 931 passed; every corrected sentence checked
  against actual commit history (`git log`, `git ls-files`) and the
  retained `spikes/s5/S5_RESULT.md`; exactly four files changed in
  total — `CLAUDE.md`, `ENGINEERING_LOG.md`,
  `docs/STAGE2_EVIDENCE_PLAN.md`, and `docs/threat-model.md` — no code,
  tests, workflows, or ADRs touched.
- **Still open**: everything S5 already listed as open (Linux
  repetition, production implementation, candidate-decision acceptance)
  remains open — this session only corrected wording, it made no new
  decision.

---

## Session (2026-09-13): S5 interruption evidence reproduced on Linux/x86_64

Manual GitHub Actions run `34783737248` (attempt 1, source `3d25fa6`)
reproduced the macOS S5 result on Ubuntu 24.04/Linux x86_64 with real
Docker and a real throwaway Git worktree. All seven classifications
passed: normal completion, cooperative cancellation, SIGINT, SIGTERM,
expected SIGKILL orphaning, fresh-process reconciliation, and the
separate idempotency check. The SIGKILL child left its labeled
container and registered worktree exactly as expected; a fresh
reconciler removed them without changing the four canaries.

- **Evidence**: retained under
  `spikes/s5/evidence/linux-x86_64/run-34783737248-attempt-1/`, including
  the separately uploaded workflow diagnostics. Provenance and harness
  SHA-256 matched source commit `3d25fa6`; 175 focused S5 tests passed;
  harness cleanup and independent workflow baseline/final checks were
  clean, with no capture/comparison failures or leftovers.
- **Decision/scope**: evidence only. No label, registry, lock,
  cancellation, signal-ownership, or startup-reconciliation design is
  accepted; nothing is implemented in production. `S5_RESULT.md`
  remains DRAFT pending author review.

---

## Session (2026-09-15): S5 production decisions accepted (ADR 0004, ADR 0005)

Documentation only. The author accepted the S5 lifecycle architecture as
`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md` and
`docs/adr/0005-cancellation-and-signal-ownership.md`; mechanics live there.

- **Accepted decisions**: separate `lifecycle_id` (public `run_id`
  unchanged); per-user state root with repository namespaces; four
  required Docker labels and `baseline`/`verification` roles; bounded
  lifecycle projection plus typed maintenance trace (run events stay
  canonical); inode-verified, never-unlinked `flock` locks; fail-closed
  current-repository reconciliation; `codeagent reconcile` with
  resource-preserving abandonment (`ABANDONED_UNRESOLVED` never clean);
  replaced-repository namespaces fail closed as an accepted v1
  limitation; hidden checkpoint refs (ADR 0003 amendment pending);
  `VerificationOutcome.CANCELLED` with entrypoint-owned SIGINT/SIGTERM.
- **Guarantee**: recovery of owned resources plus interruption
  detection — never crash consistency.
- **Status**: nothing implemented; T-F1/T-F2 remain open until
  implementation and production acceptance tests pass.
- **Next step**: Milestone 2, starting with the ADR 0003 checkpoint-ref
  amendment.

---

## Session (2026-09-15): ADR 0003 Amendment 1 — hidden checkpoint refs

Documentation only; first Milestone 2 task. Accepted Amendment 1 to
`docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`; the
original decision text is unchanged.

- **Why**: checkpoints are detached-`HEAD` commits with no ref, so after
  discard they survive only until `git gc` — recreation "from the last
  accepted checkpoint" was not structurally guaranteed (code reading;
  no spike evidence).
- **Decision**: one hidden ref per lifecycle,
  `refs/codeagent/runs/<lifecycle_id>/checkpoint`, created at the
  starting commit and changed only by compare-and-swap
  `git update-ref --no-deref`; a commit is accepted only after all
  checks pass and the ref advances; discard-and-recreate keeps the ref,
  terminal teardown deletes it last; `refs/codeagent/` is never swept;
  `git push --mirror` visibility is documented.
- **Cross-ADR alignment**: ADR 0004 now uses a write-ahead
  `checkpoint_ref` transition record (`absent`/`creating`/`present`/
  `advancing`/`removing`) instead of one recorded SHA, and validates
  object IDs against the repository's own object format (SHA-1 or
  SHA-256), with operation-specific live-owner rollback: a failed
  terminal delete stays `removing`, never `present`.
  `ToolCompleted(success=True)` is delayed until the whole `apply_patch`
  transaction is accepted, and one failure occurrence has exactly one
  `OperationalError` (same `error_id` and code on every event).
  `ToolCompleted` legality is extended rather than translated. T-M1 now separates today's
  `GitWorktree` controls (including its `prune`/`rmtree` fallback,
  which Milestone 2 removes) from the accepted ones.
- **Status**: all mechanisms remain unimplemented. Milestone 2 owns ref
  mechanics and tests; ADR 0004 (Milestone 3) owns durable write-ahead
  and dead-run reconciliation.

---

## Session (2026-09-16): Milestone 2 slice A — checkpoint-ref primitive

**Outcome**: `src/codeagent/checkpoint_ref.py` implements ADR 0003
Amendment 1's ref mechanics only — no controller, worktree, patch,
lifecycle-store, reconciliation, cancellation, or CLI wiring. API:
`CheckpointRef(source_repo, lifecycle_id)` with `observe`, `create`,
`advance`, `delete`.

- Mutations run inside a `git update-ref --stdin` transaction: probing
  showed plain compare-and-swap does not stop a symbolic-ref
  substitution race, so `prepare` locks the ref and it is re-observed
  *while locked* before `commit`. Verified on `files` and `reftable`.
- Every invocation strips all `GIT_*` env vars (a hostile `GIT_DIR` had
  redirected `git -C <repo>` into another repository) and passes
  `-c core.hooksPath=/dev/null`, since clearing env vars does not stop
  a repository's own hooks from running host code — recorded as
  threat-model **T-M3**: `patch.py`/`workspace.py` are still exposed and
  must be fixed before patch integration; this module protects only
  itself.
- Mutation outcomes are decided by re-observing the ref, never assumed:
  the precise original cause (launch/timeout/protocol failure) survives
  when the ref turns out unchanged, and a still-unconfirmed transaction
  process fails closed with `TRANSACTION_CLEANUP_UNCONFIRMED` (chained
  from any abort failure) instead of silently returning.
- No new `ErrorCode`: a module-local `CheckpointRefError`, same split as
  `workspace.GitWorktreeError`, so the integrating slice still produces
  exactly one `OperationalError` per occurrence.
- **Verification**: 90 focused tests (real repositories), full suite
  1018 passed / 3 skipped (pre-existing Docker tests, no local daemon).
- **Next step**: T-M3 remediation in `workspace.py`/`patch.py`, then the
  lifecycle store that writes ADR 0004's `creating`/`advancing`/
  `removing` intent around these calls.

---

## Session (2026-09-16): ADR 0006 accepted — Git safety policy for T-M3

**Outcome**:
`docs/adr/0006-git-safety-policy-for-filters-hooks-and-content-fidelity.md`
is **Accepted**. **No production code changed — design only.**

- Real fixtures (hostile filters/hooks, a local fake `ext::`
  remote-helper) proved `worktree add --no-checkout` + `read-tree` +
  `check-attr --cached -z --stdin` inspects every tracked path's
  attributes with zero filter/hook execution before materialization;
  `filter` `set`/valued is unsafe, `unspecified`/`unset` safe;
  `ident`/`working-tree-encoding` silently alter committed bytes
  (both reproduced) and are refused per destination, not repo-wide;
  `commit` can rerun clean/process filters, so it needs the same
  backstop as `add`; `required=false` proven unnecessary once
  overrides only touch subkeys that exist.
- The same fake-helper fixture proved `read-tree`/`ls-files`/
  `check-attr --cached` never need blob content and never fetch, while
  `checkout`/`cat-file` do by default and are fully blocked by
  `GIT_NO_LAZY_FETCH=1`. Verified macOS/Git 2.54.0 only. `--no-lazy-fetch`
  requires Git 2.45+, so ADR 0006 sets that as CodeAgent v1's minimum
  supported Git version — older installs are refused, not degraded.
- Key decisions: whole-run refusal for any active `filter` anywhere;
  per-destination refusal for byte-transforming attributes; fixed
  enumeration bounds (128/256/65,536); three-way lazy-fetch runtime
  classification (unsupported-substrate / objects-unavailable /
  ordinary failure), `ErrorCode`s deferred to implementation.
- **Status**: Accepted, **zero implementation**. T-M3 remains open.
  Next: implement `_git_safety.py`, harden `workspace.py`/`patch.py`,
  pass acceptance tests on macOS and Linux — only then does
  `checkpoint_ref.py` integrate with patch application.

---

## Session (2026-09-16): check-attr cannot classify `filter` alone

**Correction to accepted ADR 0006**, found while reviewing the
unintegrated `_git_safety.py` foundation slice.

- `git check-attr` prints the literal string `unset` both for a
  genuinely negated attribute (`a.txt -filter`) and for an explicit
  assignment naming a driver *called* `unset` (`b.txt filter=unset`);
  same collision for `unspecified`. Proven with positive controls in
  one fixture: identical reported strings for all four paths, while
  the `unset`/`unspecified`-named drivers really executed on `git add`
  and the genuine cases did not.
- Impact: the context-free rule would have classified those paths safe.
  `worktree add` relies on attribute inspection as its *sole* control
  (no enumerate-and-neutralize backstop), so this was a real bypass
  permitting host code execution during checkout.
- Correction: `filter` classification now takes the enumerated
  driver-name set and treats `unset`/`unspecified` as safe only when
  that exact string is not a configured driver name, refusing
  conservatively on collision — accepting refusal of some genuinely
  safe paths over guessing. Non-filter attributes are unaffected: Git
  does not resolve their values as driver names.
- Same pass: NUL framing now requires a terminal NUL (truncated output
  whose field count still divides by three was accepted before), empty
  paths/attribute names are refused, version parsing is fully anchored
  (`2.45evil` no longer parses), and argv construction became private
  so `run_git` is the only public execution API.
- **Status**: ADR 0006 amended in place (findings 5/13 qualified,
  finding 16 added, §1/§2 rules corrected, two magic-driver acceptance
  tests added); status unchanged. Still zero integration — T-M3 open.

---

## Session (2026-09-17): ADR 0006 implemented for workspace.py

**Outcome**: `src/codeagent/_git_safety.py` gained bounded/chunked
tracked-path listing and filter-attribute inspection helpers; every
Git invocation in `src/codeagent/workspace.py` now routes through that
shared module. T-M3 closed for workspace creation and source
inspection **only** — `patch.py` remains unprotected.

- `GitWorktree` registers with `--no-checkout`, populates the index via
  `read-tree`, inspects every tracked path's `filter` attribute
  (driver-set-dependent per finding 16), and refuses the whole run
  before materializing anything if any path is unsafe or ambiguous.
  `snapshot_source()`'s `status` carries the enumerate-and-neutralize
  backstop.
- `git worktree add`'s outcome is treated as potentially mutating
  regardless of how it concludes (nonzero, an infra error, or an
  ambiguous report): any failure after the attempt performs exact
  registration removal plus tempdir cleanup, confirmed by observation
  — never `prune`, never a repository-wide sweep on this path. An
  unconfirmed cleanup raises `GitWorktreeCleanupError` chaining the
  original failure as its cause; simultaneous registration+tempdir
  failures are both represented in one message.
- Path-bearing exceptions (`Path.resolve()`, `TemporaryDirectory`
  construction/cleanup) are sanitized via `from None`, verified against
  the complete rendered traceback, not just the top-level message.
- **Verification**: 116 focused `_git_safety` tests, 49 focused
  `workspace` tests, 1170 in the full suite — real hostile
  hooks/clean/smudge/process filters, the `unset`/`unspecified`
  collision, ambient global/system-level filter config, hostile
  `GIT_*` env vars, and a real `git://` daemon partial-clone
  lazy-fetch refusal. macOS only; Linux CI still pending.
- **Remaining gaps**: `patch.py` unintegrated; `__exit__`'s legacy
  `rmtree`/`prune` fallback unchanged (ADR 0004 gap); Linux CI
  unconfirmed until this commit runs there.

## ADR 0006 patch.py-hardening slice: implemented; Linux CI validation pending

Extended `_git_safety.py` and rewrote `patch.py` to close T-M3 for the
patch-application path. Full investigation history is in ADR 0006
Amendments 1-3; this is the concise final state.

- **Replace-ref / literal-pathspec defenses**: `refs/replace/*`
  subverts `rev-parse`/`cat-file -p`/`ls-tree`, neutralized by
  `--no-replace-objects` + `GIT_NO_REPLACE_OBJECTS=1`. Pathspec magic
  survives `--`, neutralized by `--literal-pathspecs` +
  `GIT_LITERAL_PATHSPECS=1`. `write-tree`/`checkpoint_ref.py` confirmed
  unaffected by replace refs.
- **Full six-attribute checking**: `filter`/`text`/`eol`/`ident`/
  `working-tree-encoding`/`crlf`, cached and working-tree views, before
  write and `add` — live transformations reproduced and refused.
- **Bounded subprocess/object handling**: a deadline-controlled binary
  seam (concurrent stdin writer for `cat-file --batch-check`) backs
  object-availability/blob/commit-header retrieval and a genuinely
  bounded `list_tracked_paths`; a real deadlock and a join-before-start
  bug were found and fixed in its cleanup path.
- **Structured GitSafetyError mapping**: every `_git_safety` call in
  `patch.py` is wrapped so nothing escapes `apply()` unhandled, mapped
  to the precise `ErrorCode` per pre-/post-mutation position. Two new
  `ErrorCode`s share one `OperationalError` identity across
  `ToolCompleted`/`RunFinished` (controller-tested).
- **Strict commit acceptance**: expected parent/tree/blob captured
  before `commit` runs once; HEAD observed once after and structurally
  verified — unchanged/mismatched HEAD is never accepted.
- **`.gitattributes` targets refused** (`PATCH_UNSUPPORTED_GIT_SUBSTRATE`):
  a real probe showed a nested `.gitattributes` file's classification
  can be masked by its own staged content, so no trusted independent
  validation exists yet. A repository-wide helper is preserved, unused.
- **Final local totals**: full suite 1300 passed; `_git_safety.py` 184
  and `patch.py` 43 focused tests; 3 real Docker tests passed with
  `CODEAGENT_REQUIRE_DOCKER=1`; clean diff-check and resource checks —
  all on macOS/Git 2.54.0.
- **Remaining gaps**: Linux CI pending; checkpoint-ref integration with
  patch application not wired up; no trusted `.gitattributes`
  independent-layer inspection; `GitWorktree.__exit__`'s legacy
  `rmtree`/`prune` fallback unchanged (ADR 0004 gap).

## Milestone 2 slice 2B-1: checkpoint session state machine (unwired)

**Nothing here is integrated; it makes no production lifecycle
guarantee.** Reviewed alone, before any controller wiring.

- **Defect found while planning:** `_classify_outcome` computed the
  mutation outcome then discarded it, re-raising the original failure
  bare — so a timeout with the ref *confirmed unchanged* looked
  identical to one never observed, though the two demand opposite
  write-ahead handling. Fixed by publishing a `MutationOutcome` on
  every `CheckpointRefError`, defaulting to `UNKNOWN` so unclassified
  raise sites fail closed.
- Outcomes are annotated **in place** on the same exception rather than
  copied onto a replacement, keeping identity, traceback and the
  `__cause__`/`__context__`/`__suppress_context__` state existing tests
  assert on.
- `TRANSACTION_CLEANUP_UNCONFIRMED` **dominates every classification**,
  handled before any observation: an unconfirmed child may still hold
  Git's ref lock, so nothing observed around it is authoritative. The
  intended-value case previously returned success. Proven load-bearing
  — removing the short-circuit fails four dedicated tests.
- `checkpoint_session.py` holds ADR 0004 section 5's record **in memory
  only**, under ADR 0004's field names so Milestone 3 serializes rather
  than redesigns it. Collapses run off `(operation, outcome)`, never a
  second observation. Every transitional state refuses every further
  operation — `removing` included, since the record cannot distinguish
  a confirmed-unchanged failure from an unknown lock outcome; only
  reconciliation's fresh inspection can (Milestone 3). The 2B-2 error
  mapping is recorded in that module's docstring.
- `new_lifecycle_id()` accepts no seed — binding this function only, so
  2B-2 must make the trusted composition root mint exclusively via it.

**Verified**: 1438 full-suite, 125 checkpoint-ref (from 93), 106 new
checkpoint-session, 3 real-Docker; py_compile and `git diff --check`
clean; no leftover containers, worktrees, refs or Git processes.
macOS/Git 2.54.0; Linux CI pending. **Next**: 2B-2 — entry gate,
create/advance ordering, workspace ownership, worktree-before-ref
teardown, event ordering, error identity.

## Session (2026-09-17): pre-2B-2 architecture decision set (documentation only, no code)

**Nothing implemented; 2B-1 remains implemented and unwired.**
Planning-only review, recorded as **ADR 0003 Amendment 2** and
**ADR 0006 Amendment 4** (both Accepted). No production file, test, or
event schema changed; detail lives in the amendments.

- **Lazy checkpoint establishment**: the initial ref is created inside
  the first `apply_patch` transaction, after `ToolRequested`/
  `PolicyDecisionRecorded` and the entry gate, before any mutation.
  `workspace.initial_commit` is the sole starting-checkpoint authority;
  `RunConfig.initial_checkpoint_id` is removed. No `apply_patch` call
  means no ref.
- **Durable evidence artifact**: binary-safe, self-describing, published
  atomically outside both the source repository and the disposable
  worktree, owner-only `0700`/`0600`, 1 MiB hard bound for the current
  single-file engine only (not assumed for future multi-file/add-file
  support), never fabricating totals/hashes for a truncated capture.
- **New finding**: `git diff` against working-tree content executes
  `clean`/`.process` filters, unaffected by `--no-ext-diff`/
  `--no-textconv`; `enumerate_filter_neutralization` suppresses it (ADR
  0006 Amendment 4).
- **Gated teardown/precedence**: evidence capture always attempted
  first, never blocking cleanup; verifier-cleanup-unconfirmed preserves
  the worktree and skips checkpoint-session deletion; otherwise exact
  disposal, then (if confirmed) ref deletion; `RunFinished.error` order
  is cleanup-unconfirmed > evidence-incomplete >
  evidence-durability-unconfirmed > evidence-artifact-collision >
  evidence-capture-failed > original result. No `rmtree`/`prune`.
- **New taxonomy**: `ErrorDomain.EVIDENCE` (4 codes),
  `ErrorDomain.LIFECYCLE`, and a required no-default
  `ContainerCleanupStatus` (`NOT_APPLICABLE`/`CONFIRMED_ABSENT`/
  `UNCONFIRMED`), `NOT_APPLICABLE` legal only before `docker create`.
- **Status**: Accepted designs, not implementations. **Next: implement
  2B-2.**

## Milestone 2 slice 2B-2: checkpoint/evidence integration implemented

Implements ADR 0003 Amendment 2 and ADR 0006 Amendment 4 in full.
Verified locally on macOS with a real Docker daemon; Linux CI and
SHA-256 object-format coverage are still pending.

- **New**: `src/codeagent/evidence.py` — `FilesystemEvidenceSink`,
  strictly observational (one filter enumeration reused for `status`+
  `diff`), framed binary artifact (magic/header-length/JSON
  header/payload), component-aware containment, `0700`/`0600`
  permissions enforced independent of umask, atomic no-replace
  hard-link publish, 1 MiB hard bound with immediate child termination
  on overflow (no fabricated totals), and all four `EvidenceCaptured`
  shapes. 24 tests, including real hostile external-diff/textconv/
  clean/process filter positive controls against the production path.
- **`_git_safety.py`**: new `run_git_bounded_preview` (overflow returns
  a confirmed-terminated, truncated, `complete=False` result instead of
  raising; `run_git_bounded` itself unchanged). 12 new tests.
- **`workspace.py`**: `initial_commit`, `entry_gate()`, `dispose()`,
  `preserve()`, tri-state `__exit__`. Every `rmtree`/`prune` fallback
  removed — unconfirmed disposal now raises loudly. 18 new tests
  including a static AST proof neither remains in the module.
- **`executor.py`**: `ContainerCleanupStatus` threaded through
  `_attempt`/`_execute`, no production default; `create_attempted` set
  immediately before `docker create` so a bare launch failure still
  requires confirmed cleanup rather than `NOT_APPLICABLE`.
- **`controller.py`**: `RunConfig.lifecycle_id` required/validated,
  `initial_checkpoint_id` removed; three new required collaborators
  (`workspace`, `session`, `evidence_sink`); `_dispatch_apply_patch`
  reordered to the full accepted sequence with `_map_checkpoint_error`
  implementing the exact reason+`MutationOutcome` mapping; new
  `_terminate` replaces every direct finish path with the accepted
  gated-teardown/six-tier-precedence choreography.
- **Real end-to-end**: `test_slice_b.py`/`test_slice_c.py` now wire
  real `CheckpointRef`/`CheckpointSession`/`FilesystemEvidenceSink`
  (slice C with real Docker) and confirm the ref, worktree, and
  container are all genuinely gone after teardown, with a genuinely
  published, correctly-framed evidence artifact.
- **Real defect found and fixed during implementation**: the evidence
  containment/symlink check initially rejected `output_root` paths
  under macOS's `/var -> /private/var` (a standard OS symlink, not a
  threat) — narrowed to only check components at or below the deepest
  *already-existing* ancestor, so ambient OS structure above the
  caller-specified portion of the path is trusted while a hostile
  symlink introduced within it is still refused.
- Discovered while wiring the controller: `RunFinished` cannot carry an
  `error` for any `terminal_reason` other than `POLICY_VIOLATION`/
  `UNRECOVERABLE_ERROR` (existing `events.py` contract), and
  `domain.TerminalReason` has no slot for "the original outcome
  succeeded but teardown didn't." Resolved by having `_terminate`
  override the *trigger* to `UNRECOVERABLE_ERROR` whenever precedence
  selects a cleanup/evidence failure — the original result's own error
  (if any) stays exactly where it was already recorded; only
  `RunFinished.error` changes.
- **Full suite: 1845 passed** (up from 1804), including 3 real-Docker
  tests with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check` and
  `py_compile` clean; no leftover containers, worktrees, or temp files
  after a full run.
- **Not done**: Linux CI; SHA-256 object format; a dedicated unit test
  for every scenario in the original task's exhaustive list (several
  are exercised only incidentally through the real end-to-end tests);
  ADR 0004's durable lifecycle store, locks, reconciliation,
  abandonment, and CLI/frontend work remain Milestone 3.

## Slice 2B-2 correction pass: five independently verified defects fixed

All five reproduced first, then fixed, in the unstaged 2B-2 implementation.

1. **`evidence.py` used single `os.write` calls**, not robust against a
   partial write, `EINTR`, or a zero-progress write. Added `_write_all`
   (retries `EINTR`, raises categorically on zero progress) and used it
   for every artifact segment/payload write. Real fault-injection tests
   (a blocking pipe forcing genuine short writes, a forced
   1-byte-short writer patched into every `os.write` call during a real
   capture) prove the published artifact is always exactly correct —
   never silently truncated.
2. **`RunController._terminate`**: an unexpected exception from
   `EvidenceSink.capture()` or from constructing `EvidenceCaptured`
   itself aborted `_terminate` before workspace/ref cleanup or
   `RunFinished` ever ran. `_capture_evidence` now guards both steps
   independently, converting either into a sanitized
   `EVIDENCE_CAPTURE_FAILED` result; `preserve()` is now guarded too.
   Two new tests use a raising `FakeEvidenceSink` and confirm dispose/
   session.delete/`RunFinished` (and, combined with an unconfirmed
   verifier, `preserve()`) still run.
3. **Evidence symlink validation missed an intermediate symlink when
   the final output directory already existed** — the prior
   "already-exists" boundary heuristic stopped validating entirely once
   the whole path was pre-materialized (e.g. an output directory reused
   across runs), a real, previously-undetected gap. Replaced with an
   explicit, fixed allowlist (`_TRUSTED_AMBIENT_SYMLINK_ROOTS = {/tmp,
   /var, /etc}`): every other path component is now checked
   unconditionally, existing or not. A real fixture reproduces the
   original blind spot; the ADR's "every parent path component"
   wording is amended in place to record this narrowing.
4. **Combined incomplete + durability-ambiguous capture** could report
   `EVIDENCE_DURABILITY_UNCONFIRMED` with `complete=False`, violating
   the accepted terminal precedence (`EVIDENCE_INCOMPLETE` must win).
   Fixed in `evidence.py`'s publish-error handling and hardened
   structurally in `events.EvidenceCaptured.__post_init__` (this
   pairing is now impossible to construct, not merely avoided by
   convention). New combined-failure test forces both conditions at
   once.
5. **`GitWorktree.dispose()` let the `git worktree remove` command's
   own reported status (nonzero exit or a raised exception) override a
   confirmed exact absence**, and a tempdir-cleanup failure discarded
   `self._tempdir`, making retry impossible. Fixed: only the final
   observation (registration status + directory presence) decides
   success; `self._tempdir` is preserved on cleanup failure and a
   `FileNotFoundError` from a stale `cleanup()` call is treated as
   confirmed-already-gone. New tests cover both the "reports failure,
   really succeeded" and "reports success, really didn't" cases, plus
   a retry that recovers after a transient tempdir-cleanup failure.

**Verified**: focused suites (evidence/events/workspace/controller) 744
passed; full suite 1860 passed including 3 real-Docker tests with
`CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check`/`py_compile` clean; no
leftover containers, worktrees, or temp files. macOS/Git 2.54.0; Linux
CI still pending, unchanged from before this pass.

## Slice 2B-2 ambient-symlink hardening

The correction pass above fixed defect 3 with a fixed named allowlist
(`/tmp`/`/var`/`/etc` trusted unconditionally by name). Found — before
this was ever exercised on Linux CI — that trusting those three names
on *every* platform was itself too permissive: Linux does not make them
symlinks at all, so the exception had no ambient justification there
and would accept a hostile symlink placed at one of those exact names.

- `src/codeagent/evidence.py`: replaced the name-only allowlist with
  `_is_verified_ambient_symlink` — consulted only when
  `_current_platform_is_darwin()` is true, and even then only when the
  component's real resolved target (`os.path.realpath`) exactly matches
  the recorded expectation in `_MACOS_AMBIENT_SYMLINK_TARGETS` (`/tmp`
  → `/private/tmp`, `/var` → `/private/var`, `/etc` → `/private/etc`,
  confirmed against this session's real macOS host via `os.readlink`).
  A mismatched target, or any of the three names off Darwin, is refused
  like any other hostile symlink component.
- `tests/unit/test_evidence.py`: added a real, unmocked end-to-end test
  under the genuine `/tmp` (skipped off Darwin), plus fully
  platform-independent positive/negative tests (verified-target trusted
  on simulated Darwin; mismatched target refused on simulated Darwin;
  a genuinely matching target refused on simulated non-Darwin) and
  direct unit coverage of `_is_verified_ambient_symlink`. Removed the
  now-superseded single allowlist test. Net: 37 tests (from 33).
- ADR 0003 Amendment 2's correction note extended to record this
  second narrowing precisely, distinguishing it from the first
  (already-exists-boundary) correction.
- CLAUDE.md's living status updated to the current totals (see below);
  the correction-pass entry above is left as the historical record of
  what was true at that point in time, not retroactively edited.

**Verified**: `tests/unit/test_evidence.py` 37 passed; full suite 1864
passed including 3 real-Docker tests with `CODEAGENT_REQUIRE_DOCKER=1`;
`git diff --check`/`py_compile` clean; no leftover containers,
worktrees, or temp files. macOS/Git 2.54.0; Linux CI still pending.

## 2026-09-18: Linux CI confirmation for Slice 2B-2; Slice 3A-1 design accepted

Linux CI run [35306212472](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35306212472)
(commit `fafefbe`) concluded `success`: full suite 1863 passed plus 1
intentional Darwin-only skip, all 3 real-Docker tests passed, no
leftover `codeagent-verify` containers. This closes the "Linux CI
validation is pending" caveat carried by Slice 2B-2's status notes in
`CLAUDE.md`, ADR 0003 Amendment 2, and ADR 0006 Amendment 4 — all three
corrected to cite this run. SHA-256 object-format coverage (ADR 0003's
own required-test list) remains genuinely outstanding, unchanged by
this run. Also corrected a stale claim in `CLAUDE.md`'s Slice 2B-1
bullet that "nothing imports `checkpoint_session`" — true when 2B-1 was
committed, no longer true since `RunController` began driving it as a
required collaborator in 2B-2.

Milestone 3 Slice 3A-1 (trusted lifecycle state-root, repository
identity, repository/generic lock primitives) completed a five-round,
planning-only architecture review and is now **Accepted** as ADR 0004
Amendment 1 — design-complete, not implemented. No production module
exists yet; implementation is the next Milestone 3 step.

## 2026-09-22: Slice 3A-1 implemented and locally validated; still unwired

Implemented ADR 0004 Amendment 1's state-root/identity/lock substrate:
`src/codeagent/_lifecycle_fs.py`, `state_root.py`, `state_locks.py`,
`repo_identity.py`, each with a matching focused test module. A
correction pass fixed a real in-process test regression found while
combining the four focused test files: `test_lifecycle_fs.py`'s
capability test used `importlib.reload()` on a shared module, which
recreated its exception classes as new objects while the other three
modules kept the originals bound via `from X import Y` — making later
`except` matching order-dependent across the whole pytest process.
Fixed by moving that test into an isolated subprocess, and by
replacing every mid-test `monkeypatch.undo()` (several of which
patched `os`/`fcntl` module-wide) with scoped `monkeypatch.context()`
blocks so cleanup is guaranteed even on an unexpected assertion
failure — this specific fix changed no production behavior. Separately,
earlier hardening passes in this same slice *did* change production
behavior before final verification: the top-level `import fcntl` was
removed from `_lifecycle_fs.py`/`state_locks.py` in favor of a
lazily-imported, fail-closed operation-time capability seam (per ADR
0004's own operation-time capability rule), and descriptor
ownership/cleanup, private-file validation, lock-cleanup
classification, state-root probing, and `repo.json` validation were
all corrected. A follow-up security review of the four new files,
limited to high/medium exploitable findings at an >=8/10 reporting
threshold, produced no reportable finding. Two candidates were
identified and independently rejected: unvalidated ancestor
directories in `ensure_bounded_ancestor` (3/10 confidence — the leaf
state-root directory is independently re-validated regardless of
ancestor provenance) and a missing `check_git_preflight()` call in
`repo_identity.py` (2/10 confidence — `run_git()` already applies the
mandatory hardened argv/environment unconditionally). Neither is a
concrete, exploitable gap, and this is not a claim that the slice or
codebase is vulnerability-free or fully audited. No vulnerability
recorded; `check_git_preflight()` before the first repository
operation is carried forward as a Slice 3A-2 integration invariant,
not a 3A-1 defect.

**Verified**: the four focused test files collected and passed
together as 199/199 in both forward and reverse file order (no
cross-test ordering dependency); full suite 2063/2063, including the 3
real-Docker tests with `CODEAGENT_REQUIRE_DOCKER=1`; `py_compile` and
`git diff --check` (including untracked files) clean; no leftover
`codeagent-verify` containers, extra worktrees, `refs/codeagent` refs,
child processes, or temp state roots. macOS/Git 2.54.0 only — Linux CI
validation is still pending. Nothing from this slice is wired into
`RunController`, the CLI, or any lifecycle-lock integration; that is
Slice 3A-2 and later Milestone 3 work, unchanged in scope by this
entry.

## 2026-09-22: Slice 3A-2 implemented and locally validated; still unwired

Implemented ADR 0004 Amendment 1's §16 steps 8-11: `src/codeagent/
lifecycle_store.py`'s `prepare_lifecycle()` composes Git preflight,
3A-1's repository discovery/state-root/repository-lock/`repo.json`
primitives (all reused unchanged), a freshly minted `lifecycle_id`, an
exclusively created `runs/<lifecycle_id>/` directory, the lifecycle
lock, and an atomically published initial `PREPARING` `lifecycle.json`
— strictly before any Docker container, disposable worktree, or
checkpoint ref exists. Backed by two small, genuinely reusable
additions to Slice 3A-1's own modules rather than new architecture:
`_lifecycle_fs.py` gained `create_exclusive_directory_at()` (refuses,
never adopts, a pre-existing entry — distinct from the shared
managed-directory-chain primitive, which tolerates one) and
`publish_private_file_atomically_at()` (same-directory temp file,
write, `fsync`, close, `os.replace`, directory `fsync`, with
exact-temp-file-only cleanup on any failure and cleanup-dominates-and-
chains semantics matching this module's existing discipline);
`state_locks.py` gained `acquire_lifecycle_lock()`, a thin wrapper that
— unlike `acquire_repository_lock` — opens no short-lived parent-fd of
its own, since the run directory is the caller's own long-lived
descriptor.

**Correction pass** (same day, before this slice was considered
final) fixed three real defects a review found:

1. **Publication outcome ambiguity.**
   `publish_private_file_atomically_at()` reported the same
   `LifecycleFsFailure.FSYNC_FAILED` reason for two materially
   different states: a file-`fsync` failure before `os.replace` (no
   final projection installed) and a directory-`fsync` failure after
   a successful `os.replace` (a complete `lifecycle.json` exists but
   its durability is unconfirmed). Added a dedicated
   `INSTALLED_DURABILITY_UNCONFIRMED` reason for the second case, and
   a matching split one layer up
   (`LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED` vs.
   `.PROJECTION_DURABILITY_UNCONFIRMED`), preserving cause-chain
   causality throughout and never deleting or reverting an installed
   file. New load-bearing tests assert both the categorical reason and
   the exact on-disk state for each outcome, at both the primitive
   layer (`test_lifecycle_fs.py`) and the composition layer
   (`test_lifecycle_store.py`).
2. **Incorrect diagnostic source path.** `prepare_lifecycle()` was
   persisting `validated_identity.canonical_common_dir` (the Git
   *common* directory, typically `<repo>/.git`) as `source_repo_path`,
   not the repository's working-tree root. Now persists
   `context.working_tree_root` (`TrustedRepositoryContext`, from
   repository discovery), with an explicit fail-closed guard if it is
   ever unexpectedly `None` (repository discovery already refuses a
   bare repository, so this should be unreachable in practice). Added
   a regression proving `source_repo_path` equals the canonical
   working-tree root and differs from the Git common directory.
3. **Duplicated and incomplete checkpoint-ref schema validation.** The
   module had defined its own `CheckpointRefIntent`/
   `CheckpointRefAttribution`, duplicating
   `checkpoint_session.CheckpointIntent`/`CheckpointTransition` (which
   this ADR's own §5 text says exists specifically so Milestone 3's
   durable store can reuse it rather than redesign it), and its
   validator accepted any nonempty string (including placeholders like
   `"A"`/`"B"`) as a persisted SHA. Removed the duplicate types in
   favor of importing `checkpoint_session`'s directly (the initial
   projection's `checkpoint_ref` is now literally `ABSENT_TRANSITION`);
   the validator now requires every non-null persisted OID to be an
   exact lowercase-hex value of the *repository's actual* object
   format's length (40 for sha1, 64 for sha256), reusing
   `CheckpointTransition`'s own `__post_init__` combination-table
   validation via construction rather than a second copy. Also
   narrowed `worktree`/`failure` validation to only the `absent`/
   `null` shape this slice actually produces: this ADR does not give
   either field a combination table as precise as containers' (§7) or
   checkpoint_ref's (§5), so the validator now refuses every other
   shape categorically instead of describing a partial future
   validator as complete. Added SHA-1 and SHA-256 positive/negative
   tests (wrong length, uppercase, non-hex, cross-format-length
   rejection) and categorical-refusal tests for every non-absent
   worktree intent and every populated failure shape.
4. **Zero-OID persisted-value gap (found by a follow-up review of this
   same correction pass).** Item 3 above did not actually close the
   zero-OID gap: `_is_valid_oid_for_format()` accepted an all-zero
   40/64-character OID as structurally valid hex of the right length,
   and the test meant to prove otherwise
   (`test_checkpoint_ref_zero_oid_refused_as_persisted_value`) was
   misleadingly named — it called the validator and asserted success,
   the opposite of what its name claimed, and this log's own item 3
   text above briefly (and incorrectly) described it as a negative
   test. Fixed at the shared validation boundary this ADR's §5 text
   already designates for reuse: `checkpoint_session._require_oid_shape()`
   (used by `CheckpointTransition.__post_init__`) now refuses an
   all-zero-character OID outright, so `CheckpointTransition`
   construction — and therefore `validate_lifecycle_json_schema()`,
   which already wraps that construction in a `try/except ValueError` —
   inherits the rule with no change needed in `lifecycle_store.py`
   itself, avoiding reintroducing the very duplication item 3 removed.
   The same check was added to `CheckpointSession._validate_against_
   repository()` (the pre-existing, separate validation
   `establish()`/`advance()` run before ever constructing a
   `CheckpointTransition`) so neither path lets a raw `ValueError`
   escape past its own sanitized exception boundary.
   `checkpoint_ref.ObjectFormat.zero_oid` — the Git-argv-only value —
   is untouched. The misleadingly-named test is now two genuinely
   negative tests (sha1 and sha256), plus direct
   `CheckpointTransition`/`CheckpointSession` zero-OID regressions in
   `test_checkpoint_session.py` and an ordinary-nonzero-OID acceptance
   test proving the fix does not overreach.

The initial projection instantiates the complete ADR 0004 §5 schema
(all three identities, diagnostic `run_id`/canonical working-tree root,
`PREPARING`, both containers and the worktree/checkpoint-ref
attributions at their `absent` shape, `failure=null`, a zero-attempt
empty-history reconciliation summary). `validate_lifecycle_json_schema()`
enforces the complete, object-format-aware container and
checkpoint-ref valid-combination tables plus exact-key-set
(unknown-field) refusal at every nesting level, narrowed as described
above for `worktree`/`failure`; it has no production caller in this
slice (nothing yet reads a projection back) and is exercised directly
by unit tests only, the same accepted pattern
`ContainerCleanupStatus.NOT_APPLICABLE` already uses elsewhere. One
flagged, documented assumption remains: each `recent_failures` entry
reuses the ADR's existing 512-byte sanitized-detail bound rather than
a newly invented one, pending Slice 3B's own confirmation once it
actually populates that field.

`LifecycleLease` retains the state-root descriptor, run-directory
descriptor, repository lock, and lifecycle lock for the caller's
required lifetime, releasing them on `close()` in the exact required
order — lifecycle lock, run-directory descriptor, repository lock,
state-root descriptor — attempting every stage regardless of an
earlier stage's outcome, with a cleanup failure dominating and
chaining from the earliest failure (or, via the context-manager
protocol, from an in-flight body exception, matching
`StateRoot`/`LockHandle`'s existing convention). A composition failure
at any point unwinds exactly what was acquired so far through the same
`close()` path.

**Verified**: `py_compile` clean on all seven changed/new files
(including `checkpoint_session.py` for the zero-OID follow-up); the
directly affected test files (`test_checkpoint_session.py`,
`test_checkpoint_ref.py`, `test_lifecycle_store.py`,
`test_lifecycle_fs.py`, `test_state_locks.py`) collected and passed
together, 474 passed; the five focused test files
(`test_lifecycle_fs.py`, `test_repo_identity.py`,
`test_state_locks.py`, `test_state_root.py`, `test_lifecycle_store.py`)
collected and passed together, 311 passed, in both forward and reverse
file order (no cross-test ordering dependency); the full local suite:
2,180 passed; `git diff --check` clean.

**Real-Docker validation** (mandatory before this correction pass was
considered complete, per explicit review instruction): Docker Desktop
was confirmed genuinely ready (`docker info` succeeding, not merely
the application open); the pinned verification image
(`python:3.12-slim@sha256:78387bc3...184ea`) confirmed `linux/arm64`.
With `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
dedicated real-Docker tests, 3 passed, 0 skipped; the complete suite,
2,180 passed, 0 skipped; no leftover `codeagent-verify` containers
afterward.

A real two-process test confirms both the repository lock and the
lifecycle lock are cross-process exclusive; a real SIGKILL test
confirms a fresh process can still acquire both locks afterward
(kernel-released `flock`), creating a new, separate lifecycle_id/run
directory rather than adopting or touching the dead run's own
directory — matching this slice's explicit non-goal of reconciliation.
Failure-injection tests cover: preflight failure causing zero
repository operations; the exact repository-lock → run-directory →
lifecycle-lock → projection ordering; lifecycle-id collision refusal
(never adopted); a hostile pre-existing symlink at the run-directory
name refused the same way; run_id/path/whole-document size-bound
refusal; write/fsync/replace/directory-fsync failures at each
publication stage, each now leaving and asserting the exact distinct
on-disk state its corrected classification claims; temp-file
cleanup-failure chaining; and complete four-stage descriptor/lock
release even when every stage fails. macOS/Git 2.54.0, plus the
Docker Desktop Linux VM for the real-Docker run above — GitHub
Actions Linux CI validation is still pending. Nothing from this slice
is wired into `RunController` or the CLI. T-E1 is not mitigated by
this slice alone: nothing yet calls `prepare_lifecycle()` before a
real run starts. Automatic reconciliation, abandonment, the
maintenance trace, and controller/CLI wiring remain later Milestone 3
work, unchanged in scope by this entry.

## 2026-09-22: GitHub-hosted Linux CI confirmed for Slices 3A-1 and 3A-2

Commit `8aefa5f01f595a4f550823d90ed7f12a0bde9ba6` (the zero-OID
correction pass on top of Slice 3A-2, which also carries the Slice
3A-1 code unchanged) was pushed to `main` and its resulting GitHub
Actions run,
[35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790),
concluded `success` on GitHub-hosted `ubuntu-24.04` x86_64, Python
3.12. This closes the "Linux CI validation is pending" caveat this log
and `CLAUDE.md`/`docs/adr/0004-...md` carried for both Slice 3A-1 and
Slice 3A-2 up to and including the prior entry above — that entry is
left unchanged as an accurate record of what was true at the time it
was written, before this push and run existed.

Exact run evidence: the pinned verification image was pulled and
confirmed `linux/amd64`; the dedicated mandatory real-Docker step
passed 3/3 with no skips; the complete-suite step passed 2,177 with 3
skipped — those 3 skips are exactly the real-Docker tests already
exercised for real in the dedicated step immediately before it, a
deliberate two-step design, not a Docker-unavailability skip; no
leftover `codeagent-verify` containers remained afterward.

This is `ubuntu-24.04` x86_64 GitHub-hosted evidence specifically —
not a general Linux claim and not ARM64 CI coverage. It confirms
Slice 3A-1's and Slice 3A-2's own composition and test suites only.
It does not change scope: `prepare_lifecycle()` remains unwired from
`RunController` and the CLI, T-E1 remains unmitigated for that reason,
and automatic reconciliation, abandonment, the maintenance trace, and
controller/CLI wiring remain later Milestone 3 work.

## 2026-09-22: Milestone 3 Slice 3B-1 — initial-shape automatic reconciliation

Implemented per ADR 0004 Amendment 2 (recorded in this same session,
after a three-round narrowing pass: an initial full-cleanup proposal
was rejected as too large; a first narrower "Slice 3B-1" boundary was
proposed and then corrected on ten separate points — clean-final
cross-field invariants, a trusted API taking the caller's already-
validated identity/context/lock rather than re-deriving them, safe
fd-relative enumeration with an exact recognized-name policy, a
precisely pinned maintenance-trace contract, a corrected SIGKILL model,
attempt-counting semantics, the restored `FAILED` outcome, maintenance-
trace blocking authority, and an exact REFUSED-vs-SUBSTRATE_UNAVAILABLE
enumeration mapping — before implementation began).

`src/codeagent/reconciliation.py` (new) implements `reconcile_
repository()`: enumerates and prevalidates the complete `repos/<repo_
key>/runs/` namespace (sorted, fd-relative, no-follow) before touching
any legitimate entry — a positively observed malformed name, symlink,
wrong type, wrong owner, or unsafe permissions aborts the whole pass as
`REFUSED`; a genuine inspection failure aborts as `SUBSTRATE_
UNAVAILABLE`. For each entry: a pre-lock terminal peek recognizes only
`COMPLETE`/`RECONCILED` with the complete absent-attribution shape as
`SKIPPED_TERMINAL` (zero lock or inspection calls) — a terminal state
with any non-absent field is `REFUSED`, never skipped. A nonterminal
`PREPARING`/`RECONCILING` entry in the same absent shape acquires its
lifecycle lock non-blocking (busy → `SKIPPED_ACTIVE`, which blocks the
pass — an invariant violation while the repository lock is held, never
treated as a benign concurrent run), re-reads the projection fresh, and
performs real inspection: an unfiltered `docker ps -a --no-trunc`
listing, a real `git worktree list --porcelain` listing via the shared
hardened `run_git` seam, and a real `CheckpointRef.observe()` (its
independently rediscovered object format is compared against the
trusted `RepositoryIdentity` and refused on disagreement). Any resource
found present, or any inspection failure, produces `REFUSED`/
`SUBSTRATE_UNAVAILABLE` respectively with **zero projection write**.
Only when everything is confirmed absent does it write `RECONCILING`
(incrementing `reconciliation.attempts_total` exactly once, only on a
fresh non-resuming transition) then `RECONCILED`. Both writes reuse the
existing atomic-publish primitive; either one failing (pre-installation
or installed-but-durability-unconfirmed) is `FAILED`, blocking, with
the exact distinct surviving on-disk state asserted by tests. It never
removes or mutates a container, worktree, or checkpoint ref, and never
writes `LifecycleState.RECONCILIATION_FAILED` (schema-defined,
deliberately unwritten — reserved for a slice with a real external
mutation to give up on, since this slice's own only side effects are
read-only inspection plus its own freely-retryable projection writes).

A one-exclusive-file-per-pass maintenance trace
(`repos/<repo_key>/maintenance/<maintenance_id>.jsonl`, `O_CREAT|
O_EXCL`, never reopened by a later pass) records `ReconciliationStarted`
/`ReconciliationEntryRecorded`/`ReconciliationFinished`, each event
individually written and `fsync`ed, with the containing directory
`fsync`ed once at file-creation time, fixed UTC second-precision
timestamps, and fixed bounds on every field (`CONTAINER_ID_MAX_BYTES`
128, `MAINTENANCE_EVENT_MAX_BYTES` 4096, reusing the existing `run_id`
and 512-byte detail bounds). Failure to create, write, `fsync`,
directory-`fsync`, or close this trace also blocks admission in this
slice — a deliberate, narrow policy recorded in the amendment, since
this slice has no controller, CLI, or event sink through which an
in-memory warning could otherwise ever reach an operator; a later
wiring slice may revisit it once one exists.

`lifecycle_store.py` gained `load_lifecycle_projection()` (fixed-name,
fd-relative, `O_NOFOLLOW` open; the opened descriptor is authoritative
for every check — regular-file type, `st_nlink == 1`, current-uid
ownership, `0600`-or-narrower permissions, the existing size bound,
strict duplicate-key-and-trailing-data-rejecting JSON, object-format-
aware schema validation, and comparison against separately supplied
trusted `lifecycle_id`/`repo_key`/`state_root_id` — never re-derived
from a prior path-based check) and `_publish_projection_state()` (a
thin wrapper reusing the exact existing atomic-publish primitive and
size bounds for the `RECONCILING`/`RECONCILED` transitions). `reconcile_
repository()` is wired into `prepare_lifecycle()` at the exact
documented insertion point — the comment marker that had sat there
since Slice 3A-2 — via a function-scoped import of `reconciliation.py`
to avoid a module import cycle (`reconciliation.py` imports several
names from `lifecycle_store.py`).

**A genuine, expected behavior change surfaced three existing
`test_lifecycle_store.py` tests as failing**, all for the same reason:
each left a run directory durably created (with its lifecycle lock
already acquired) but no `lifecycle.json` ever published into it — a
crash-before-publish shape this slice correctly refuses (`REFUSED`,
never adopted or repaired) rather than silently ignoring. One test
additionally planted a hostile symlink directly under `runs/`, now
caught by the shared-namespace prevalidation before ever reaching the
old `create_exclusive_directory_at` collision check it originally
exercised. All three were updated to assert the new, correct
`RECONCILIATION_BLOCKED` outcome, with a comment explaining why —
this is the named residual risk ADR 0004 Amendment 2 records (recovery
requires future abandonment), not a regression.

**A self-inflicted test bug found and fixed during verification**: the
real-SIGKILL integration test initially called `importlib.reload()` on
`lifecycle_store` in the surviving process after already importing
`reconciliation` (which itself imports several names from `lifecycle_
store`) at the test module's top level. Since `reload()` mutates a
module's `__dict__` in place, this desynchronized enum class identity
between `reconciliation.py`'s own old-bound `ContainerIntent`/
`WorktreeIntent`/`LifecycleState` references and the new instances
`load_lifecycle_projection` constructed after the reload — the same
class of hazard already documented in this project's history for Slice
3A-1's own correction pass. Fixed by removing the unnecessary reload
entirely; no production code was involved.

**Verified**: `py_compile` clean on all four changed/new files. The
eight-file focused set (`test_lifecycle_fs.py`, `test_repo_identity.py`,
`test_state_locks.py`, `test_state_root.py`, `test_lifecycle_store.py`,
`test_checkpoint_session.py`, `test_checkpoint_ref.py`, `test_
reconciliation.py`) collected and passed together, 596 passed, in both
forward and reverse file order. `test_reconciliation.py` alone: 49
passed (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`). Full
local suite: 2,229 passed. With `CODEAGENT_REQUIRE_DOCKER=1` (a skip
treated as a failure): the dedicated real-Docker step
(`tests/integration/test_slice_c.py`), 3 passed, 0 skipped; the
complete suite, 2,229 passed, 0 skipped; no leftover
`codeagent-verify` containers, extra worktrees, `refs/codeagent` refs,
child processes, or temp state roots afterward (`git worktree list`,
`git for-each-ref refs/codeagent`, `docker ps -a`, `ps aux`, and a
`/tmp`/scratch-directory scan all confirmed clean). `git diff --check`
clean.

Real cross-process tests: a process is SIGKILLed immediately after it
durably publishes its initial `PREPARING` projection (still holding
both locks, which the kernel releases on death); a fresh process's own
`prepare_lifecycle()` call performs genuine Docker/Git/ref absence
inspection against the real repository, reconciles the dead entry to
`RECONCILED`, and only then mints its own separate lifecycle_id. A
second real test confirms `SKIPPED_ACTIVE` via a deliberately
inconsistent fixture — a subprocess holding only the lifecycle lock,
never the repository lock, since a correct owner can never present that
combination while the reconciler itself holds the repository lock (the
prior proposal's "a live normal owner" framing was invalid for exactly
this reason and was corrected before implementation).

Not implemented, per Amendment 2's own accepted boundary: any
container/worktree/checkpoint-ref *removal*, `RECONCILIATION_FAILED`,
abandonment, the explicit `codeagent reconcile`/`--abandon` CLI, and
any `RunController`/CLI wiring of `prepare_lifecycle()` itself. T-E1 is
now partially mitigated: a dead prior run is recovered automatically,
but concurrent-run refusal still depends only on the repository lock's
existing `BUSY` behavior, since nothing yet calls `prepare_lifecycle()`
before a real run starts. Linux CI validation is still pending for this
slice.

## 2026-09-22: Slice 3B-1 correction pass — bounded inspection, inner-entry validation, descriptor ownership, REFUSED/SUBSTRATE_UNAVAILABLE precision

A focused review of the just-landed Slice 3B-1 implementation found four
real defects, all independently verified against the code before being
fixed (the architecture, scope, and outcome vocabulary itself were not
revisited):

1. **Unbounded external inspection.** `_docker_ps_all_names()` used
   `subprocess.run(capture_output=True)` (no byte cap), and
   `_worktree_registered_paths()` used the ordinary `run_git()` seam
   (same). Fixed: Docker listing now uses a new bounded-read helper
   (`_drain_bounded`/`_kill_and_confirm`, modeled directly on
   `codeagent._git_safety`'s own private `_drain_stdout`/
   `_terminate_and_confirm` shape, generalized for a non-Git
   subprocess) with a 1 MiB cap and confirmed child termination on
   timeout or overflow; worktree listing now reuses the existing
   public `run_git_bounded()` primitive with the same cap, and adds
   `-z` to `git worktree list --porcelain` so every field is
   NUL-delimited rather than newline-delimited — a registered path
   containing a newline (reproduced with a real worktree at a path
   containing a literal newline component) can no longer be misparsed
   or missed. Neither an overflow nor a timeout is ever silently
   truncated into a partial "answer"; both are refused outright, since
   a truncated worktree listing could otherwise produce a false
   "absent" conclusion.
2. **Untrusted inner entries.** `_check_inner_entries()` recognized
   `lifecycle.json`/`lifecycle.lock`/the temp-publication pattern by
   filename alone, with no fd-relative type/symlink/permission check —
   a hostile symlinked or wrong-type `lifecycle.lock` next to a
   terminal `RECONCILED` entry would previously have been silently
   skipped as `SKIPPED_TERMINAL` without ever being inspected (zero
   lock/inspection calls is exactly the terminal fast path's own
   contract). Fixed: every recognized name is now independently
   fd-relative, no-follow validated (regular file, current-uid owned,
   no group/other permission bits); a positively observed violation is
   `REFUSED`, a genuine inability to inspect is `SUBSTRATE_UNAVAILABLE`.
   The function's return type is now a small typed result
   (`_InnerEntriesCheck`) carrying `has_temp_leftover` all the way
   through to the final `ReconciliationEntryResult` (previously
   computed but discarded) and into the maintenance trace's
   `has_recognized_temp_leftover` field, which the ADR requires and
   this slice's first landing had silently dropped.
3. **Maintenance-trace descriptor ownership.** `_open_maintenance_trace()`
   had three real bugs: (a) a `close_confirmed([fd])` call inside its
   own `except` handler could itself raise, letting a raw
   `LifecycleFsError` escape a function that promises only
   `ReconciliationError`; (b) the trailing `finally: close_confirmed
   ([maintenance_dir_fd])` could likewise raise a raw error and, on the
   success path, leave the just-created trace file's own descriptor
   (`fd`) leaked with nothing left to ever close it; (c) `reconcile_
   repository()`'s own `state_root.open_repo_dir()` and `open_managed_
   directory_chain(repo_dir_fd, [RUNS_DIRNAME])` calls were entirely
   unwrapped, relying on being incidentally caught by a broad
   `except BaseException` and then re-raised **raw**. Fixed: every
   descriptor now has an explicit ownership-transfer point (`fd` is
   transferred to the returned `_MaintenanceTraceWriter` only on
   confirmed final success; `maintenance_dir_fd` is never transferred
   and is always closed by this function, attempted regardless of
   outcome); a cleanup failure always dominates and chains from
   whatever failure was already active, never from itself; and every
   `LifecycleFsError`-raising call in this module is now wrapped at
   its own site before it can propagate further.
4. **Eroded REFUSED-vs-SUBSTRATE_UNAVAILABLE distinction.**
   `lifecycle_store.load_lifecycle_projection()`'s `os.open()` call
   mapped every `OSError` to `SCHEMA_INVALID` (→ `REFUSED`), including
   genuine I/O failures that are not a positively observed bad shape;
   `reconciliation._process_entry()` mapped every directory-open
   `LifecycleFsError` to `REFUSED` regardless of its actual reason; and
   `_check_inner_entries()`'s own listing failure was reported as a
   bare refusal string, always classified `REFUSED` by its caller.
   Fixed: `load_lifecycle_projection()` now inspects `errno` — `ENOENT`
   (missing) and `ELOOP` (`O_NOFOLLOW` refusing a symlink) remain
   `REFUSED`; any other `OSError` (permission genuinely denied at the
   OS level, `EIO`, resource exhaustion) is `SUBSTRATE_UNAVAILABLE`.
   Two new shared helpers, `_classify_pass_level_fs_failure()` and
   `_classify_entry_fs_failure()`, apply the same rule (`LifecycleFsFailure.
   SUBSTRATE_UNAVAILABLE`/`.CLEANUP_UNCONFIRMED` → `SUBSTRATE_UNAVAILABLE`;
   every other reason, e.g. `SYMLINK_REFUSED`/`NOT_A_DIRECTORY`/
   `UNSAFE_PERMISSIONS` → `REFUSED`) at every corrected boundary:
   `_process_entry()`'s directory open, and the pass-level
   `open_repo_dir()`/`runs/`/`maintenance/` opens from finding 3.

**Verified**: `py_compile` clean on all three changed files. One
pre-existing test (`test_recognized_temp_leftover_is_not_refused`)
needed a one-line fix — it wrote its synthetic temp-leftover file with
default (umask-derived) permissions, which the new fd-relative
validation now correctly refuses as unsafe; the fix sets the same
private `0600` mode the real publication primitive always uses.
`test_reconciliation.py` alone: 69 passed (macOS, real Docker daemon,
`CODEAGENT_REQUIRE_DOCKER=1`), up from 49 — 20 net additional collected
tests (confirmed by `pytest --collect-only`), covering every finding
above: bounded-read overflow/timeout with confirmed
termination, Docker launch failure, bounded worktree-listing overflow,
a real newline-containing worktree path parsed correctly by both the
unit-level parser and a full reconciliation pass, symlinked/wrong-type
recognized inner entries, inner-entry inspection failure, explicit
maintenance-trace evidence for a recognized leftover, four
maintenance-trace descriptor-ownership failure-injection scenarios
including simultaneous primary-plus-cleanup failure, repository-
directory open failure in both classifications, and REFUSED-vs-
SUBSTRATE_UNAVAILABLE coverage at every corrected boundary). The
eight-file focused set collected and passed together, 616 passed, in
both forward and reverse file order. Full local suite: 2,249 passed.
With `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the
dedicated real-Docker step (`tests/integration/test_slice_c.py`), 3
passed, 0 skipped; the complete suite, 2,249 passed, 0 skipped; no
leftover `codeagent` containers, extra worktrees, `refs/codeagent`
refs, child processes, or temp state roots afterward. `git diff
--check` clean. No architectural or scope change: the accepted Slice
3B-1 boundary, outcome vocabulary, clean-final invariant, attempt-
counting semantics, lock ordering, and no-removal boundary are all
unchanged.

## 2026-09-23: Milestone 3 Slice 3B-2 — locked, authoritative lifecycle-projection writer

Implemented per ADR 0004 Amendment 3, after a three-round planning-only
review pass (an initial writer-signature proposal was corrected for
stale-write prevention, lock enforcement, and object-format sourcing;
a second pass corrected factory-validation completeness, checkpoint SHA
continuity, publication-recovery precision, transition-edge exactness,
owner-state no-op scope, and the shared-predicate export) before any
code was written.

`LifecycleLease` gained `object_format` (from `RepositoryIdentity.
object_format`, set once in `prepare_lifecycle()`, never inferred from
a caller-supplied SHA) and `open_projection_writer()` — the only
sanctioned way to obtain a `_LifecycleProjectionWriter`. The factory
refuses an incomplete lease (`run_dir_fd`/`object_format`/`state_root`
unset) before any raw `TypeError`/`AttributeError`/invalid-descriptor
error can escape, then calls the writer's own `_require_lease_lock_held()`
— the exact same complete scope check (held, `LIFECYCLE`-kind, exact
`repo_key`/`lifecycle_id` match, never merely `is_held`) every write
method uses — so an already-`close()`d lease (whose `release()` already
cleared `is_held`) is refused identically to a never-fully-prepared one,
not by a separate, potentially-divergent check.

Every write method's shared flow (`_require_current`): verify lock scope
→ load and fully validate the currently installed authoritative
projection fresh (reusing `load_lifecycle_projection` unchanged — a
corrupt or identity-mismatched file is refused exactly as before, never
overwritten) → refuse a stale caller-supplied `expected` before any
publication I/O.
`STALE_EXPECTED_PROJECTION` is a new, deliberately distinct reason from
`ILLEGAL_TRANSITION`: the former means the target may be legal from the
*real* current state but the caller's belief is out of date (caught,
for example, by a caller who skips the mandated `refresh()` step after
a `PROJECTION_DURABILITY_UNCONFIRMED` result and blindly retries); the
latter means the target is illegal from any state. `refresh()` is the
explicit, read-only recovery operation for the former case — one
authoritative read, zero publication/write I/O, never invoked
automatically.

Three new writer methods, each reusing the existing atomic-publish
primitive and `_classify_publication_failure` unchanged:
`advance_lifecycle_state()` (the owner state graph `PREPARING→ACTIVE→
CLEANING→COMPLETE`, the last edge gated by the clean-final absent-shape
predicate; `RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED` refused
unconditionally, including an identical-state request, keeping
reconciler-owned transitions structurally separate from this owner-
facing API); `record_container_transition()` (ADR 0004 §7's table per
role, plus the Amendment 3 clarification that
`(creating,null)→(absent,null)` — a failed-but-confirmed create — is
legal only when the caller has independently confirmed, by real Docker
inspection this writer never performs, that nothing was ever created);
`record_checkpoint_ref_transition()` (ADR 0004 §5's table with full
cross-transition SHA continuity — not merely per-record shape, which
`CheckpointTransition.__post_init__` already guaranteed in isolation —
so e.g. `advancing→present` must land on either the prior or the
proposed SHA, never a third value; `transition` is trusted as the
already-decided output of `checkpoint_session.CheckpointSession`'s own
collapse logic, never re-derived here).

**`checkpoint_session.py` is deliberately untouched.** Direct inspection
of `establish()`/`advance()`/`delete()` before this slice's design was
finalized confirmed each sets its transitional intent and calls the
corresponding `CheckpointRef` method on the very next line, with no
seam between them. The reviewed plan considered adding a narrow
optional transition-publisher callback there (invoked between the
in-memory intent change and the Git call) but selected the smaller
option: `checkpoint_session.py` is already a live production
collaborator (`RunController` requires it for every real checkpoint
commit), and adding an unused seam would grow this slice's blast radius
onto currently-live behavior for infrastructure nothing yet calls.
Consequence, stated plainly rather than implied: `record_checkpoint_ref_
transition()` is fully correct and tested standalone, but is **not yet
a usable durable write-ahead boundary in production** — nothing can
call it at the ADR-required moment for a real checkpoint-ref mutation
today.

`is_projection_fully_absent_shape()` moved from a private
`reconciliation.py`-only helper (`_is_absent_shape`) to a deliberately
public, explicitly-named predicate in `lifecycle_store.py`, imported
explicitly by `reconciliation.py` (four call sites updated) rather than
preserved as an accidental private re-export. `tests/unit/
test_reconciliation.py`'s three direct references were updated to
`ls.is_projection_fully_absent_shape(...)`, referencing the owning
module.

Populated `failure` and non-absent worktree writing remain deferred,
unchanged from Slice 3A-2/3B-1's own narrowing — both are still refused
at the schema-validation layer (`SCHEMA_INVALID`) before this slice's
own transition-edge logic would ever see them; two tests
(`test_cleaning_to_complete_worktree_dirty_shape_is_unloadable`,
`test_cleaning_to_complete_populated_failure_shape_is_unloadable`)
document this precisely: the `CLEANING→COMPLETE` clean-final guard's
worktree/failure checks are correct but currently unreachable in
practice, intercepted earlier by the loader's own existing narrowing.

**Verified**: `py_compile` clean on all four changed/new files.
`test_lifecycle_store.py`/`test_reconciliation.py` together: 193
passed. The eight-file focused set collected and passed together, 651
passed, in both forward and reverse file order. Full local suite:
2,284 passed. Docker Desktop was confirmed down at the start of
verification (`docker info` failing) and was started and waited on
until genuinely ready before any Docker-dependent test ran — the
initial failures this produced in the ordinary (non-`CODEAGENT_
REQUIRE_DOCKER`) run were purely environmental, not a code regression,
confirmed by rerunning identically once Docker was ready. With
`CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the
dedicated real-Docker step (`tests/integration/test_slice_c.py`), 3
passed, 0 skipped; the complete suite, 2,284 passed, 0 skipped; no
leftover `codeagent` containers, extra worktrees, `refs/codeagent`
refs, child processes, or temp state roots afterward — this slice
itself performs no Docker or Git operation of any kind; the mandatory
verification exercised the *existing* real-Docker tests unaffected by
it. `git diff --check` clean.

No architectural or scope change beyond what Amendment 3 records: no
`workspace.py`/`executor.py` change, no controller/CLI wiring, no
resource removal, no abandonment, and (per the boundary decision above)
no `checkpoint_session.py` change. T-E1's mitigation status is
unchanged — nothing from this slice is wired into a real run.

## 2026-09-23: Slice 3B-2 correction pass — resource state gate, writer-call ordering, sanitized checkpoint input, durability wording

A focused review of the just-landed Slice 3B-2 writer found four real
defects and one imprecise-wording issue, all independently verified
against the code before being fixed (the accepted writer architecture,
API shapes, and transition tables were not revisited):

1. **Missing resource state gate.** Neither `record_container_
   transition()` nor `record_checkpoint_ref_transition()` checked the
   authoritative lifecycle state at all — a `COMPLETE` or reconciler-
   owned (`RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED`) projection
   could still be dirtied by a resource transition, including an exact
   no-op, violating the clean-final rule and I11/I15 ("clean-final and
   abandoned entries are never locked, inspected, or mutated again").
   Fixed: a new shared `_require_resource_writable_state()` check
   (`PREPARING`/`ACTIVE`/`CLEANING` only) runs immediately after the
   lock/authoritative-read/stale checks and before any no-op or edge
   logic in both methods. This enforces an already-accepted rule; it is
   not a new lifecycle state graph (ADR 0004 Amendment 3 §5a).
2. **Non-uniform writer-call ordering.** `record_container_transition()`
   validated `role`/`intent`/`id` shape *before* calling
   `_require_current()`, unlike `advance_lifecycle_state()` and
   `record_checkpoint_ref_transition()` — an invalid `role` could mask
   a wrong lock scope or a stale `expected` never actually being
   checked. Fixed: call order is now uniform across all three write
   methods (lock scope → authoritative read/stale comparison → state
   gate → request-shape validation → edge validation → publish).
3. **Unsanitized checkpoint-transition input.** `record_checkpoint_ref_
   transition()` accessed `transition.accepted_sha`/etc. directly with
   no type check — `None` or any non-`CheckpointTransition` value
   raised a raw `AttributeError`, not a sanitized `LifecycleStoreError`.
   Fixed: `isinstance(transition, CheckpointTransition)` is now checked
   immediately after the state gate, before any field access, refusing
   with `ILLEGAL_TRANSITION` and fixed categorical text.
4. **Imprecise durability wording.** `refresh()`'s docstring and ADR
   0004 Amendment 3 §4 both said the new content was "very likely
   durably installed but not confirmed" after
   `PROJECTION_DURABILITY_UNCONFIRMED` — conflating "installed"
   (which `os.replace` genuinely confirmed) with "durable" (which was
   explicitly *not* confirmed). Corrected to: the new complete
   projection is currently installed; its directory-entry durability
   was not confirmed; no power-loss durability claim is made either
   way. Corrected in `lifecycle_store.py`, the one affected test
   comment, and ADR 0004 Amendment 3. `CLAUDE.md` and the prior
   `ENGINEERING_LOG.md` entry were checked and found already precise —
   no change needed there, and no truthful historical entry was
   rewritten.
5. **Leaking corruption-test descriptor.** `test_write_refuses_corrupt_
   projection_without_overwriting` never closed the `os.open()`
   descriptor it corrupted the fixture through, and wrote without
   `O_TRUNC`, leaving a non-deterministic fixture (corrupt prefix plus
   leftover original-content tail bytes, dependent on exact original
   length). Fixed: the descriptor is retained, written with `O_TRUNC`
   for a deterministic all-corrupt fixture, and closed in `finally`;
   the test now also asserts the corrupt bytes remain byte-for-byte
   unchanged after the write is refused.

**Verified**: `py_compile` clean on all four changed files.
`test_lifecycle_store.py` + `test_reconciliation.py`: 208 passed (up
from 193; 15 new tests covering every finding above — refusal from
`COMPLETE` for both resource methods including exact no-ops, refusal
from each of the three reconciler-owned states for both methods
including exact no-ops, confirmation that legal edges still work in
`ACTIVE`/`CLEANING`, the released-lock-plus-invalid-request ordering
proof for both methods, and `None`/wrong-type checkpoint-transition
sanitization). No regression in the full existing test_lifecycle_
store.py suite (123 passed before the 15 additions, unchanged). The
eight-file focused set collected and passed together, 666 passed, in
both forward and reverse file order. Full local suite: 2,299 passed.
Docker was already running at verification start (confirmed via
`docker info`, no start needed this pass). With
`CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the
dedicated real-Docker step, 3 passed, 0 skipped; the complete suite,
2,299 passed, 0 skipped; no leftover `codeagent` containers, extra
worktrees, `refs/codeagent` refs, child processes, or temp state roots
afterward. `git diff --check` clean.

No architectural or scope change: the accepted writer boundary, API
shapes, transition tables, and lock/stale-write/publication semantics
are all unchanged — this pass added a missing invariant check,
corrected call ordering, sanitized one input, fixed wording, and fixed
one test's own descriptor hygiene.

## 2026-09-23: Milestone 3 Slice 3B-3 — durable checkpoint-ref transition-publication seam

Implemented per ADR 0004 Amendment 4. Planning identified this as the
smallest dependency-correct next slice: Slice 3B-2's
`record_checkpoint_ref_transition()` was already fully accepted and
tested, but nothing could ever call it at the ADR-required moment,
since `CheckpointSession.establish()`/`advance()`/`delete()` set their
transitional intent and call the corresponding `CheckpointRef` method
on the very next statement, with no seam between them — confirmed by
direct inspection before any code was written, per the accepted plan.

`checkpoint_session.py` gained a structural `CheckpointTransitionPublisher`
Protocol and an optional, keyword-only `transition_publisher`
constructor parameter, defaulting to `None`. The module's own
import-boundary test (`test_module_imports_nothing_beyond_its_narrow_
dependencies`) needed one deliberate addition — `typing`, for the
`Protocol` itself — updated with an explanatory comment rather than
silently loosened.

Every `self._transition` reassignment across `establish`/`advance`/
`delete` is now immediately followed by a publish attempt, preserving
assignment-first ordering deliberately: the transitional intent is
published *before* the corresponding Git mutation, so a publish failure
there means Git is never called at all (fail-closed by ordering alone,
no extra branching needed); the collapse is published *after* a
confirmed Git outcome, so a publish failure there never undoes the
already-happened mutation. For `establish()`/`advance()`'s confirmed-
`UNCHANGED` recovery path, the recovery collapse is published before
the original `CheckpointRefError` is re-raised; if that publication
itself fails, its exception is raised explicitly `from` the original
error — deliberate chaining, not left to incidental `__context__` —
named **projection-consistency failure dominance** in the ADR, a
distinct rule from this repository's existing cleanup-dominance
convention (an already-decided in-memory collapse that could not be
durably confirmed is a different failure class from an unconfirmed
resource-release). No new `CheckpointSessionFailure` was added to wrap
publisher exceptions — they propagate as their own original type,
unchanged.

`lifecycle_store.py` gained `LifecycleCheckpointRefPublisher`, the one
concrete adapter: it wraps a `_LifecycleProjectionWriter` and tracks
its own `current` expected `LifecycleProjection` across calls, so
`checkpoint_session.py` never needs to know anything about
`LifecycleProjection`. `publish()` replaces `current` only after
`record_checkpoint_ref_transition()` returns normally — every
`LifecycleStoreError` propagates unchanged and leaves `current`
untouched, and `PROJECTION_DURABILITY_UNCONFIRMED` is never silently
treated as success. A separate, explicit `refresh()` method (never
invoked automatically by `publish()`) recovers the currently installed
authoritative projection after that result, matching the recovery
discipline Slice 3B-2 already established for the writer itself.

**Verified**: `py_compile` clean on all four changed files. One test
bug found and fixed during verification, not a production defect: an
`UNCHANGED`-recovery chaining test for `advance()` scripted its fake
publisher to fail on call #1 (the pre-mutation "advancing" publish)
instead of call #2 (the recovery-collapse publish it was meant to
target) — corrected with an explanatory comment; the production
ordering was correct throughout. `test_checkpoint_session.py` alone:
133 passed (up from 111; 22 new tests covering default-unchanged
behavior, exact publish/Git call ordering for all three operations,
delete-from-absent publishing nothing, pre-mutation and post-collapse
publisher failures for all three operations, `UNCHANGED`-recovery
publication and its explicit-chaining failure mode for both
`establish`/`advance`, and non-collapsing outcomes never inventing a
publish call). `test_lifecycle_store.py`'s new adapter section: 8
passed, including the real end-to-end integration test (real repo,
real `prepare_lifecycle()`, real lease/writer/adapter, real
`CheckpointRef`, real `CheckpointSession` — `establish`/`advance`/
`delete` each durably publish, with `lifecycle.json` reloaded and
compared against the real ref's actual state after every call, no
mocking of the writer). The three directly affected files together:
349 passed. The eight-file focused set collected and passed together,
696 passed, in both forward and reverse file order. Full local suite:
2,329 passed. Docker Desktop was found down at verification start
(`docker info` failing — the same environmental state, not a
regression, that produced identical-looking failures in the prior
slice's verification); started and waited on until genuinely ready
before any Docker-dependent test ran. With `CODEAGENT_REQUIRE_DOCKER=1`
(a skip treated as a failure): the dedicated real-Docker step
(`tests/integration/test_slice_c.py`), 3 passed, 0 skipped; the
complete suite, 2,329 passed, 0 skipped; no leftover `codeagent`
containers, extra worktrees, `refs/codeagent` refs, child processes, or
temp state roots afterward. `git diff --check` clean.

No architectural or scope change beyond what Amendment 4 records: no
`RunController`/`executor.py`/`workspace.py` change, no CLI, no
container/worktree writing, no resource removal, no abandonment, no
new legal checkpoint-ref transition edge. `docs/threat-model.md`
unchanged — nothing here is wired into a real run, so no threat-model
claim becomes true or false by this slice. Controller error translation
for a publisher's `LifecycleStoreError` remains explicitly deferred:
`RunController` today only catches `CheckpointRefError`/
`CheckpointSessionError`, and this slice does not claim the seam is
ready for that integration on its own.

## 2026-09-23: Linux CI evidence reconciliation for Slices 3B-1, 3B-2, 3B-3

Documentation-only. Each slice's own "Linux CI validation is still
pending" caveat, truthful at the time it was written, is now closed
prospectively with the corresponding GitHub-hosted Linux CI run,
independently re-verified via the GitHub API before citing it:

- **Slice 3B-1** (initial-shape automatic reconciliation): commit
  `048314e8713777f3401a2e64445e3e8da9507cc1`, run
  [35814528028](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35814528028),
  `ubuntu-24.04` x86_64, Python 3.12, success. Dedicated mandatory
  real-Docker step: 3 passed, 0 skipped. Complete-suite step: 2,246
  passed, 3 skipped — the 3 skips are exactly the real-Docker tests
  already executed for real in the dedicated step immediately before
  it, not a Docker-unavailability skip. No leftover `codeagent-verify`
  containers.
- **Slice 3B-2** (locked, authoritative lifecycle-projection writer):
  commit `0bf66f65b8cbe37ea897af3eb00ca8741522a8da`, run
  [35826244500](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35826244500),
  `ubuntu-24.04` x86_64, Python 3.12, success. Dedicated mandatory
  real-Docker step: 3 passed, 0 skipped. Complete-suite step: 2,296
  passed, 3 skipped — same pattern as above, not a Docker-unavailability
  skip. No leftover `codeagent-verify` containers.
- **Slice 3B-3** (durable checkpoint-ref transition-publication seam):
  commit `bc8cb770bcfeea9a8161c102536e9b69896f24ef`, run
  [35889103564](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35889103564),
  `ubuntu-24.04` x86_64, Python 3.12, success. Pinned verification image
  pulled and confirmed `linux/amd64`. Dedicated mandatory real-Docker
  step: 3 passed, 0 skipped. Complete-suite step: 2,326 passed, 3
  skipped — same pattern, not a Docker-unavailability skip. No leftover
  `codeagent-verify` containers.

This is GitHub-hosted `ubuntu-24.04` x86_64 implementation/test-suite
evidence specifically, not a general Linux or ARM64 claim, and not a
security review. No production behavior, transition tables, failure
semantics, or scope boundary changed by this entry or by the
corresponding `CLAUDE.md`/ADR 0004 wording updates: `prepare_lifecycle()`
remains unwired to `RunController`, and T-E1 remains only partially
mitigated (a dead prior run in the current repository is reconciled
automatically before a new run is admitted, but concurrent-run refusal
still depends only on the repository lock's ordinary `BUSY` behavior,
since nothing yet calls `prepare_lifecycle()` before a real run starts).
The earlier dated entries for these three slices, which truthfully
stated Linux CI validation was pending at the time they were written,
are left unmodified.

## 2026-09-23: Milestone 3 Slice 3B-4 — bounded Docker control-plane execution and validated container-ID capture

Implemented after two planning correction rounds: the first proposed
recommendation (a three-hook, incomplete container-transition
publisher seam) was rejected as an unused abstraction that would need
later replacement — no concrete adapter, no real durable transition,
no validated ID, and unresolved error behavior. A second round
identified the real prerequisite: bounded Docker control-plane
execution and validated container-ID capture, independently useful
today and demonstrably required before any complete lifecycle-aware
container producer slice.

New shared module `src/codeagent/_bounded_subprocess.py`:
`run_bounded_stdout()` owns the complete launch/monitor/read/wait/
kill/confirm lifecycle behind one call, modeled closely on
`codeagent._git_safety`'s own proven `_read_bounded` design (structured
argv, no shell, one monotonic deadline, a single cleanup-and-raise
path). `BoundedProcessFailure` distinguishes `LAUNCH_FAILED`
(categorically distinct, no process to clean up),
`MONITORING_FAILED`, `TIMED_OUT`, `OUTPUT_LIMIT_EXCEEDED`, and
`TERMINATION_UNCONFIRMED` (which dominates and is raised explicitly
`from` a fully-formed `BoundedProcessError` representing the original
failure — a correction made during implementation: the first draft
chained from the internal-only `_BoundedFailure` signal type instead,
which would have leaked that private type into a public exception's
`__cause__`).

`codeagent.reconciliation.py` is refactored to use this shared runner:
its own former private copy of the identical logic
(`_drain_bounded`/`_kill_and_confirm`/`_BoundedReadFailure`) is
removed rather than kept duplicated merely to preserve two tests that
called it directly — those two tests are replaced by direct coverage
of the shared primitive in the new `tests/unit/test_bounded_
subprocess.py`, and every other `test_reconciliation.py` test passes
unmodified, proving the extraction changed no observable behavior.

`codeagent.executor.py`: every non-streaming Docker control-plane
command (`create`/`inspect`/`rm`/`ps -a`) now runs through
`_run_docker()`, rewritten to call the shared runner with its own
fixed, documented, non-arbitrary byte limit and timeout per command —
these four commands previously had **no timeout and no output bound
at all**, a real, independent gap this slice closes (`docker start
--attach`'s own separately-bounded streaming collection and its own
caller-configured timeout are unchanged). `_parse_create_id()`
strictly validates a successful create's stdout as exactly 64
lowercase hex characters plus one trailing LF, rejecting every other
shape categorically and never trusting an ID from a nonzero create
result. Once validated, `start`/`inspect`/`rm` all target that
immutable ID rather than the mutable generated name. Final cleanup
confirmation performs one fresh, unfiltered, `--no-trunc`
`docker ps -a --format '{{.ID}}\t{{.Names}}'` listing, strictly parsed
by `_parse_cleanup_listing()` (exactly one record per nonempty line,
duplicate ID/name rejected as ambiguous), requiring both the exact
name and, when available, the exact ID to be absent.

Introduces no lifecycle publisher, no lifecycle-store adapter, no
deterministic lifecycle-derived container name, no ownership label,
and no controller wiring — legacy UUID-suffixed naming is unchanged,
and no lifecycle projection is written anywhere in this module.

**Verified**: `py_compile` clean on all four changed/new production
and test files. Two real test-fixture bugs found and fixed during
verification, neither a production defect: `_name_only_listing()`'s
helper initially paired every name with the *same* dummy container ID,
so any test listing more than one name tripped `_parse_cleanup_
listing()`'s own duplicate-ID rejection — fixed to generate a distinct
dummy ID per name. Several "confirmed gone" test fixtures reused the
fixed valid container ID as if it were an unrelated listing entry,
which — correctly, per the new dual-identity confirmation rule — made
cleanup register as unconfirmed (the ID appeared to still be present);
fixed by pairing "confirmed gone" fixtures with a genuinely distinct
dummy ID, while the fixtures deliberately testing "still present"
detection via ID match were left as originally written. `test_
executor.py`: 95 passed (up from 63). `test_bounded_subprocess.py`:
16 passed (new). `test_reconciliation.py`: 67 passed (net -1: two
direct private-helper tests removed, one launch-failure test's
monkeypatch target updated). The three directly affected files
together: 178 passed. The established eight-file focused set plus
these three, collected and passed together: 805 passed, in both
forward and reverse file order. Full local suite: 2,383 passed. With a
real Docker daemon and `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as
a failure): the 3 dedicated real-Docker tests, 3 passed, 0 skipped —
exercising the real full create/ID-capture/ID-targeted-start-inspect-
remove/dual-identity-cleanup path end to end, not only mocked unit
coverage; the complete suite, 2,383 passed, 0 skipped; no leftover
`codeagent-verify` containers, extra worktrees, `refs/codeagent` refs,
child processes, or temp state roots afterward. `git diff --check`
clean.

`docs/threat-model.md`'s T-G4 is updated to distinguish the
pre-existing, unchanged 64 KiB verification-command stream bound
(Milestone 1) from this slice's newly-bounded Docker control-plane
calls (previously entirely unbounded and untimed-out) — no claim of
lifecycle attribution, crash recovery, or T-F1 mitigation is made,
since none of that exists yet. Linux CI validation is still pending
for this slice.

## 2026-09-23: Slice 3B-4 correction pass — narrow, pre-review fixes

A pre-commit correction pass on Slice 3B-4, prompted by a joint review
of the unstaged diff, found and fixed eight real gaps before this
slice was considered final. None changed the slice's scope (no naming,
labeling, or lifecycle-integration work was introduced).

1. **Unbounded per-syscall read request.** `_drain()` called
   `os.read(fd, _BOUNDED_READ_CHUNK)` unconditionally — a flat 64 KiB
   request regardless of how close to the caller's limit the buffer
   already was. A 65-byte-limited command (`docker create`) could
   therefore read up to ~64 KiB into memory in one syscall before
   overflow was even detected, contradicting the documented "never
   requests more than the remaining allowance" bound. Fixed:
   `to_read = min(_BOUNDED_READ_CHUNK, limit + 1 - len(buf))`, computed
   fresh every iteration.
2. **Kill-confirmation deadline drift.** `_terminate_and_confirm`
   computed `confirm_deadline = max(deadline, time.monotonic()) +
   _KILL_CONFIRM_GRACE_SECONDS` — since `deadline` is the *original*
   command deadline, an early failure (e.g. an overflow detected 1
   second into a 30-second budget) let `max(deadline, now)` evaluate to
   the still-distant original deadline, granting a kill-confirmation
   wait of up to ~29 remaining seconds plus the 2-second grace period,
   nowhere close to "a fixed grace window." Fixed: `_terminate_and_
   confirm` now takes no `deadline` parameter at all and computes
   `confirm_deadline = time.monotonic() + _KILL_CONFIRM_GRACE_SECONDS`
   fresh, unconditionally.
3. **Cancellation exceptions swallowed.** The outer `except BaseException
   as exc:` handler in `run_bounded_stdout` converted *everything* not
   already handled — including `KeyboardInterrupt`/`SystemExit`/
   `GeneratorExit` — into `BoundedProcessError(MONITORING_FAILED)`,
   silently discarding a real interrupt's identity and type. Fixed:
   ordinary `Exception`s are still mapped to `MONITORING_FAILED`, but a
   genuine `BaseException` that is not an `Exception` now still
   triggers cleanup, then either re-raises the *exact original
   instance* unchanged (cleanup succeeded) or raises
   `BoundedProcessError(TERMINATION_UNCONFIRMED)` (reap not confirmed)
   or `BoundedProcessError(CLEANUP_UNCONFIRMED)` (reap confirmed but a
   descriptor close was not), explicitly `from` that original exception
   in either case — never wrapped into an ordinary categorical outcome.
   `_drain`'s own inner exception handler
   was widened from `except _BoundedFailure` to `except BaseException`
   for the same reason: a cancellation-style exception reaching the
   read loop must still trigger `_drain`'s own descriptor cleanup
   before propagating, not skip it entirely.
4. **Incomplete argument validation.** The prior validation
   (`if not argv or not all(argv): raise ValueError(...)`) had a real
   correctness bug beyond mere incompleteness: a bare `str` argv (e.g.
   `"ls -la"`) iterates as individual *characters*, each truthy, so
   `not all(argv)` was `False` and the string silently passed
   validation, then `list(argv)` exploded it into single-character
   "arguments." Neither `stdout_limit` nor `timeout_seconds` rejected a
   `bool` (Python's `bool` is an `int` subclass, so `True`/`False`
   silently passed as `1`/`0`), and `timeout_seconds` had no validation
   at all. Fixed: a new `_validate_call_arguments()` runs before any
   deadline is computed, rejecting a bare `str`/`bytes` argv, a
   non-string or empty element, a NUL-containing element, a `bool` or
   non-positive `stdout_limit`, and a `bool`, non-finite, or
   non-positive `timeout_seconds` — every rejection is fixed,
   sanitized text that never echoes the caller's actual value.
5. **Misclassified read/wait failures.** A genuine `os.read()` `OSError`
   was mapped to `TIMED_OUT`, conflating "the pipe itself errored" with
   "nothing became ready before the deadline." `_confirm_exit` and
   `_terminate_and_confirm` each caught only `subprocess.
   TimeoutExpired`, letting any other `wait()` failure escape raw.
   Fixed: a read `OSError` is now `MONITORING_FAILED`; both `wait()`
   call sites now catch `Exception` broadly, with a non-timeout failure
   still becoming a categorical outcome (`MONITORING_FAILED` during
   ordinary completion confirmation, `TERMINATION_UNCONFIRMED` during
   termination confirmation) rather than escaping raw.
6. **Silently swallowed descriptor-close failures.** `selector.close()`
   and `process.stdout.close()` (in both `_drain` and `_terminate_and_
   confirm`) were wrapped in bare `except Exception: pass`, meaning a
   close failure was indistinguishable from success even on an
   otherwise fully successful drain. Fixed: a new
   `BoundedProcessFailure.CLEANUP_UNCONFIRMED` reason surfaces any such
   failure, attempted on every path (success included), dominating a
   clean result and chaining from whatever read or termination failure
   preceded it (never the raw close exception, and never discarded).
   `run_bounded_stdout`'s outer handler was also corrected to surface
   `_drain`'s own internal chain (a `CLEANUP_UNCONFIRMED` raised `from`
   a deeper read failure) as a proper nested public
   `BoundedProcessError` chain, not just the outermost reason.
7. **Loose container-name acceptance.** `executor._parse_cleanup_
   listing()` accepted any nonempty string as a valid container name,
   never validating it against Docker's own container-name grammar.
   Fixed: a new `_CONTAINER_NAME_RE` (`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
   is now required for every row's name field — whitespace, a carriage
   return, NUL, Unicode, or leading/embedded punctuation all now make
   the entire listing untrusted, not merely that one row.
8. **Docstring overclaim.** `run_bounded_stdout`'s docstring did not
   explicitly disclaim that its deadline could interrupt the
   synchronous `Popen()` call itself. Corrected to state plainly that
   the deadline governs monitoring/read/wait only after a child process
   object is returned by `Popen`.

Two test-infrastructure bugs were found and fixed during this same
pass's own verification, neither a production defect: patching
`os.read` process-wide in several new tests also intercepted
`subprocess.Popen`'s own internal errpipe read (used on some code
paths to detect a child's `exec()` failure) whenever the fake
`Popen` factory's argument shape triggered CPython's traditional
fork-based launch path rather than `posix_spawn` — corrected by
filtering the injected/spied behavior to the target subprocess's own
stdout fd, captured after real `Popen()` returns. One test's own
assertion assumed a `wait()` failure would classify as
`MONITORING_FAILED` without accounting for the same permanently-broken
`wait()` also dominating to `TERMINATION_UNCONFIRMED` when it recurred
during termination cleanup — corrected to make the fake `wait()` fail
only on its first call.

**Verified**: `py_compile` clean on all six changed/new production and
test files. `test_executor.py`: 113 passed (up from 95; 18 new
container-name-grammar tests). `test_bounded_subprocess.py`: 47 passed
(up from 16; 31 new tests covering all eight findings).
`test_reconciliation.py`: unchanged at 67 passed. The three directly
affected files together: 227 passed. The established eight-file
focused set plus these three, collected and passed together: 854
passed, in both forward and reverse file order. Full local suite:
2,432 passed. With a real Docker daemon and
`CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
dedicated real-Docker tests, 3 passed, 0 skipped — confirming the
tightened container-name grammar accepts real Docker's own generated
names and the corrected read-bound/deadline logic doesn't change real
behavior; the complete suite, 2,432 passed, 0 skipped; no leftover
`codeagent-verify` containers, extra worktrees, `refs/codeagent` refs,
child processes, or temp state roots afterward. `git diff --check`
clean.

`docs/threat-model.md`'s T-G4 wording was reviewed and found not
materially altered by this correction pass (it describes the aggregate
byte bound, which was always eventually enforced; only the transient
per-syscall over-read risk before detection is what this pass closes),
so it is left as committed. No other documentation claim from the
original slice needed correction.

## 2026-09-23: Slice 3B-4 second correction pass — narrower, review-driven

A second, narrower joint review of the still-unstaged Slice 3B-4 diff
(after the first eight-finding correction pass above) found three
further real issues, none changing scope or behavior beyond the fix
itself.

1. **Incomplete public failure chain under repeated cleanup failure.**
   `run_bounded_stdout`'s outer handler already converted a chained
   internal `_drain()` failure (`CLEANUP_UNCONFIRMED` `from` a deeper
   `_BoundedFailure` such as `OUTPUT_LIMIT_EXCEEDED`) into a public
   `inner_cause`, but never attached it to `original` before possibly
   using `original` as the `from` target of a *further* raise if
   `_terminate_and_confirm` then also failed — in exactly that
   double-failure case, the deepest read failure silently vanished from
   the public chain (the final exception's `__cause__` pointed to
   `original`, but `original.__cause__` was still unset). Fixed:
   `original.__cause__` is now set to `inner_cause` immediately, before
   `_terminate_and_confirm` is even attempted, so both the success path
   (`raise original from inner_cause`) and the repeated-failure path
   (`raise ... from original`, where `original` now already carries its
   own cause) preserve the complete chain.
2. **Cancellation-cleanup-failure docstring overclaim.** Both the
   module-level docstring and `run_bounded_stdout`'s own docstring
   stated that a cancellation-style `BaseException`'s cleanup failure
   always becomes `TERMINATION_UNCONFIRMED` — true only when the
   process itself cannot be confirmed reaped; when reap succeeds but a
   descriptor close does not, the actual (already-correct) code
   produces `CLEANUP_UNCONFIRMED` instead, which the docstrings never
   mentioned. Corrected both to name both outcomes explicitly.
3. **`executor._run_docker()` documentation drift.** Its docstring
   still claimed one deadline covers "launch through confirmed
   termination," contradicting the shared runner's own (already
   correct, from the first pass) disclosure that the deadline cannot
   bound the synchronous `Popen()` launch itself. Corrected to state
   the deadline governs monitoring/read/wait only after the `docker`
   process is launched. Its failure-list wording (and
   `_DockerControlPlaneFailure`'s own docstring) was also loosened from
   an incomplete enumeration (missing `MONITORING_FAILED` and
   `CLEANUP_UNCONFIRMED`, both real since the first correction pass) to
   durable categorical phrasing that doesn't need updating every time a
   new reason is added.

`CLAUDE.md` and this log's own first-correction-pass entry both
independently reproduced the same "only `TERMINATION_UNCONFIRMED`"
cancellation overclaim finding 2 corrects; both are updated alongside
the code. `docs/threat-model.md` was inspected and found to contain no
claim these three fixes make inaccurate — left unchanged.

**Verified**: `py_compile` clean on both changed production files and
the one changed test file. Two new regression tests in `test_bounded_
subprocess.py` force all three chain levels (a real `OUTPUT_LIMIT_
EXCEEDED` read failure, a descriptor-close failure inside `_drain()`,
and a second failure during `_terminate_and_confirm()`) and assert the
complete public cause chain plus that no internal `_BoundedFailure`
ever appears in it — one variant with a repeated `CLEANUP_UNCONFIRMED`
(`CLEANUP_UNCONFIRMED -> CLEANUP_UNCONFIRMED -> OUTPUT_LIMIT_EXCEEDED`),
one with a dominant `TERMINATION_UNCONFIRMED`
(`TERMINATION_UNCONFIRMED -> CLEANUP_UNCONFIRMED -> OUTPUT_LIMIT_
EXCEEDED`). `test_bounded_subprocess.py`: 49 passed (up from 47). The
three directly affected files together: 229 passed. The established
eight-file focused set plus these three, collected and passed
together: 856 passed, in both forward and reverse file order. Full
local suite: 2,434 passed. With a real Docker daemon and
`CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
dedicated real-Docker tests, 3 passed, 0 skipped; the complete suite,
2,434 passed, 0 skipped; no leftover `codeagent-verify` containers,
extra worktrees, `refs/codeagent` refs, child processes, or temp state
roots afterward. `git diff --check` clean.
