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

## 2026-09-23: Erratum — false attribution of every prior complete-suite's 3 Linux CI skips to the real-Docker tests, plus Slice 3B-4's own Linux CI evidence

This is a plain factual error, not time-relative history: `.github/
workflows/ci.yml` has been committed exactly once (`a845cb3`) and has
never been modified since, and its "Run complete test suite" step has
carried `CODEAGENT_REQUIRE_DOCKER: "1"` from that single commit
onward. That means Docker was required and available for every
complete-suite step on every Linux CI run this project has ever
recorded — so the 3 tests that step's own suite reports skipped could
never have been the 3 real-Docker tests in
`tests/integration/test_slice_c.py`: those already ran again, for
real, inside that same step, and this run's own log confirms `3
passed` in the dedicated step immediately before it, not 3 "already
covered" skips. Every prior dated entry in this log, and every
corresponding passage in `CLAUDE.md` and `docs/adr/0004-owned-
resource-lifecycle-and-reconciliation.md`, that explained a
complete-suite's 3 skips as "the same real-Docker tests already
exercised in the dedicated step" was wrong from the moment it was
written, for every one of these runs:

- commit `8aefa5f`, run
  [35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790)
  (Slice 3A-1/3A-2), complete suite 2,177 passed, 3 skipped;
- commit `048314e8713777f3401a2e64445e3e8da9507cc1`, run
  [35814528028](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35814528028)
  (Slice 3B-1), complete suite 2,246 passed, 3 skipped;
- commit `0bf66f65b8cbe37ea897af3eb00ca8741522a8da`, run
  [35826244500](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35826244500)
  (Slice 3B-2), complete suite 2,296 passed, 3 skipped;
- commit `bc8cb770bcfeea9a8161c102536e9b69896f24ef`, run
  [35889103564](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35889103564)
  (Slice 3B-3), complete suite 2,326 passed, 3 skipped.

Following this repository's own documentation-ladder convention for a
historical record (unlike `CLAUDE.md`, a living current-status
document whose four corresponding passages this same session's edits
correct in place), this log's own prior entries above are **not**
rewritten — this erratum supersedes their specific "same real-Docker
tests" explanation without altering their surrounding prose, exact
totals, run IDs, commit SHAs, platform-scope claims, or any other
evidence, all of which remain accurate.

The correct explanation, which none of the cited runs' own logs prove
directly (`python -m pytest -q` does not report skip names or
reasons): the narrowest claim every one of these runs' logs actually
supports is **three platform/host-specific tests skipped; they were
not the real-Docker tests**. Source inspection of this repository's own
skip conditions (not CI-log evidence) identifies exactly three
sites that unconditionally skip on a Linux, case-sensitive-filesystem
runner, and no others found by a full-repository search for every
`pytest.skip`/`skipif` call:

- `tests/unit/test_evidence.py`: `@pytest.mark.skipif(sys.platform !=
  "darwin", ...)` on the real, unmocked `/tmp` ambient-symlink test;
- `tests/unit/test_lifecycle_fs.py`: `@pytest.mark.skipif(platform.
  system() != "Darwin", ...)` on the macOS case-canonicalization test;
- `tests/unit/test_repo_identity.py`:
  `test_discover_repository_identity_case_alias_containment` calls
  `pytest.skip()` at runtime when a differently-cased alias path does
  not resolve via `os.path.exists()` — true on any case-sensitive
  filesystem, which is the Linux/ext4 default.

Every other conditional skip found in the repository (git-version-
dependent ones, e.g. `--object-format=sha256` or
`--ref-format=reftable` support in `test_checkpoint_ref.py`,
`test_git_safety.py`, `test_lifecycle_store.py`,
`test_repo_identity.py`) depends on the installed `git` version, which
this session did not independently confirm for any of these runner
images — so those are not ruled in or out with certainty, and the
three named above are presented as the most plausible source-based
candidates, not a CI-log-proven identity.

**Slice 3B-4's own Linux CI evidence** (commit
`1a15485575f460b43fa87e7fd159f73c5234fd7e`, run
[35920856512](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35920856512),
`ubuntu-24.04` x86_64, Python 3.12, success — independently
re-verified via the GitHub API and log before citing it): the pinned
verification image was pulled and confirmed `linux/amd64`; the
dedicated mandatory real-Docker step, 3 passed, 0 skipped (confirmed
directly in the log, and confirmed that step's own env carries
`CODEAGENT_REQUIRE_DOCKER: 1`); the complete-suite step (same env
variable confirmed present), 2,431 passed, 3 skipped — per the
corrected explanation above, three platform/host-specific tests, not
the real-Docker tests; no leftover `codeagent-verify` containers
(the check step itself concluded `success` with an empty result). This
is GitHub-hosted `ubuntu-24.04` x86_64 implementation/automated-test
evidence specifically — not a general Linux claim, not ARM64 evidence,
and not a security review. Slice 3B-4 adds no lifecycle publisher, no
lifecycle projection write, no ownership label, no deterministic
lifecycle-derived container name, no controller wiring, and no crash-
reconciliation behavior — none of that was in this slice's scope.

`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s five
occurrences of the same false attribution are corrected in place
alongside this entry, since those passages describe verification
evidence, not the ADR's own accepted design, transition tables, or
scope boundaries: two for run `35794177790`, both within Amendment 1
(its own "Implementation status (Slice 3A-1, ...)" and "Implementation
status (Slice 3A-2, ...)" subsections — Slice 3A-2 has no separate
Amendment of its own), and one each for run `35814528028` (Amendment
2's evidence section), run `35826244500` (Amendment 3's evidence
section), and run `35889103564` (Amendment 4's evidence section).
`docs/threat-model.md` was inspected and contains no occurrence of
this specific false attribution and no stale Slice 3B-4
Linux-CI-pending statement to update — left unchanged.

## 2026-09-23: Milestone 3 Slice 3B-5 — safe reconciliation and removal of ADR-attributable Docker containers

Extends Slice 3B-1's automatic reconciliation (`reconciliation.py`)
with the container half of ADR 0004 §7's persisted-combination table:
real observation, ownership proof, write-ahead transitions, and
removal, per `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`
Amendment 5 (Accepted 2026-09-23). Worktree and checkpoint-ref removal
remain out of scope, unchanged.

Five design points were locked in across five rounds of planning
review before implementation started (see this session's own planning
transcript, not reproduced here): (1) a legacy-named, unlabeled
container is structurally invisible to reconciliation forever, not
merely "unwired" — ownership proof requires the exact deterministic
name *and* all four required labels; (2) the live-owner's
`_CONTAINER_TRANSITION_EDGES` table has no `creating -> removing` edge,
so reconciliation needed its own separate edge table
(`_RECONCILER_CONTAINER_TRANSITION_EDGES`) and its own write path
(`_publish_reconciler_container_transition`), since the live-owner
writer is categorically refused during `RECONCILING`; (3)
reconciliation's own `creating -> absent` edge proves a weaker,
point-in-time claim ("confirmed absent by a fresh observation") than
the live-owner's stronger historical claim ("no container was ever
created") — the ADR text is explicit about this distinction; (4) a
no-op write-ahead skip requires all three of state-already-
`RECONCILING`, `attempts_total`-already-correct, and attribution-
already-equal — omitting any one risks silently skipping a required
state/attempt-count transition; (5) after any `docker rm` attempt
(including a launch failure, timeout, overflow, nonzero exit, or
unconfirmed termination), a fresh independent listing is always
performed regardless of that attempt's own outcome — `docker rm`'s
exit code is never authoritative for removal.

New in `lifecycle_store.py`: `_CONTAINER_ID_HEX_RE` (exact 64-
lowercase-hex grammar), `_RECONCILER_ELIGIBLE_STATES`,
`_RECONCILER_CONTAINER_TRANSITION_EDGES`,
`_publish_reconciler_container_transition` (hard-coded to
`RECONCILING`, never any other state; validates state eligibility,
role, id grammar, edge legality, and `attempts_total` consistency; one
atomic write per call), and `is_projection_reconciliation_eligible_shape`
(worktree/checkpoint-ref/failure absent, containers unconstrained —
strictly looser than the existing `is_projection_fully_absent_shape`).

New in `reconciliation.py`: `_docker_ps_all_id_name_pairs` (strict
`{{.ID}}\t{{.Names}}` listing, replacing the old name-only
`_docker_ps_all_names`; duplicate id or name anywhere untrusts the
whole listing), `_docker_inspect_ownership` (strict three-field
`docker inspect --type container --format
'{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{json .Config.Labels}}'` by immutable
id, `_INSPECT_OWNERSHIP_MAX_BYTES = 16 KiB`), `_classify_container`
(the ADR §7 table per role), `_remove_and_confirm_absent` (remove by
id, always re-observe), and the rewritten `_reconcile_locked_entry`:
checkpoint-ref and worktree inspection now run *before* containers
(reordered from 3B-1, since both roles' container decisions must be
fully computed — with zero mutation on either conflict — before any
container write happens); both roles' write-ahead writes are published
before any `docker rm`; removal proceeds baseline-first, stopping
before verification's removal if baseline does not reach durable
absence this pass; the final `RECONCILING -> RECONCILED` collapse is
unchanged (`_publish_projection_state`). `_process_open_entry_body`'s
early terminal-peek gate is widened to match. `ReconciliationEntryResult`
gains `baseline_id`/`verification_id`, populated from a positively
observed candidate and retained after a successful removal (the
maintenance trace is retrospective, never write-ahead — Amendment 2
§4); `_MaintenanceTraceWriter.entry_recorded` now populates real ids
instead of always `None`.

`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md` section
7's own table is corrected in place: the old "(observed ID recorded in
the maintenance trace first)" parenthetical was wrong given Amendment
2 §4's already-accepted retrospective-trace rule (the projection's own
write-ahead transition is what actually protects the id, not the
trace) — corrected to reference the real write-ahead mechanism and
Amendment 5. `docs/threat-model.md`'s T-E1 entry is updated: dead-run
container recovery is now implemented, not merely planned; worktree/
checkpoint-ref removal remain the open planned-control gap.

The pre-existing 3B-1 test `test_no_removal_call_reachable_from_
reconciliation_module` (a static AST proof that *zero* removal-shaped
calls or literals were reachable anywhere in `reconciliation.py`) is
obsolete by design, not a regression — this slice's whole point is a
sanctioned `docker rm` path. Replaced with two narrower static proofs:
no *filesystem*-removal primitive (`shutil.rmtree`/`os.remove`/
`os.unlink`/`os.rmdir`) is reachable (the real invariant that still
holds, since worktree/checkpoint-ref removal remain out of scope), and
exactly one `["docker", "rm", "--force", ...]`-shaped argv literal
exists in the module.

Verified: `tests/unit/test_reconciliation.py` and
`tests/unit/test_lifecycle_store.py` together, 215 passed (the eight
pre-existing tests broken by the `_docker_ps_all_names` rename/
reshaping were fixed mechanically, not weakened — same assertions,
updated mock shape). New tests added: strict listing/inspect grammar
and duplicate-id rejection, label-match/mismatch ownership proof,
id-under-a-different-name conflict, zero-mutation-when-either-role-
conflicts, a `creating`-role direct-to-absent path, a `present`-role
owned-removal path, a `present`-role already-confirmed-absent path
that write-aheads through `removing` without ever calling `docker rm`,
a true resumed no-op (asserted via a write-count spy on
`publish_private_file_atomically_at`), `ACTIVE`/`CLEANING` state
eligibility, and maintenance-trace id population/retention. Two new
tests use real Docker fixtures created directly with the exact
deterministic name and required labels (never through `DockerVerifier`
— consistent with this project's established real-fixture discipline):
one proves a genuinely owned, labeled container is observed and
removed end to end with the projection collapsing to
`{intent: absent, id: null}` and the container genuinely gone from
`docker inspect` afterward; the other proves an unlabeled container
occupying the same deterministic name is refused and never removed.

Full local suite (macOS, real Docker daemon,
`CODEAGENT_REQUIRE_DOCKER=1`, a skip treated as a failure): all 3
dedicated real-Docker tests in `test_slice_c.py` passed, 0 skipped; the
complete suite, 2,453 passed, 0 skipped (up from the 2,435-test
pre-3B-5 baseline: +18 net — 20 new tests, 1 pre-existing test replaced
by 2 narrower ones, minus the obsolete removed test); no leftover
`codeagent-verify` containers after the run (confirmed via `docker ps
-a --filter name=codeagent-`). The nine-file focused set used since
Slice 3A-1 (`test_lifecycle_fs.py`, `test_repo_identity.py`,
`test_state_locks.py`, `test_state_root.py`, `test_lifecycle_store.py`,
`test_checkpoint_session.py`, `test_checkpoint_ref.py`,
`test_reconciliation.py`, `test_executor.py`), plus
`test_bounded_subprocess.py`, collected and passed together, 875
passed, in both forward and reverse file order. **Not yet run**: Linux
CI (this slice was left unstaged/uncommitted per the author's explicit
instruction for joint review before any commit or push).

Not implemented (later Milestone 3 work, unchanged from prior slices):
`executor.py` changes, lifecycle-aware container creation, a producer-
side publisher seam wired into `DockerVerifier`, deterministic-name
container creation, `RunController`/CLI wiring, worktree or
checkpoint-ref removal, abandonment, and signal handling.

## 2026-09-23: Slice 3B-5 correction pass — narrow, pre-review fixes

A narrow correction pass on the unstaged Slice 3B-5 implementation,
against the accepted "Slice 3B-5 — Precision Correction (Final)" plan.
Preflight confirmed: branch `main`, HEAD `c8b66b31d977074ff17bb1457dab1272d19f1bf8`,
matches `origin/main`, dirty set unchanged from the prior session (the
3B-5 files only). All eight findings independently re-verified against
source before any change; all eight were **confirmed** real gaps, not
disputed or refined away.

1. **Strict Docker-inspect name grammar (confirmed).**
   `_docker_inspect_ownership()` accepted a name with no leading `/` by
   silently treating it as already-bare. Fixed: exactly one leading
   `/` is now required (`raw_name.startswith("//")` also rejected);
   anything else is `SUBSTRATE_UNAVAILABLE`-shaped malformed output,
   never a name to strip and proceed with. `_docker_ps_all_id_name_pairs`
   also switched from `str.splitlines()` (which treats `\r`, lone `\r`,
   and several Unicode separators as row boundaries, silently absorbing
   a CRLF-terminated row as the expected bare-LF shape) to a strict
   `text.split("\n")` with an explicit missing-trailing-newline check —
   a stray `\r` a real CRLF row would leave on its last field is now
   caught by the existing name grammar instead of being silently
   stripped.
2. **Inspection order (confirmed).** The implementation checked the
   worktree before the checkpoint ref, contradicting its own docstring,
   the ADR, and `CLAUDE.md`'s own description of the accepted order.
   Reordered to checkpoint-ref → worktree → Docker, matching the
   accepted plan; no compelling reason existed to instead correct the
   documentation to the accidental order, so the code was fixed.
3. **Post-removal re-observation contract (confirmed).** `_remove_and_
   confirm_absent()` trusted the post-removal *listing* alone to
   conclude `STILL_PRESENT` — a listing can never itself prove
   ownership (I2). Fixed: when the listing still shows the exact owned
   id/name pair live, the candidate is now re-inspected by immutable id
   before concluding anything beyond absence; a listing-level pairing
   disagreement remains `CONFLICT` without a redundant inspect (the
   listing already disproves continuity there); a well-formed re-
   inspect that still proves ownership is `STILL_PRESENT`; identity or
   label disagreement on re-inspect is `CONFLICT` (→ `REFUSED`); a
   failed or malformed re-inspect is `SUBSTRATE_UNAVAILABLE`.
4. **Retrospective container-ID preservation on every applicable exit
   (confirmed).** `_ContainerDecision.removal_id` was only populated
   for `OWNED_REMOVE`/`CONFIRMED_ABSENT`, so every `REFUSED`/
   `SUBSTRATE_UNAVAILABLE` classification branch in `_classify_container`
   silently dropped a real, already-observed candidate id. Fixed: every
   branch that has observed *any* candidate id now carries it on
   `removal_id` (used only for reporting on a refused/unavailable
   decision — the actual write-ahead/removal logic still only consumes
   `removal_id` from `OWNED_REMOVE`/`CONFIRMED_ABSENT` decisions, which
   are the only ones ever reaching the write-ahead loop). The container-
   decision-conflict early exit and both write-ahead-failure exits in
   `_reconcile_locked_entry` now also populate `baseline_id`/
   `verification_id` from whatever had already been observed for
   *both* roles at that point, not just the failing one.
5. **Direct writer/ordering tests (confirmed — zero existed).** Added
   26 new direct, fd-only unit tests for `lifecycle_store.
   _publish_reconciler_container_transition` to `test_lifecycle_store.py`
   (exact legal/illegal edges, state eligibility, role/id grammar,
   fresh-cycle exact-increment and its rejection, resumed-cycle
   unchanged-count and its double-increment rejection, the true no-op's
   all-three condition, `removing→removing` same-id continuity and its
   different-id rejection, full preservation of every unrelated
   projection field, and both publication-failure classifications) —
   none of these existed before this pass; `_publish_reconciler_
   container_transition` had previously been exercised only indirectly
   through the full `reconcile_repository()` pipeline. Added 3 explicit
   ordering tests to `test_reconciliation.py`: both roles' candidates
   are inspected before either role's first write-ahead publish, both
   roles' write-ahead publishes precede baseline's own `docker rm`,
   baseline's `docker rm` precedes verification's; an unresolved
   baseline blocks verification's own removal for that pass while still
   preserving verification's own already-published write-ahead; and a
   one-role conflict causes zero `docker rm` calls for either role
   (extending the pre-existing zero-projection-write assertion).
6. **Crash-resume and real-Docker acceptance criteria (confirmed —
   partially incomplete before this pass).** Added two real-SIGKILL
   crash-resume tests, each spawning a genuinely separate child process
   that runs one real `reconcile_repository()` pass against a real
   Docker container and self-SIGKILLs immediately after its n-th
   reconciler-owned container write durably lands (a monkeypatched
   counting wrapper around `lifecycle_store._publish_reconciler_
   container_transition`, resolved fresh in the child's own re-imported
   module — not inherited from the parent): one crashes after a single
   role's own write-ahead (baseline `removing(id)` durably on disk,
   real container still genuinely present, real `docker rm` never
   issued yet); the other crashes after *both* roles' write-ahead
   writes land (both durably `removing`, both real containers still
   present). In both cases a fresh `reconcile_repository()` pass then
   resumes, removes the real container(s), reaches `RECONCILED`, and
   the attempt count is never re-incremented across the crash — the
   dead process's own attempts_total increment is durable and honored,
   not repeated. Also added two more real-Docker ADR-§7-table-row
   tests independent of the pre-existing `creating`-role coverage: row
   2 (`present`/owned, exercised on the *verification* role) and row 3
   (`present` + a real container whose live id disagrees with the
   persisted id, exercised on the *baseline* role) — both roles are now
   independently exercised with real Docker for at least one non-
   `creating` row each. **Not exercised with real Docker** (rows 4/7/8/
   9 of the table): the trivial `creating`+absent / `absent`+absent
   confirmed-absent rows (already exhaustively covered by dozens of
   mocked tests and implicitly confirmed by every real-Docker test's
   own pre-condition that nothing else is present) and the `absent`+
   name-present ambiguity row (`ID null, absent | Name present ->
   REFUSED`) — this specific ambiguity row is covered only by mocks
   (`test_present_container_causes_refused_no_projection_write`), a
   real deliberate limitation: constructing it with real Docker adds no
   further confidence over the already-real `creating`+present-
   unlabeled coverage, since both paths exercise the identical
   `_classify_container` `ABSENT`/`CREATING` REFUSED branches against a
   real live container: the *code path proving ownership-vs-ambiguity
   classification against real Docker* is already exercised; only the
   specific `attribution.intent is ABSENT` guard clause itself is
   mock-only.
7. **Pinned Docker image and load-bearing cleanup (confirmed).** Every
   real-Docker fixture in `test_reconciliation.py` now creates
   containers from this repository's own pinned, digest-verified
   `executor.DEFAULT_IMAGE` (imported, never duplicated) running
   `python3 -c pass`, replacing the floating, CI-unapproved
   `alpine:latest`. Fixture teardown (`_force_remove_container`) now
   asserts `check=True` — a cleanup failure now fails the test instead
   of being silently swallowed. Added `test_final_cleanup_no_leftover_
   slice_3b5_containers`, querying both `codeagent-baseline-*` and
   `codeagent-verification-*` name families (not only the unrelated
   `codeagent-verify` family Milestone 1 already checks), as the last
   test in the module's real-Docker set.
8. **Documentation reconciled to the corrected behavior only after (1)–(7)
   above were implemented and verified** — this entry, plus in-place
   corrections to `CLAUDE.md`'s Slice 3B-5 bullet and the checkpoint-
   ref/worktree ordering language already in
   `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
   Amendment 5. No scope boundary changed: still no `executor.py`
   change, no producer publisher seam, no controller/CLI wiring, no
   worktree/checkpoint-ref removal, no abandonment, no signals, no
   model or UI work.

Verified (post-correction-pass): `test_reconciler_writer` direct tests,
33 passed on first run, then 33 (unchanged) as part of the full
`test_lifecycle_store.py` file; `test_reconciliation.py` alone, 94
passed with `CODEAGENT_REQUIRE_DOCKER=1` (0 skipped — up from 86 before
this pass: +8 net, all real-Docker or direct-parser tests); the
established nine-file focused set plus `test_bounded_subprocess.py`,
916 passed (up from 875), in both forward and reverse file order; the
full local suite (macOS): 2,494 passed with no `CODEAGENT_REQUIRE_
DOCKER` set, and 2,494 passed / 0 skipped with `CODEAGENT_REQUIRE_
DOCKER=1` (identical total — every real-Docker test ran both times, none
skipped either way); `git diff --check` clean; no leftover
`codeagent-baseline-*`/`codeagent-verification-*` containers, extra
worktrees, `refs/codeagent` refs, or temp state roots confirmed
directly after the run. Nothing staged, committed, or pushed.

## 2026-09-23: Slice 3B-5 second correction pass — narrower, review-driven

A second, narrower correction pass on the still-unstaged Slice 3B-5
implementation. Preflight confirmed: branch `main`, HEAD unchanged at
`c8b66b31d977074ff17bb1457dab1272d19f1bf8`, matches `origin/main`,
dirty set unchanged (same eight files). All seven findings
independently re-verified against current source before any change;
all seven were **confirmed**, none disputed or refined away.

1. **`observed_id` vs `removal_id` (confirmed).** `_ContainerDecision.
   removal_id` was overloaded for both "the id authorized for removal"
   and "the retrospective id actually observed" — for the final
   ambiguity fallthrough in `_classify_container` (persisted id and
   recomputed name disagree), the old code reported `removal_id=
   persisted_id` even when `persisted_id` itself was never positively
   observed live (only a *different* live id at the name, or the
   persisted id live only under a different name, was actually
   confirmed). Fixed: `_ContainerDecision` now carries both fields with
   distinct, documented semantics — `removal_id` set only when a write
   targeting that id is authorized (`OWNED_REMOVE`, or `CONFIRMED_ABSENT`
   with a persisted id that must still transit `removing` on its way to
   `absent`); `observed_id` set only to a safely parsed candidate
   genuinely, positively observed live, for retrospective trace
   purposes only, never for a write. The three-way ambiguity case
   (different live id at the name / persisted id live elsewhere / both)
   now reports whichever occupies the recomputed name when something
   does, falling back to the persisted id only when it is live
   exclusively under a different name — documented as a deliberate
   precedence choice, never a fabricated value.
2. **Both retrospective ids derived immediately (confirmed).**
   `observed_ids` was populated progressively inside the write-ahead
   loop, so a role whose loop iteration was never reached (an earlier
   role's failure returned first) silently reported `None` even though
   its id was already known from classification. Fixed: both ids are
   now derived from both already-completed decisions immediately after
   the conflict-check gate, before the first publication of any kind;
   every later return (the write-ahead loop no longer touches this
   dict at all) retains those exact values. One pre-existing test
   (`test_present_container_already_confirmed_absent_needs_no_actual_
   rm_call`) asserted the old, incorrect behavior for the "persisted
   present, now confirmed absent" case and was corrected to assert
   `entry.baseline_id is None` (nothing was ever positively observed
   for that role) — distinct from `removal_id`, still `persisted_id`,
   which still correctly drives the write-ahead path.
3. **Parser regression tests (confirmed — none existed for this
   boundary).** Added 12 explicit tests for the inspect-name grammar
   and strict listing split fixed in the prior correction pass:
   no-leading-slash, two-leading-slashes, inspect CRLF, an extra
   trailing line, a missing field, an extra field, listing CRLF,
   missing final listing LF, an internal blank listing row (pinned as
   deliberately *skipped*, not rejected — documented rationale: a blank
   line carries no data to be ambiguous about), duplicate name, and
   duplicate id (already covered) plus an extra-tab-field row. Writing
   the CRLF-in-inspect-output test **found and fixed a real, second gap
   beyond the seven findings**: `_docker_inspect_ownership`'s
   single-line check (`text.count("\n") != 1`) did not reject an
   embedded `\r` — a CRLF row's trailing `\r` survived into the labels
   JSON field, and `json.loads` silently tolerates trailing whitespace
   after a complete value, so the malformed row passed undetected. Now
   explicitly rejected (`"\r" in text`) before any field parsing.
4. **Post-removal classification tests (confirmed — largely untested
   directly).** Added 8 direct unit tests for `_remove_and_confirm_
   absent` (still-present-owned → `FAILED`; re-inspect wrong
   identity/wrong labels → `CONFLICT`; re-inspect failure →
   `SUBSTRATE_UNAVAILABLE`; a listing-level pairing conflict → `CONFLICT`
   *without* ever calling inspect, asserted via a spy that raises if
   invoked; three `docker rm` failure shapes — launch failure, nonzero
   exit, timeout — each still followed by a successful fresh
   confirmation) plus one parametrized pipeline test proving all three
   non-absent removal outcomes leave the role's on-disk attribution at
   exactly the `removing(id)` its write-ahead phase already published,
   untouched. Diagnostic-detail strings were confirmed to contain no
   raw id/payload in the still-present and wrong-identity cases.
5. **Load-bearing SIGKILL evidence (confirmed).** Both real-SIGKILL
   crash-resume tests now assert `proc.exitcode == -signal.SIGKILL`
   (POSIX's own negative-signal exit-code convention) as load-bearing
   evidence the child actually crashed via signal, not merely exited;
   a child still alive after the join timeout is now explicitly
   `kill()`ed and joined before the test fails, so a failed run can
   never leave an orphaned process behind. The pre-crash durable-
   projection and attempt-count assertions are unchanged.
6. **Real-Docker acceptance-criterion gap (confirmed — addressed by
   adding coverage, not by narrowing the criterion).** Two more real-
   Docker tests close the two safe, deterministic remaining ADR 0004 §7
   table rows explicitly named in review: a `present`-persisted role
   whose real container was genuinely removed out of band before
   reconciliation ever ran (row 1's "no container has that name or id"
   confirmed-absent case, using a real container's own id, never a
   fabricated one) and an `absent`-persisted role with a real,
   correctly-labeled container still occupying the deterministic name
   (row 8's ambiguity, which the ADR is explicit is `REFUSED`
   regardless of labels). One row remains deliberately unit-only, not
   silently: the trivial "nothing at all live" confirmed-absent case
   (`creating`/`absent` + no container anywhere), which is already the
   default precondition of every other real-Docker test in this module
   plus dozens of mocked tests — a dedicated real-Docker test for
   literal absence-of-anything would assert only that `docker ps`
   returns nothing, which adds no evidence beyond what every existing
   real-Docker test's own setup and the final cleanup test already
   confirm every run. This is presented as the recommendation, not
   baked into the ADR or `CLAUDE.md` as a fait accompli.
7. **Final-cleanup filter accuracy (confirmed).** The regex `--filter
   name=` query was independently verified against a real Docker
   Desktop container to actually anchor (`^`) and alternate
   (`(baseline|verification)`) as intended, not merely substring-match
   (confirmed it does *not* match the unrelated `codeagent-verify-*`
   family). A second, fully parser/filter-independent query was added
   alongside it: one unfiltered `docker ps -a` listing, matched
   client-side in plain Python string logic that depends on nothing
   Docker-version- or platform-specific — both are asserted
   independently, and a failure now lists the actual leftover names
   rather than a bare boolean.

Verified (post-second-correction-pass): `tests/unit/test_reconciliation.py`
alone, 132 collected (up from 94), with `CODEAGENT_REQUIRE_DOCKER=1`; the
established nine-file focused set plus `test_bounded_subprocess.py`, 954
passed (up from 916), in both forward and reverse file order; the full
local suite (macOS): 2,532 passed both with and without
`CODEAGENT_REQUIRE_DOCKER=1` (identical total both times — every
real-Docker test ran both times, none skipped either way, up from
2,494); `git diff --check` clean; no leftover
`codeagent-baseline-*`/`codeagent-verification-*` containers by either
the regex-filtered or the parser-independent client-side query, no
extra worktrees, no `refs/codeagent` refs. Docker Desktop was started,
when needed, only via the plain `open -a Docker` command (no elevation
requested by anything in this pass); no macOS administrator-access
dialog appeared during this pass. Nothing staged, committed, or pushed.

## 2026-09-24: Slice 3B-5 third correction pass — narrow consistency fixes

A third, narrow consistency pass on the still-unstaged Slice 3B-5
implementation, crossing midnight from the second pass (2026-09-23) —
this entry is dated 2026-09-24, the date this specific pass actually
ran; the two prior dated entries are unchanged, since that work
genuinely completed on 2026-09-23. Preflight confirmed: branch `main`,
HEAD unchanged at `c8b66b31d977074ff17bb1457dab1272d19f1bf8`, matches
`origin/main`, dirty set unchanged (same eight files), Docker already
ready (no restart needed, no administrator-access dialog).

1. **Strict listing blank-row behavior (confirmed).**
   `_docker_ps_all_id_name_pairs()` silently skipped every blank row
   anywhere in a nonempty listing (`if not line: continue`) — genuinely
   inconsistent with ADR 0004 Amendment 5's own "each row is exactly
   one id/name record" statement and with this slice's own fail-closed
   discipline elsewhere. Fixed: empty output (`stdout == b""`) remains
   valid and returns two empty maps (the loop never executes, since
   `"".split("\n")[:-1] == []`); once output is nonempty, a blank row —
   leading, internal, or an extra trailing one beyond the single
   required final LF — is now rejected (`_DockerListingError`), never
   silently skipped. One pre-existing test from the second correction
   pass (`test_docker_ps_all_id_name_pairs_skips_internal_blank_row`)
   had explicitly pinned the old, now-reversed behavior as a deliberate
   decision; replaced with four tests: empty output is valid, and
   leading/internal/extra-trailing blank rows are each rejected.
2. **ADR Amendment 5 section 5 corrected in place (confirmed).** The
   accepted text described the post-removal listing as directly
   classifying an exact surviving owned id as `FAILED`, omitting the
   ownership-proof re-inspect step production has actually performed
   since the first correction pass (finding 3 there). Corrected to
   state the real sequence: neither name nor id present -> confirmed
   absent; a listing-level disagreement -> `REFUSED` with no further
   inspection; the exact pair still present -> re-inspected by
   immutable id, requiring both identity and all four labels again;
   confirmed ownership -> `FAILED`; disagreement -> `REFUSED`; a failed
   or malformed re-inspect -> `SUBSTRATE_UNAVAILABLE` — all three
   non-absent outcomes retaining `removing(id)`. No change to the
   transition table, edge set, or scope.
3. **ADR Amendment 5 evidence wording corrected (confirmed).** "exact
   totals and CI evidence" implied a CI run this slice does not yet
   have (it has never been pushed). Corrected to "exact local
   verification totals and evidence... Linux CI is still pending".
4. **`CLAUDE.md` internal contradiction resolved (confirmed).** The
   first correction pass's own paragraph stated the `absent`+name-
   present ambiguity row remained mock-only; the second correction
   pass's paragraph immediately after it described a real-Docker test
   for exactly that row, without updating the first paragraph — an
   apparent live contradiction to a reader going top to bottom. Fixed
   in place (not rewritten as if the limitation never existed): the
   first paragraph now says explicitly "at the time this first
   correction pass concluded" and points forward to the second pass's
   own closure of that specific row, leaving only the trivial "nothing
   live at all" rows as the remaining stated unit-only limitation.
5. **Date accuracy verified.** The system's own clock crossed midnight
   between the second and third passes (2026-09-23 -> 2026-09-24); this
   entry and the corresponding `CLAUDE.md` note are dated 2026-09-24,
   the date this pass genuinely ran. Both prior dated entries
   (2026-09-23) were independently re-confirmed as correct for the work
   they record and left untouched.

Verified: the listing-parser test group (`test_docker_ps_all_id_name_
pairs*`), 11 passed; `tests/unit/test_reconciliation.py` alone, 135
passed with `CODEAGENT_REQUIRE_DOCKER=1`, 0 skipped; the nine-file
focused set plus `test_bounded_subprocess.py`, 957 passed (up from
954), in both forward and reverse file order; the full local suite
(macOS), 2,535 passed both with and without `CODEAGENT_REQUIRE_
DOCKER=1` (identical total both times, up from 2,532); `git diff
--check` clean; no leftover `codeagent-baseline-*`/
`codeagent-verification-*` containers by either the regex-filtered or
the parser-independent client-side query, no extra worktrees, no
`refs/codeagent` refs. Docker was already ready and was not restarted;
no macOS administrator-access dialog appeared. Nothing staged,
committed, or pushed.

## 2026-09-24: Slice 3B-5 — Linux CI evidence reconciliation

Documentation-only pass reconciling the "Linux CI pending" wording left
in place after finalization, per the author's own instruction to treat
that reconciliation as a separate pass. No production code, tests,
workflow, configuration, dependencies, accepted transition tables, or
scope boundaries changed.

Preflight confirmed: branch `main`, HEAD
`923d99a6596771c37defb801237e4642c56a3305`, matches `origin/main`,
working tree clean. Every cited CI fact was independently re-verified
against the GitHub API and a freshly re-fetched job log (byte-identical
to the log fetched during finalization) before being written into
documentation, not merely copied from the prior finalization report.

**Commit** `923d99a6596771c37defb801237e4642c56a3305` ("feat:
reconcile owned lifecycle containers"), pushed as an ordinary
fast-forward (`c8b66b3..923d99a`), no force push.

**CI run** [35954619345](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35954619345),
job `Test (ubuntu-24.04, Python 3.12)`, `ubuntu-24.04` x86_64, Python
3.12, `headSha` confirmed `923d99a6596771c37defb801237e4642c56a3305`,
conclusion `success`. Every step succeeded: mandatory Docker preflight;
`Resolve pinned verification image`; `Pull pinned verification image
and confirm linux/amd64` (confirmed pulled
`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`,
platform line read verbatim: "Pulled image reports platform:
linux/amd64"); `Run real Docker verification tests (must execute, not
skip)` — env confirmed `CODEAGENT_REQUIRE_DOCKER: 1`, result "3 passed
in 2.26s", 0 skipped; `Run complete test suite` — invoked as
`python -m pytest -q` (no `-v`, so no individual test identities are
ever printed), env also confirmed `CODEAGENT_REQUIRE_DOCKER: 1`,
result "2532 passed, 3 skipped in 39.37s". Since the dedicated
real-Docker tests already ran and passed within this same
complete-suite step under the identical env var, these 3 skips are
**not** the real-Docker tests — described here only as three
platform/host-specific skips, never a guessed test identity, since the
log genuinely does not expose them. `Verify no leftover codeagent-verify
containers` — the step's own embedded script filters exactly
`docker ps -a --filter "name=codeagent-verify"`; output: "codeagent-verify
containers currently present (expected: none):" followed by nothing —
confirmed empty, and confirmed (by reading the step's own script in the
log) that this workflow-level check covers only the unrelated
`codeagent-verify` family (Milestone 1's), unchanged by this slice.
Slice 3B-5's own `codeagent-baseline-*`/`codeagent-verification-*`
leftover-container assertion (`test_final_cleanup_no_leftover_
slice_3b5_containers`) is not a separate workflow step — it is one of
the 2,532 tests that passed inside the complete-suite step itself.

**Post-CI state reconfirmed**: local `main` and `origin/main` both at
`923d99a6596771c37defb801237e4642c56a3305`.

This is `ubuntu-24.04` x86_64 implementation/automated-test evidence
specifically — not a general Linux or ARM64 portability claim, and not
a security review. It confirms nothing about `prepare_lifecycle()`
being wired into any real entry point, nor about producer-side
lifecycle publication, controller/CLI wiring, worktree/checkpoint-ref
removal, abandonment, or signal handling, none of which exist yet;
`docs/threat-model.md`'s T-E1 partial-mitigation boundary is unchanged
by this slice and was inspected during this pass — it contains no
stale Slice-3B-5-specific Linux-CI-pending statement to correct, so it
was left untouched.

`CLAUDE.md`'s Slice 3B-5 bullet (both its opening framing and its
closing statement, previously "Linux CI has not yet run"/"Linux CI has
not run") and `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
Amendment 5 evidence section (previously "Linux CI is still pending —
this slice has not been pushed, so no CI run exists for it yet") are
both updated in place with the verified evidence above. No other prose
in either file — design, transition tables, ownership rules, failure
classification, inspection/mutation order, or milestone boundary — was
touched. The three earlier dated `ENGINEERING_LOG.md` entries
(2026-09-23 x2, 2026-09-24's own third-correction-pass entry), which
truthfully stated CI was pending or unrun *at the time each was
written*, are left unmodified, per this project's own established
convention of never rewriting historical entries to match later state.

Verified: `git diff --check` clean on the resulting documentation-only
diff; no production code, test, workflow, configuration, or dependency
file changed; nothing staged. No test run or Docker session was needed
for this pass — the completed, independently re-verified CI run is the
evidence.

## 2026-09-27 — Milestone 3 Slice 3B-6: lifecycle-aware `DockerVerifier` container production

Implemented ADR 0004 Amendment 6: `DockerVerifier` gains an opt-in
`lifecycle_context` that switches it to deterministic
`codeagent-baseline-<lifecycle_id>`/`codeagent-verification-<lifecycle_id>`
naming, the four ADR 0004 section 7 labels, and durable write-ahead
publication of every container-attribution transition through a new
`lifecycle_store.LifecycleContainerPublisher` — before this slice,
`executor.py` still minted UUID-suffixed names and wrote no lifecycle
projection at all, so nothing reconciliation's own Slice 3B-5 removal
path could recover was ever actually produced.

Two new dependency-light leaf modules keep `executor.py` out of the
persistence stack: `container_lifecycle.py` (naming/labeling/publisher
protocol/`ContainerPublicationError` vocabulary) and
`_docker_ownership.py` (the strict `docker ps -a`/`docker inspect`
parsing grammar, generalized out of three previously independent
copies in `executor.py` and `reconciliation.py`). `lifecycle_store.
LifecycleContainerPublisher` is the sole translation boundary from
`LifecycleStoreError` to `ContainerPublicationError`, via an exhaustive
map (subscript access, not `.get(..., default)`) that a dedicated test
checks against the live `LifecycleStoreFailure` enum.

One real correction happened mid-implementation, not after: an initial
working draft (following an earlier, superseded planning pass) had
several post-create observation branches returning the public
`NOT_APPLICABLE` cleanup status, which is only legal before this
invocation's own `docker create` is ever entered. Corrected before any
test was written against the wrong behavior — `NOT_APPLICABLE` is now
reserved strictly for pre-create failures (a `CREATING` publish
failure, a foreign pre-create occupant, or a pre-create observation
failure); everything after `docker create` has been entered resolves to
`CONFIRMED_ABSENT` or `UNCONFIRMED`, including the row where a fresh
post-failure observation confirms absence while the lifecycle
projection itself conservatively stays at `CREATING` (a deliberate
split between the public Docker-side cleanup claim and the durable
lifecycle-projection state, since this invocation can never prove the
live-owner's own stronger "nothing was ever created" precondition after
an uncertain create).

Verified locally (macOS, Docker Desktop): full suite 2,599 passed with
and without `CODEAGENT_REQUIRE_DOCKER=1` (identical total both ways);
the twelve-file focused set (existing eight plus
`test_bounded_subprocess`, `test_container_lifecycle`,
`test_docker_ownership`, `test_executor`) 1,015 passed in both forward
and reverse file order; `tests/integration/test_slice_3b6.py` (new, 6
tests, no mocking of the writer or Docker CLI) proves the real
end-to-end path — real `prepare_lifecycle()` lease, real writer, real
publisher, real Docker — including three real, independently spawned
SIGKILL child-process tests (confirmed via `proc.exitcode ==
-signal.SIGKILL`) at each of the three accepted crash boundaries, each
followed by a fresh `reconcile_repository()` pass proving the correct
residual-state resolution. No leftover containers, worktrees,
`refs/codeagent` refs, processes, or temp state roots afterward;
`git diff --check` clean. `.github/workflows/ci.yml`'s leftover-
container check was also corrected in the same pass (Docker's own
`--filter name=` is a substring match, not an anchored proof) to list
every container name once and apply an anchored `grep -E` covering all
three container-name families; a dedicated unit test pins the exact
pattern. Linux CI has not yet run for this slice — do not claim it has
until a workflow run against this commit completes.

Not implemented (unchanged scope from every prior Milestone 3 slice):
`RunController`/CLI wiring, abandonment, worktree/checkpoint-ref
removal, signal handling, model integration. `docs/threat-model.md`'s
T-E1 entry is unaffected — nothing here changes when or whether
`prepare_lifecycle()` is called before a real run starts.

### Correction pass (same day, before this slice was ever committed)

A review pass on this still-unstaged slice found one real correctness
bug and several hardening gaps, all fixed before commit:

- **Identity-binding structural fix**: `DockerVerifierLifecycleContext`
  previously accepted `lifecycle_id`/`state_root_id` as independent
  constructor fields alongside `publisher`, which could disagree with
  the publisher's own recorded identity — a real, dangerous mismatch a
  crash could leave unattributable to any reconciler. Fixed by making
  `container_lifecycle.ContainerTransitionPublisher` expose
  `lifecycle_id`/`state_root_id` as read-only properties (implemented
  by `LifecycleContainerPublisher` from its own `current` projection),
  and by making `DockerVerifierLifecycleContext`'s own `lifecycle_id`/
  `state_root_id` computed properties derived from `publisher` alone —
  the mismatch is now structurally unrepresentable, not merely refused.
- **Real bug found by the real integration test, not by any mock**: a
  fully successful occupied-name recovery collapses the projection back
  to `ABSENT`, and the code then tried to publish `PRESENT` for its own
  new container directly — there is no `ABSENT->PRESENT` edge (only
  `ABSENT->CREATING`), so the real writer refused it
  (`ILLEGAL_TRANSITION`), leaving a real created-but-never-started
  container behind that the run then reported as `UNCONFIRMED` cleanup
  instead of `PASSED`. Fixed by re-publishing `CREATING` immediately
  after a successful recovery. The mock-based `_FakeContainerPublisher`
  in `tests/unit/test_executor.py` did not catch this originally
  because it never validated transition edges at all; hardened in the
  same pass to enforce the identical edge table locally, so this class
  of bug is now caught without needing real Docker.
- `tests/integration/test_slice_3b6.py::_reconcile_fresh()` leaked an
  open `StateRoot` and repository `LockHandle` on every call (neither
  type has destructor-based cleanup). Fixed: releases the lock before
  closing the state root, both attempted regardless of an earlier
  failure, chaining a cleanup failure from whatever failure was already
  active (`LifecycleLease.close()`'s own convention). A new load-bearing
  regression proves the same repository lock is immediately
  reacquirable afterward.
- Real-Docker fixture cleanup was made load-bearing:
  `_force_remove_container()` now confirms the container is actually
  gone and raises if it cannot, instead of silently ignoring `docker
  rm`'s exit code; every test that builds a container under a
  deterministic name now has a `finally`-block safety net that removes
  it by validated ID if a mid-test assertion fails; the label-inspection
  test now asserts its worker thread genuinely finished before the
  lease closes.
- `test_real_extra_image_provided_labels_never_defeat_ownership`
  previously only asserted a run completed cleanly and speculated the
  pinned image "may" carry extra labels — it never proved one existed.
  Replaced with `test_real_occupied_name_recovery_accepts_extra_image_
  provided_label`: pre-creates a real, correctly owned container
  carrying the four required labels plus one genuine unrelated label,
  proves that label is actually present, then proves occupied-name
  recovery accepts ownership despite it and removes the pre-existing
  container by immutable ID before completing a new run.
- The real end-to-end test now uses a deterministic `("true",)` command
  and mounts the actual repository (not `tmp_path`, which also contains
  the state root), and asserts `PASSED` for the baseline and both
  verification attempts — previously it only asserted cleanup status,
  which does not distinguish a genuinely passing run from one that
  merely failed to start.
- `tests/unit/test_ci_container_leftover_check.py` previously tested a
  hand-copied duplicate of the workflow's `grep -E` pattern, which could
  stay green even if the real workflow drifted. It now reads
  `.github/workflows/ci.yml` directly and extracts the actual configured
  pattern before testing it, and additionally asserts the detection
  step remains anchored, singular, `if: always()`, and never issues a
  `docker rm`/`docker kill`.
- The reviewer's finding that ADR 0004 Amendment 6 contained a
  duplicated phrase in its `REMOVING(id=None)` paragraph was checked
  directly against the file and did not reproduce — no change was made
  for that specific item.

Verified after these fixes: `tests/unit/test_executor.py` 133 passed
(up from 130); `tests/unit/test_lifecycle_store.py` +
`tests/unit/test_executor.py` 323 passed together;
`tests/integration/test_slice_3b6.py` 7 passed (up from 6) against a
real Docker daemon, including the corrected extra-label-recovery test
and the new lock-release regression; no leftover containers, worktrees,
`refs/codeagent` refs, or temp state roots afterward. Linux CI has
still not run for this corrected version — do not claim it has until
it is committed, pushed, and a workflow run against that commit
completes.

### Second correction pass (same day, still before this slice was ever committed)

The totals in the section immediately above (`test_executor.py` 133,
`test_lifecycle_store.py`+`test_executor.py` 323 together,
`test_slice_3b6.py` 7) were this pass's own local totals at the time
they were written and are preserved above as historical evidence of
what that pass actually verified. They are **superseded** by the totals
below, which reflect one further, narrower correction plus its own
fresh full verification.

A follow-up review found that `tests/integration/test_slice_3b6.py`'s
own cleanup helpers were fail-open, not fail-closed:
`_container_exists()` treated *any* nonzero `docker inspect` exit —
including a genuinely unavailable Docker daemon, not just a genuinely
absent container — as "absent." That let `_force_remove_container()`
report cleanup confirmed, and `_cleanup_deterministic_name_if_present()`
silently skip cleanup, without positive evidence in either case. Fixed
by deriving presence/absence exclusively from a fresh, strict,
unfiltered listing via the shared `_docker_ownership.
docker_ps_all_id_name_pairs()` (the same shared listing this slice's
own production code already uses) — a listing failure
(`DockerListingError`) now propagates and fails the test, never
silently meaning "absent." `_force_remove_container()` additionally
refuses a malformed id before ever calling `docker rm` (via a new
`_require_valid_container_id()`), and confirms absence via the same
fresh listing rather than `docker inspect`'s own ambiguous nonzero
exit; `_cleanup_deterministic_name_if_present()` now obtains its
candidate id from that same strict name→id listing instead of `docker
inspect <name>`. This is a test-infrastructure correction only — no
production code changed in this pass. Five new mock-based regression
tests were added to `tests/integration/test_slice_3b6.py` pinning this
behavior: listing failure is never treated as absence; a malformed id
never reaches `docker rm`; a still-present id fails cleanup; a
listing confirming absence succeeds; and removal is always by the
listed id, never by name.

**Final, authoritative totals after both correction passes** (macOS,
real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`): the directly
affected set (`test_executor.py` + `test_lifecycle_store.py` +
`test_slice_3b6.py`), 335 passed; the thirteen-file focused set (the
twelve named in the first correction-pass entry above plus
`test_ci_container_leftover_check.py`), 1,025 passed, in both forward
and reverse file order; the dedicated real-Docker tests
(`test_slice_c.py` + `test_slice_3b6.py`), 15 passed, 0 skipped;
`test_slice_3b6.py` alone, 12 tests (7 real-Docker end-to-end/SIGKILL
tests plus the 5 new mock-based cleanup-helper regressions); the
complete suite, 2,615 passed, 0 skipped, identical with and without
`CODEAGENT_REQUIRE_DOCKER=1`; no leftover containers of any CodeAgent
family (confirmed via a strict listing covering all three name
families: `codeagent-verify-*`, `codeagent-baseline-*`,
`codeagent-verification-*`), no extra worktrees, no `refs/codeagent`,
no related processes, no stray temp state roots; `git diff --check`
clean. These are the numbers `CLAUDE.md`'s main Slice 3B-6 section and
ADR 0004 Amendment 6's evidence section now report — both were updated
in this same pass to replace the stale pre-correction totals (1,015 /
2,599 / 6 tests) with these final ones. Linux CI has still not run for
this twice-corrected version — do not claim it has until it is
committed, pushed, and a workflow run against that commit completes.

### Third correction pass (same day, still before this slice was ever committed)

The totals in the section immediately above (335 / 1,025 / 15 /
`test_slice_3b6.py` 12 tests / complete suite 2,615) were that pass's
own local totals at the time and are preserved above as historical
evidence. They are **superseded** by the totals below.

A follow-up review found that `tests/integration/test_slice_3b6.py`'s
Docker-availability gating was applied module-wide
(`pytestmark = pytest.mark.skipif(not _docker_available(), ...)`), so
the five mock-only cleanup-helper regressions added by the second
correction pass — which make no Docker call at all and exist
specifically to test failure injection — were also silently skipped
whenever Docker happened to be unavailable locally, defeating their own
purpose. Fixed by replacing the module-wide marker with a per-test
`requires_docker = pytest.mark.skipif(...)` applied only to the seven
genuine real-Docker tests: `test_reconcile_fresh_releases_the_
repository_lock`, `test_real_lifecycle_aware_baseline_and_two_
verification_attempts_reuse_role`, `test_real_docker_create_sets_
exact_deterministic_name_and_four_labels`,
`test_real_occupied_name_recovery_accepts_extra_image_provided_label`,
and the three `test_real_sigkill_after_*` tests. The lock/reconciliation
test was independently re-derived rather than assumed: a fresh
`reconcile_repository()` pass over any reconciliation-eligible entry
always issues a real `docker ps -a` for both container roles, so it
genuinely requires Docker and is marked accordingly. The prior
mandatory-Docker CI hard-failure behavior (`CODEAGENT_REQUIRE_DOCKER=1`
set but no daemon available fails the whole module at collection time)
is unchanged. A new structural test,
`test_exactly_the_real_docker_tests_carry_the_requires_docker_marker`,
introspects the module directly and asserts exactly these seven
functions carry the marker and no others — proven both by running the
five mock tests with Docker's own binary made unreachable via `PATH`
(6 passed: the five regressions plus the structural test itself; the
seven real-Docker tests skipped cleanly, 0 failures) and by running the
complete file with a real daemon present (all 13 pass, 0 skipped).

Also fixed: a stale sentence in `CLAUDE.md`'s Slice 3B-6 section, in the
paragraph describing `reconciliation.py`'s import-only migration, which
still said "forward and reverse file order together with the other
seven focused files, 1015 passed both ways" — inconsistent with the
final thirteen-file focused-set description elsewhere in the same
bullet. Replaced with wording that points at the final thirteen-file
set instead of repeating a stale, narrower count.

**Final, authoritative totals after all three correction passes**
(macOS, real Docker daemon): the directly affected set
(`test_executor.py` + `test_lifecycle_store.py` +
`test_slice_3b6.py`), 336 passed; the thirteen-file focused set, 1,025
passed, in both forward and reverse file order (unchanged by this
pass — `test_slice_3b6.py` is not part of that set); the dedicated
real-Docker tests (`test_slice_c.py` + `test_slice_3b6.py`), 16 passed,
0 skipped; `test_slice_3b6.py` alone, 13 tests (7 real-Docker tests,
each individually marked; 5 mock-based cleanup-helper regressions; 1
structural marker-placement test) — with Docker unavailable, exactly
the 6 non-Docker tests pass and the 7 real-Docker tests skip, 0
failures; the complete suite, 2,616 passed, 0 skipped, identical with
and without `CODEAGENT_REQUIRE_DOCKER=1` whenever Docker is actually
available; no leftover containers of any CodeAgent family (a strict
listing covering all three name families), no extra worktrees, no
`refs/codeagent`, no related processes, no stray temp state roots;
`git diff --check` clean. `CLAUDE.md`'s main Slice 3B-6 section and ADR
0004 Amendment 6's evidence section were both updated in this same pass
to these final numbers, superseding the 335/1,025/15/2,615/12-tests
figures the immediately preceding entry recorded. Linux CI has still
not run for this (thrice-corrected) version — do not claim it has until
it is committed, pushed, and a workflow run against that commit
completes.

## 2026-09-27 — Milestone 3 Slice 3B-6: Linux CI evidence reconciliation

Documentation-only pass. The prior entry's "Linux CI has still not run"
statement is now superseded — the slice was committed
(`388e80a496fe39075c8261ab9c407d16c7130b82`) and pushed as an ordinary
fast-forward, and the resulting GitHub Actions run
([36338011857](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36338011857),
`ubuntu-24.04` x86_64, Python 3.12) was independently re-fetched and
re-verified against its own raw logs before writing anything below —
not merely trusted from an earlier report.

**Verified facts**: overall conclusion `success`; Docker preflight
succeeded; the pinned image was pulled and confirmed `linux/amd64`.

**The dedicated-step/complete-suite distinction, recorded explicitly so
a future reader cannot conflate the two**: the separately named "Run
real Docker verification tests (must execute, not skip)" step ran
exactly `python -m pytest tests/integration/test_slice_c.py -v` and
reported `3 passed in 2.28s` — this is Milestone 1's own 3 legacy
tests, and only those. It did **not** run
`tests/integration/test_slice_3b6.py`. This slice's own seven genuine
Docker-dependent tests (three real end-to-end/labels/recovery tests,
three real-SIGKILL tests, and the lock/reconciliation test — each
carrying its own per-test `requires_docker` marker) ran instead inside
the separate "Run complete
test suite" step, `python -m pytest -q`, with `CODEAGENT_REQUIRE_
DOCKER=1` independently confirmed present in that step's own logged
environment. With Docker genuinely available there, every one of those
seven tests' `requires_docker` marker was inactive (skipif conditions
false, so not skipped), and the module-level mandatory-Docker guard
(`CODEAGENT_REQUIRE_DOCKER=1` set with no daemon found fails collection
outright, verified by reading that guard's own source) would have
failed the entire run rather than letting any of the seven silently
skip. That step reported exactly `2613 passed, 3 skipped in 46.03s`,
verified against the raw log.

The step used `pytest -q`, which prints no test identities — the three
skips are recorded here only as three unidentified, platform/host-
specific skips, never guessed at. They are explicitly not evidence of
Docker unavailability: preflight succeeded, the image was pulled, and
`CODEAGENT_REQUIRE_DOCKER=1` was present for that exact step. Local
macOS evidence (2,616 passed, 0 skipped, from the prior entries) and
this Linux run account for the identical 2,616 collected outcomes as
2,613 passed plus these 3 skips.

The final "Verify no leftover CodeAgent verification containers" step
was independently confirmed to use the anchored pattern
`grep -E '^codeagent-(verify-|baseline-|verification-)'` against a
complete, unfiltered `docker ps -a --format '{{.Names}}'` listing —
covering all three CodeAgent container families — and its captured
output was empty; the step succeeded.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review — and is GitHub-hosted
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim. `RunController`/CLI entry-point wiring remains
absent and T-E1's existing partial-mitigation boundary in
`docs/threat-model.md` is unchanged by this evidence.

`CLAUDE.md`'s Slice 3B-6 bullet (both its opening framing, previously
"Linux CI not yet run for this slice," and its closing statement,
previously "Linux CI validation for this specific (thrice-corrected)
slice has not yet been run") and `docs/adr/0004-owned-resource-
lifecycle-and-reconciliation.md`'s Amendment 6 evidence section
(previously "Linux CI has not yet run for this (thrice-corrected)
slice") are both updated in place with the verified evidence above,
explicitly preserving the dedicated-step/complete-suite distinction.
`docs/threat-model.md` was inspected and contains no Slice-3B-6-specific
stale Linux-CI-pending statement to correct, so it was left untouched.
No other prose in either file — design, transition tables, ownership
rules, failure classification, inspection/mutation order, milestone
boundary, or local macOS evidence — was touched. The earlier
correction-pass `ENGINEERING_LOG.md` entries, which truthfully stated
Linux CI had not yet run *at the time each was written*, are left
unmodified, per this project's own established convention of never
rewriting historical entries to match later state.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, this file, and the ADR changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this pass
— the independently re-verified, completed CI run is the evidence.

## 2026-09-27 — Milestone 3 Slice 3B-7: shared lifecycle-projection coordination and the controller-facing checkpoint lifecycle-publication boundary

A review of the previously-proposed composition-root slice (wiring
`prepare_lifecycle()` into `RunController`) found two real correctness
defects in already-existing, already-ADR-accepted code, both
independently reproduced against the repository before any fix was
written, and neither hypothetical:

1. **Independent publisher cursors.** `LifecycleContainerPublisher` and
   `LifecycleCheckpointRefPublisher` each tracked their own `_current`
   `LifecycleProjection`. Traced the exact real sequence: after a
   `writer.advance_lifecycle_state()` call moves the real projection
   from `PREPARING` to `ACTIVE`, the very next write through either
   adapter's own stale `_current` (still `PREPARING`) fails with
   `STALE_EXPECTED_PROJECTION` immediately — `LifecycleProjection`
   equality is whole-object, so any field changing anywhere invalidates
   every independently-held snapshot, not just a race across container/
   checkpoint interleaving as first assumed. Fixed with a new
   `LifecycleProjectionCursor` (the one authoritative `current` per
   lease) and a `SharedLifecyclePublishers` bundle plus
   `create_shared_lifecycle_publishers()` factory — the only sanctioned
   coordinated-construction path. Both publishers' existing public
   constructors are completely unchanged and remain source-compatible
   for isolated use.

2. **Checkpoint publication was not controller-safe.**
   `LifecycleCheckpointRefPublisher.publish()` let a raw
   `LifecycleStoreError` escape uncaught, and `RunController.run()` has
   no enclosing try/except — confirmed by reading the method in full —
   so that error would have propagated straight out of `run()`,
   skipping `_fail()`, the domain transition, `_terminate()`, evidence
   capture, worktree disposal, and checkpoint-ref deletion entirely.
   Fixed with `checkpoint_session.CheckpointPublicationFailure`/
   `CheckpointPublicationError` (dependency-light, mirroring
   `container_lifecycle`'s Amendment 6 pattern exactly),
   `LifecycleCheckpointRefPublisher.publish()` now translating via an
   exhaustive map, and `RunController`'s two checkpoint call sites
   (`establish()`/`advance()`) now also catching it, mapped to a new
   `ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED`
   (`ErrorDomain.LIFECYCLE`) — named deliberately to avoid ambiguity
   with the existing `CHECKPOINT_REF_*` codes (which are about the Git
   mutation itself, never its lifecycle-projection record), with its
   own fixed, sanitized `OperationalError` message ("the checkpoint
   lifecycle projection transition could not be confirmed") distinct
   from the generic message the `CheckpointRefError`/
   `CheckpointSessionError` branches still use. This explicitly,
   narrowly revises Amendment 4's own previously-documented exception-
   identity contract at exactly these two call sites, not silently.
   `session.delete()`'s own failures remain under `_terminate()`'s
   existing broad teardown catch, unaffected — proven by a dedicated
   regression, not assumed. A same-session correction pass fixed one
   inaccurate claim before this slice was ever committed: an internal
   code comment (`errors.py`) and `_map_checkpoint_error`'s own
   docstring both originally asserted the Git mutation "was attempted"
   whenever `CheckpointPublicationError` occurs — false for a
   pre-mutation, transitional-intent publication failure, where Git is
   never reached at all. Corrected to state only that the lifecycle-
   projection record could not be confirmed, with whether Git was ever
   reached left explicitly dependent on which publication phase failed.

A third finding surfaced while fixing #2: `establish()`/`advance()`'s
existing confirmed-`UNCHANGED` recovery flow (`raise publish_exc from
exc`) reuses the same exception object across two `raise ... from ...`
statements. Independently verified with a minimal Python reduction
(`raise b from a` then `raise b from c` on the same `b`) that this
silently overwrites `b.__cause__` from `a` to `c`, demoting `a` to
`b.__context__` — which the `from` syntax also hides from every
standard traceback via `__suppress_context__=True`. This meant a real
recovery-publish failure's own underlying `LifecycleStoreError` would
have been silently lost the moment the existing "projection-consistency
failure dominance" rule (Slice 3B-3) re-chained the same exception
object to the triggering `CheckpointRefError`. Fixed with a new,
additive `CheckpointPublicationError.pre_recovery_cause` attribute,
populated only in this exact dual-failure phase; every other phase
(pre-mutation publish, post-mutation collapse publish, delete's
transitional publish, delete's own Git failure) has only one cause and
needed no change — each was individually traced against the current
source, not assumed identical to the others. No `ExceptionGroup`: this
repository has no precedent for one and no `RunController` catch site
would benefit from it; the existing linear chain already had a
deliberate meaning this fix preserves.

Verified (macOS, real Docker daemon): the five directly affected files
(`test_lifecycle_store.py`, `test_checkpoint_session.py`,
`test_errors.py`, `test_events.py`, `test_controller.py`) collected and
passed together, 1,099 passed, in both forward and reverse file order;
the thirteen-file focused set established since Slice 3B-6, 1,036
passed; the complete suite, 2,648 passed, 0 skipped, identical with and
without `CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check` clean. A real
lease/real-writer interleaving test drives the shared bundle through
`PREPARING→ACTIVE`, both container roles through confirmed absence, and
the full checkpoint-ref lifecycle with zero spurious staleness, then
fault-injects a durability-unconfirmed publication and proves explicit
`refresh()` re-syncs both facades sharing the cursor in one call, and
finally proves a genuine external stale write is still correctly
refused — no Docker or real Git-ref mutation needed for any of it.

Not implemented (unchanged scope, explicitly deferred): any
`prepare_lifecycle()` call from production orchestration, any
`RunController` composition/entry-point wiring, a `CLEANING`-publication
hook, any lifecycle state change during a real controller run, and any
worktree-tracking schema or allocation change — all depend on this
slice's fixes and would have reproduced the same two defects the moment
they touched more than one publisher against a real controller run.
`docs/threat-model.md`'s T-E1 entry is unaffected. Linux CI has not yet
run for this slice — do not claim it has until it is committed, pushed,
and a workflow run against that commit completes.

## 2026-09-27 — Milestone 3 Slice 3B-7: Linux CI evidence reconciliation

Documentation-only pass. The prior entry's "Linux CI has not yet run
for this slice" statement is now superseded — the slice was committed
(`6a452b1d095584d754cbc51f291d1ebd5de9f3c9`) and pushed as an ordinary
fast-forward, and the resulting GitHub Actions run
([36350258312](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36350258312),
`ubuntu-24.04` x86_64, Python 3.12) was independently re-fetched via the
GitHub API and its own raw logs before writing anything below — not
merely trusted from an earlier report.

**Verified facts**: overall conclusion `success`; every job step
concluded `success`; Docker preflight succeeded; the pinned image was
pulled and confirmed `linux/amd64`.

The dedicated "Run real Docker verification tests" step ran exactly
`python -m pytest tests/integration/test_slice_c.py -v`, reporting `3
passed in 2.63s` — this is Milestone 1's own legacy real-Docker suite;
it does not specifically exercise Slice 3B-7, which introduces no
real-Docker test of its own (none of this slice's work touches Docker
at all — it is a purely in-memory cursor/exception-boundary fix). The
separate "Run complete test suite" step ran `python -m pytest -q` with
`CODEAGENT_REQUIRE_DOCKER: 1` independently confirmed present in that
step's own logged environment, reporting `2645 passed, 3 skipped in
47.16s` — `2645 + 3` equals the local collected total of 2,648
established in the implementation entry above. That step used `pytest
-q`, which prints no test identities; the three skips are recorded here
only as unidentified, platform/host-specific skips, never guessed at,
and are explicitly not evidence of Docker unavailability, since Docker
was a required, already-confirmed precondition for that same step.

The final "Verify no leftover CodeAgent verification containers" step
was independently confirmed to use the anchored pattern `grep -E
'^codeagent-(verify-|baseline-|verification-)'` against a complete,
unfiltered `docker ps -a` listing, and its captured output was empty;
the step succeeded.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review — and is GitHub-hosted
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim. It adds no entry-point wiring, no lifecycle
state change during a real controller run, no `CLEANING` integration,
no worktree tracking, and no CLI behavior beyond what Slice 3B-7 itself
already implements — `docs/threat-model.md`'s T-E1 entry remains
unaffected, and `docs/threat-model.md` itself contains no Slice-3B-7-
specific statement to correct.

`CLAUDE.md`'s Slice 3B-7 bullet (both its opening framing, previously
"Linux CI not yet run for this slice," and its closing statement,
previously "Linux CI validation for this specific slice has not yet
been run") and `docs/adr/0004-owned-resource-lifecycle-and-
reconciliation.md`'s Amendment 7 evidence paragraph (previously "Linux
CI has not yet run for this slice") are both updated in place with the
verified evidence above, explicitly noting that the dedicated step
tests the legacy suite, not Slice 3B-7 specifically. No other prose in
either file — the cursor/publisher design, the exception-translation
boundary, the dual-cause contract, the message correction, or the
milestone boundary — was touched. The original Slice 3B-7 implementation
entry above, which truthfully stated Linux CI had not yet run *at the
time it was written*, is left unmodified, per this project's own
established convention of never rewriting historical entries to match
later state.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, this file, and the ADR changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this pass
— the independently re-verified, completed CI run is the evidence.

## 2026-09-28 — Milestone 3 Slice 3C-1: dependency-light owner-state lifecycle-publication boundary and a RunController terminal hook

Three planning-only passes preceded implementation (not narrated here
in detail — see the plan file this session tracked), converging on a
deliberately narrow slice after review found the originally-proposed
"real composition helper" plan contained three real defects
(mischaracterizing worktree-attribution deferral as permanent,
overclaiming an unbuilt composition helper as a "real application
execution path" that "mitigates T-E1," and an impossible construction
order constructing `DockerVerifier` before `GitWorktree`) and under-
specified three real design gaps (dependency inversion for owner-state
publication, lease-close timing versus an already-emitted `RunFinished`,
and the size of a real composition function's input surface). The
corrected, accepted boundary — an owner-state publication abstraction,
a shared-cursor adapter, and an optional `RunController` terminal hook,
with all composition/lease-ownership work explicitly deferred — is what
this entry implements.

**Production**: `src/codeagent/lifecycle_owner.py` (new, stdlib-only):
`OwnerStatePublicationFailure` (exactly the 10 members
`ContainerPublicationFailure`/`CheckpointPublicationFailure` already
use) and `OwnerStatePublicationError`. `src/codeagent/lifecycle_store.py`:
`LifecycleOwnerStatePublisher` (the concrete adapter — `activate()`/
`begin_cleanup()`/`complete()` map to `cursor.advance_state(ACTIVE/
CLEANING/COMPLETE)`, hiding `LifecycleState` from the caller entirely;
`complete()` relies on the writer's own existing `CLEANING -> COMPLETE`
clean-final gate rather than duplicating the shape check), an exhaustive
`_OWNER_STATE_PUBLICATION_FAILURE_MAP`, and a new third field,
`owner_publisher`, on `SharedLifecyclePublishers`/
`create_shared_lifecycle_publishers()`. `src/codeagent/controller.py`:
a new local `LifecycleOwnerPublisher` Protocol beside `Workspace`/
`CheckpointSessionLike`, one new optional `RunController.__init__`
parameter, an `activate()` call in `run()` immediately after
`RunStarted` (before any baseline/model/tool work — routed through the
existing `_terminate()` early-abort machinery on failure, the same
pattern already used for a failed initial `READ_FILE`), and
`begin_cleanup()`/`complete()` calls inside `_terminate()` folded into
the existing `cleanup_unconfirmed` precedence tier.
`src/codeagent/errors.py`: one new `ErrorCode.
LIFECYCLE_STATE_PUBLICATION_FAILED` (`ErrorDomain.LIFECYCLE`), used
only for a confirmed `activate()` failure at run start — distinct from
the existing `LIFECYCLE_CLEANUP_UNCONFIRMED`, which now also covers a
terminal-path `begin_cleanup()`/`complete()` failure (message text
widened, code value unchanged). Confirmed and left unmodified:
`src/codeagent/events.py` needs no change, since `RunFinished.
_UNRECOVERABLE_ERROR_CODES` is computed dynamically as
`set(ErrorCode) - {POLICY_VIOLATION_SEVERE}`.

**A corrected design decision applied before implementation** (caught
during the final planning pass, not found as a bug afterward): the
prior draft's failure table wrongly claimed evidence-capture failure
alone skips `complete()`. The accepted, implemented rule instead makes
`complete()` attempted whenever `begin_cleanup()` and all owned-resource
cleanup are confirmed, regardless of `evidence_receipt.success` —
`lifecycle.json` is an operational resource-recovery projection, not
the audit/evidence record, and the two must never be conflated. When
both an evidence failure and a `complete()` failure occur together,
`LIFECYCLE_CLEANUP_UNCONFIRMED` dominates the evidence error under the
existing, unchanged precedence — proved by a dedicated, load-bearing
test (`test_evidence_failure_and_complete_failure_together_are_
dominated_by_lifecycle_cleanup_unconfirmed`).

**Tests**: `tests/support/fakes.py` gained `FakeLifecycleOwnerPublisher`
(records exact call order; raises the real `OwnerStatePublicationError`
type on injected failure, exercising `RunController`'s catch/fold logic
identically to the real adapter). `tests/unit/test_lifecycle_store.py`
gained the map-completeness and `UNCLASSIFIED`-scope tests, all three
transitions' happy path, stale-expectation/pre-installation/wrong-lock-
scope/illegal-transition translation tests (mirroring the container/
checkpoint precedents' exact fault-injection techniques —
`lf.write_all_eintr_safe` for pre-installation, `lf.fsync_fd`'s second
call for durability-unconfirmed, a second independent writer for
staleness), a dedicated durability-unconfirmed installed-vs-cursor
split test using `refresh()` to prove the divergence, and updates to
the existing shared-bundle structural tests (`test_shared_publishers_
have_no_shadow_current`, `test_standalone_constructor_still_works_and_
is_source_compatible`, `test_standalone_adapters_must_never_be_combined_
against_one_writer`) to include the third publisher, plus a new,
focused real-writer/real-lease (no Docker) test proving
`PREPARING -> ACTIVE -> CLEANING -> COMPLETE` through the shared bundle
via `create_shared_lifecycle_publishers()` directly — no composition API
introduced. `tests/unit/test_errors.py` pins the new code's value and
domain. `tests/integration/test_controller.py` gained thirteen new
tests covering: omitted-collaborator behavior preservation, activation-
precedes-baseline ordering, activation-failure-skips-begin_cleanup/
complete, begin_cleanup-precedes-teardown ordering, begin_cleanup-
failure-does-not-stop-cleanup, complete-gated-on-confirmed-cleanup,
evidence-failure-does-not-block-complete, the combined-failure
dominance test, an original-run-failure (`BUDGET_EXCEEDED`) combined
with a terminal lifecycle-publication failure (also dominated), reason-
parity parametrized tests for both `activate()`/`complete()` failure
paths, and a no-automatic-retry test.

**A correction pass (2026-09-28, before this slice was considered
final) fixed two overclaims and one test-coverage gap**: (1) a comment
in `controller.py`'s `run()` and this ADR's own Amendment 8 section 2
wrongly said nothing had touched "Docker, Git, the worktree, or the
checkpoint ref" by the time `activate()` runs — false, since
`RunController` is always handed an already-entered `Workspace`
(`GitWorktree`), never one it enters itself, so a physical workspace
may already exist at this point; corrected to state precisely that no
*controller-driven* baseline/Docker verification/patch/checkpoint-ref
*mutation* has begun, and that this is exactly why `_terminate()` still
disposes/preserves the workspace and invokes session cleanup on an
activation failure. (2) `errors.py`'s comment on the new
`LIFECYCLE_STATE_PUBLICATION_FAILED` code wrongly said "the run never
got started" — false, since `RunStarted` and the `RUN_STARTED` domain
transition had already occurred by the time `activate()` runs;
corrected to say normal baseline/model/tool execution never began, not
that no run event exists. (3) only `activate()`'s installed-vs-cursor
durability-unconfirmed split had a real-writer fault-injection test;
`begin_cleanup()` and `complete()` each gained their own dedicated test
using the identical trailing-directory-`fsync` technique (driving the
real preceding transitions first), and the `complete()` case gained a
new, independent `tests/unit/test_reconciliation.py` test proving a
real reconciliation pass classifies a seeded `COMPLETE`-plus-absent-
shape entry as `SKIPPED_TERMINAL` with zero lock/inspection calls (the
same code path `RECONCILED` already exercised). `OwnerStatePublicationError`'s
docstring was also corrected from "raised only by
`LifecycleOwnerStatePublisher`" to the established Slice 3B-7 wording
pattern (`CheckpointPublicationError`'s own docstring): a public
exception any conforming `LifecycleOwnerPublisher` implementation may
raise, with `LifecycleOwnerStatePublisher` named as the sole current
production translation boundary. Two integration-test messages were
also strengthened to assert the exact sanitized text (see below) rather
than only a negative "detail does not leak" check.

**Verified** (macOS, real Docker daemon already running,
`CODEAGENT_REQUIRE_DOCKER=1`, post-correction-pass totals): `py_compile`
on all changed/new files; the four directly affected files
(`test_errors.py`, `test_lifecycle_store.py`, `test_reconciliation.py`,
`test_controller.py`) collected and passed together, 500 passed, in
both forward and reverse file order; the established sixteen-file
focused Milestone-3 set collected and passed together, 1,843 passed, in
both forward and reverse file order; the complete suite, 2,690 passed,
0 skipped; `git diff --check` clean; no leftover `codeagent-*`
containers (`docker ps -a` checked directly), extra `git worktree list`
entries, `refs/codeagent` refs, or temp state roots afterward — the
default macOS state-root location
(`~/Library/Application Support/CodeAgent`) was directly checked and
confirmed absent. No production path this slice introduces calls
`prepare_lifecycle()` at all; the real-writer tests that do call it do
so only through `_prepared_lease()`/`CODEAGENT_STATE_DIR`, redirected to
an isolated per-test `tmp_path` state root, never the default location.

**Exact sanitized messages, pinned by test**:
`LIFECYCLE_STATE_PUBLICATION_FAILED` — `"the run's lifecycle projection
could not be confirmed active"`; `LIFECYCLE_CLEANUP_UNCONFIRMED` (now
covering owner-state terminal publication failures too) — `"a verifier
container, worktree, or checkpoint-ref cleanup step, or the run's
lifecycle-projection bookkeeping, could not be confirmed"`. Both
integration tests assert the exact string and separately assert neither
the injected fake's detail text nor the categorical
`OwnerStatePublicationFailure` reason string (upper- or lower-case)
appears anywhere in the message.
`git status --short` shows only the intended files modified/added,
nothing staged. `docs/threat-model.md` is unchanged — T-E1 remains
explicitly unmitigated by this slice, and no existing statement there
became inaccurate. **Not implemented, by explicit design** (Slice
3C-2): any `prepare_lifecycle()` call from production orchestration,
any `RunController` composition/entry-point wiring, and who owns/closes
a `LifecycleLease` and what a lease-close failure does to the outer
operation's result — this slice never constructs or closes a lease.
Left unstaged and uncommitted for joint review, per this session's
explicit instruction. GitHub-hosted Linux CI has not yet run for this
slice — it has not been committed or pushed.

**A brief follow-up wording pass (2026-09-28, same day, before this
slice was committed)** found the prior correction's own replacement
text still overreached in one place: the same `errors.py` comment's
final sentence, comparing `LIFECYCLE_STATE_PUBLICATION_FAILED` to
`LIFECYCLE_CLEANUP_UNCONFIRMED`, said "this one means no real work
happened at all" — still too broad, since `RunStarted`/`RUN_STARTED`
had already occurred and an already-entered physical workspace may
exist and later require disposal or preservation. Replaced with
phase-accurate wording: `LIFECYCLE_CLEANUP_UNCONFIRMED` covers
terminal-path resource cleanup or lifecycle-bookkeeping that could not
be confirmed; `LIFECYCLE_STATE_PUBLICATION_FAILED` means the ACTIVE
projection transition itself could not be confirmed before normal
baseline/model/tool execution began. A repository-wide search for the
same phrase and its equivalents ("nothing was touched," "no work
happened") found no other occurrence in current, unsuperseded code
comments or documentation. This is wording-only: no control flow,
schema, test, or behavior changed; `py_compile` on `src/codeagent/
errors.py` and `git diff --check` both passed; the full suite was not
rerun, since nothing executable changed.

## 2026-09-28 — Milestone 3 Slice 3C-1: Linux CI evidence reconciliation

Documentation-only pass. Slice 3C-1 was committed
(`3476b3d7a50e2f979059031ab9c670949a7c20fe`) and pushed to `main` as an
ordinary fast-forward (`27d0607..3476b3d`, no force). The resulting
GitHub Actions run
([36483533854](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36483533854),
`ubuntu-24.04` x86_64, Python 3.12) was independently re-fetched via
`gh run view --json` and its own raw job logs — re-fetched a second
time in this pass specifically, confirmed byte-identical to the first
fetch (the run is completed and immutable) — before writing anything
below, not merely trusted from the prior turn's report.

**Independently verified facts**: head SHA `3476b3d7a50e2f979059031ab9c670949a7c20fe`
matches the new commit exactly; overall conclusion `success`; every job
step (`Docker preflight (mandatory)`, `Resolve pinned verification
image`, `Pull pinned verification image and confirm linux/amd64`, `Run
real Docker verification tests`, `Run complete test suite`, `Verify no
leftover CodeAgent verification containers`, and both `Post` steps)
concluded `success`. The Docker preflight step ran a real `docker info`
(`Server Version: 28.0.4` observed in its own log). The pinned image
`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
was pulled, and its own log line reads exactly `Pulled image reports
platform: linux/amd64`.

The dedicated "Run real Docker verification tests" step's own log shows
it ran exactly `python -m pytest tests/integration/test_slice_c.py -v`,
reporting `3 passed in 2.33s` — this is Milestone 1's own legacy
real-Docker suite; it does not specifically exercise Slice 3C-1, which
introduces no real-Docker test of its own (none of this slice's work
touches Docker — it is a controller/lifecycle-store/errors change plus
in-memory-cursor tests). The separate "Run complete test suite" step
ran `python -m pytest -q` with `CODEAGENT_REQUIRE_DOCKER: 1`
independently confirmed present in that step's own logged environment,
reporting `2687 passed, 3 skipped in 47.83s`. `2687 + 3` equals the
local collected total of 2,690 established in the implementation entry
above. **The complete-suite step reported 2,687 passed and 3 skipped;
the skipped-test identities are not printed by the `pytest -q` log** —
this pass does not guess them. These three skips are explicitly not
evidence of Docker unavailability, since Docker was a required,
already-confirmed precondition for that same step (the daemon was
already up, the pinned image already pulled, and
`CODEAGENT_REQUIRE_DOCKER=1` already confirmed set). Separately, and
labeled here explicitly as source inspection rather than CI-log
evidence: every prior Milestone 3 slice's own Linux CI run has reported
the identical three skips, and earlier entries in this log identify the
likely candidates by source inspection as three tests that
unconditionally skip on a case-sensitive-filesystem runner
(`test_evidence.py`'s Darwin-only ambient-`/tmp`-symlink test,
`test_lifecycle_fs.py`'s Darwin-only case-canonicalization test, and
`test_repo_identity.py`'s case-insensitive-filesystem-dependent alias
test) — this is inference from source, not something this run's own
`pytest -q` log proves, and none of Slice 3C-1's own new or modified
tests are among them (all of Slice 3C-1's tests are platform-
independent).

The final "Verify no leftover CodeAgent verification containers" step's
own log shows the exact pattern
`grep -E '^codeagent-(verify-|baseline-|verification-)'` applied to a
complete, unfiltered `docker ps -a` name listing — covering all three
required families (`codeagent-verify-*`, `codeagent-baseline-*`,
`codeagent-verification-*`) — and its captured output was empty; the
step succeeded.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review — and is GitHub-hosted
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim. It does not claim `prepare_lifecycle()`,
`LifecycleLease` ownership/closure, CLI wiring, or any production
entry-point composition exists — none of that is part of this slice.
`docs/threat-model.md`'s T-E1 entry is unaffected and unchanged by this
pass: it contains no Slice-3C-1-specific statement to correct, and
T-E1 remains unmitigated because production composition/entry-point
wiring is still absent.

`CLAUDE.md`'s Slice 3C-1 bullet (previously "not yet committed, pushed,
or reviewed on Linux CI," with no closing CI-evidence paragraph) and
`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
Amendment 8 evidence paragraph (previously "GitHub-hosted Linux CI has
not yet run for this slice — it has not been committed or pushed.")
are both updated in place with the verified evidence above. No other
prose in either file — the boundary design, the exhaustive failure map,
the evidence-independence rule, the durability-unconfirmed state table,
the deferred lease-ownership scope, or the milestone boundary — was
touched. Every earlier `ENGINEERING_LOG.md` entry, including this
slice's own prior implementation and two correction-pass entries above,
is left unmodified, per this project's own established convention of
never rewriting historical entries to match later state.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, this file, and the ADR changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this
pass — the independently re-verified, completed CI run is the
evidence.

## 2026-09-29 — Milestone 3 Slice 3C-2: deterministic lifecycle-scoped worktree placement

Two rounds of joint Codex/Claude planning review preceded this slice
(both investigation-only, no code changes, recorded as plan documents
outside this repository). Round 1 independently verified, and round 2
further precision-passed, that the "next composition slice" originally
proposed was not actually the smallest correct next step:
`reconciliation.py` already hard-codes the expected deterministic
worktree path (`state_root.path/worktrees/<repo_key>/<lifecycle_id>`,
`_reconcile_locked_entry`'s own `expected_worktree_path` computation),
but nothing in production ever placed a real worktree there —
`GitWorktree` only ever used an unpredictable `tempfile.TemporaryDirectory()`.
Composing a real run on top of that gap would have made reconciliation's
own worktree-absence check silently meaningless for every dead run
(always "confirmed absent," since the path it checks was never written
to by anything), which is a false-clean result — exactly the class of
bug ADR 0004 exists to prevent — not merely a missing feature. This
slice closes that specific gap, alone, before any composition work.

**Production**: `src/codeagent/state_root.py` gains
`StateRoot.reserve_worktree_leaf(repo_key, lifecycle_id)` — opens/creates
the `worktrees/<repo_key>/` parent chain via the existing, idempotently-
reopenable `open_managed_directory_chain` primitive, then exclusively
creates the `<lifecycle_id>` leaf via the existing
`create_exclusive_directory_at` (never reopened or adopted — `mkdirat`'s
own exclusivity refuses a pre-existing entry of any kind, including a
symlink, at that exact name) — and a new private `_WorktreeLeafReservation`
class, mintable in practice only through that method, matching
`lifecycle_store._LifecycleProjectionWriter`'s own established "the only
sanctioned way to obtain one" idiom rather than inventing a new
enforcement mechanism. The honestly-scoped guarantee this provides: no
string sourced from outside this mechanism can, on its own, become the
path `GitWorktree` trusts for this deterministic location — producing a
colliding `(descriptor, path)` pair that also survives `GitWorktree`'s
own independent same-inode re-verification requires already holding a
real, currently-valid descriptor to a real directory at that exact
path, which is not obtainable from a string alone. Defending against
hostile code already running inside this same trusted process remains
explicitly out of scope (`docs/threat-model.md` A4).

`src/codeagent/workspace.py`'s `GitWorktree` gains an optional,
keyword-only `reservation` parameter (default `None` — every existing
tempdir-based caller and test is completely unaffected). When given,
`__enter__` performs an fd-relative identity check
(`os.fstat`/`os.stat(..., dir_fd=..., follow_symlinks=False)`/
`os.path.samestat`) both immediately before `git worktree add` is ever
invoked and again immediately after the full worktree materialization
(add, read-tree, filter-safety check, checkout) succeeds — only a
doubly-reconfirmed success calls `reservation.consume()`, transferring
ownership of the directory/worktree resource (not descriptor-close
responsibility, which remains the reservation's own `__exit__`'s job
regardless of consumption state). `dispose()`/`preserve()`/`__exit__`
are otherwise completely unchanged.

**Two safety constraints raised by Codex during a same-day,
pre-finalization review round, independently verified against real code
and real git/POSIX behavior before being incorporated (neither accepted
on assertion)**:

1. A pathname-only `rmdir` for an unused reservation's cleanup cannot
   distinguish its own leaf from a same-user process's same-named,
   also-empty replacement. Verified real: `os.rmdir(name, dir_fd=parent_fd)`
   removes whatever currently occupies that name, with no inode check of
   its own. Fixed: the reservation holds the parent directory's own open
   descriptor for its entire lifetime and removes the leaf only via
   `os.rmdir(leaf_name, dir_fd=parent_fd)` after a fresh, immediately-
   preceding `os.stat(..., dir_fd=parent_fd, follow_symlinks=False)` +
   `os.path.samestat` identity re-check against the held `leaf_fd` — if
   that check disagrees or the leaf cannot be inspected, nothing is
   removed and a categorical `LifecycleFsError(CLEANUP_UNCONFIRMED)` is
   returned instead (never raised directly; folded into `__exit__`'s
   existing combined-cleanup-error convention). A genuinely absent leaf
   (most plausibly because `GitWorktree`'s own enter-time failure
   cleanup already removed the whole directory via a confirmed `git
   worktree remove --force`) is distinguished from a hostile replacement
   and treated as a clean, confirmed no-op — conflating the two was an
   early bug this same review's own test suite caught before this slice
   was considered final (see below).
2. The existing, unconditional enter-time failure cleanup
   (`git worktree remove --force <path>`) must never run against a
   pathname whose inode identity can no longer be reconfirmed after a
   full worktree materialization — doing so could operate on, or force-
   remove, a foreign directory a same-user race swapped into this exact
   deterministic path in the window Git's own pathname-only interface
   cannot close. Fixed: a new `_cleanup_failed_reservation_worktree`
   (the reservation-mode counterpart to the existing
   `_cleanup_failed_worktree`) re-verifies identity by fd first; if
   identity is confirmed intact, the existing exact-removal-and-confirm
   logic runs exactly as it already does for the legacy tempdir path
   (safe there because that pathname's own unpredictability made this
   race implausible in the first place); if identity disagrees or cannot
   be inspected, no destructive Git operation is attempted at all — the
   ambiguous pathname and Git registration are left exactly as they are,
   as evidence, and a sanitized `GitWorktreeCleanupError` is raised
   instead of ever claiming a foreign resource was safely cleaned up.

Both constraints were verified with real, executable scenarios before
being written into the design (a real repo, a real reservation, a real
directory swap, and a spied `_run` proving zero or the correct number of
Git invocations in each case) — not merely reasoned about — and the
disposable `/tmp` experiment from the round-1 review
(`git worktree add` accepts a pre-existing empty directory, exit 0;
refuses a pre-existing non-empty one, exit 128) is now pinned as an
actual automated assertion
(`test_git_accepts_the_real_pre_created_empty_directory`), not only a
comment.

**A related, explicitly out-of-scope finding surfaced during this same
review, deliberately not acted on here**: `RunController.run()` has no
top-level exception handler (confirmed by direct re-reading, matching
this file's own Slice 3B-7 entry's original finding), so `_terminate()`
— and therefore checkpoint-session/owner-state cleanup — never runs on a
fully unexpected exception from any collaborator. This slice's outer
`with` usage pattern (`with state_root.reserve_worktree_leaf(...) as
reservation: with GitWorktree(..., reservation=reservation) as path:
...`) closes the equivalent gap for the worktree specifically (its
`__exit__` still fires during Python's own exception unwinding even
when `_terminate()` never runs), but the checkpoint session and owner
state are not covered by anything this slice adds. This is recorded as
an explicit, unsettled review item for Milestone 3 Slice 3C-3's own
planning — it intersects ADR 0005's still-unimplemented cancellation
semantics and `_terminate()`'s existing failure-precedence tiers, and
was deliberately not decided in passing here.

**Tests added**: `tests/unit/test_state_root.py` (17 new tests:
the exact accepted ADR path; idempotent parent-chain reopening under a
different `lifecycle_id`; exclusive leaf collision refusal; symlink/
wrong-type/unsafe-parent refusal; hex32 validation; an unused
reservation removing only its own empty leaf; an unused reservation
refusing to remove a swapped-in different inode; an unused reservation
refusing a non-empty leaf; descriptor closure on unused cleanup and
after `consume()`; `consume()` not reversed by a later descriptor-close
failure; `consume()` cannot be called twice; simultaneous removal-and-
close failure combined into one error; a cleanup failure recorded, not
raised, when a body exception is already propagating; parent-descriptor
non-leak on an exclusive-create failure; and a static AST-level proof
that `state_root.py` contains no `shutil` import, no `rmtree` call, and
no `"prune"` literal, matching `workspace.py`'s own existing regression
guard). `tests/unit/test_workspace.py` (9 new tests covering the real
happy path, the legacy `reservation=None` path's continued behavior,
successful disposal, `preserve()` across the outer context exit, an
unexpected with-body exception, descriptor closure without leaf removal
after `consume()`, pre-Git identity mismatch causing zero `git worktree
add` invocations, a registration disagreement with inode identity still
proven using exact confirmed cleanup, and an inode-identity loss after
Git mutation never running a destructive removal). `tests/unit/test_reconciliation.py`
(2 new tests: a real, non-mocked `state_root.reserve_worktree_leaf()` +
`workspace.GitWorktree(reservation=...)` entry left registered — as a
crash before disposal would leave it — now causes a genuine `REFUSED`/
`blocked` reconciliation result, where before this slice no production
code path could ever construct this exact scenario with real Git at
all, only simulate it via a mock; and a real, present-but-never-
registered directory at the same deterministic path is refused
identically).

**Verified** (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`):
`py_compile` on both changed production files; the fourteen-file focused
set (`test_lifecycle_fs`, `test_repo_identity`, `test_state_locks`,
`test_state_root`, `test_lifecycle_store`, `test_checkpoint_session`,
`test_checkpoint_ref`, `test_reconciliation`, `test_bounded_subprocess`,
`test_container_lifecycle`, `test_docker_ownership`, `test_executor`,
`test_ci_container_leftover_check`, `test_workspace`) collected and
passed together, 1,146 passed, in both forward and reverse file order
(identical total both ways); the complete local suite, 2,719 passed, 0
skipped; `git diff --check` clean; no leftover `codeagent-*` containers
(checked via `docker ps -a --format '{{.Names}}'`), no extra `git
worktree list` entries beyond this repository's own checkout, no
`refs/codeagent` refs, and the real default macOS state-root location
(`~/Library/Application Support/CodeAgent`) confirmed not created by any
of this slice's tests. Docker was already running before this session
began; it was never started or restarted, and no administrator-access
dialog appeared at any point.

**Not implemented, unchanged from every prior slice's own stated
scope**: worktree write-ahead publication into `lifecycle.json` (the
schema's categorical refusal of every non-`absent` worktree shape is
untouched — `_validate_worktree_shape` was not modified), worktree
*removal* during reconciliation, any `RunController`/CLI/composition-root
change, and any Docker or checkpoint-session change. `docs/threat-model.md`'s
T-E1 entry is unchanged: nothing here is wired into a real entry point,
and concurrent-live-run mitigation is not claimed. This work has not
been committed, staged, or pushed, and Linux CI has not run for it.

## 2026-09-29 — Milestone 3 Slice 3C-2: same-day correction pass

A second joint Codex/Claude review round (still before this slice's
first commit) found and fixed four further real defects in the
implementation above, none of them reopening the accepted high-level
design (deterministic placement, fail-closed reconciliation, T-E1
status all unchanged).

1. **Reservation type not enforced at runtime.** `GitWorktree`'s
   `reservation` parameter carried only a bare type annotation, which
   Python does not enforce — a duck-typed impostor exposing
   `verify_identity()`/`path`/`consume()` could have been accepted and
   trusted exactly like a genuine `_WorktreeLeafReservation`, defeating
   the ordinary-programmer-error/external-string boundary the class's
   documented guarantee depends on. Fixed: `GitWorktree.__init__` now
   requires `isinstance(reservation, _WorktreeLeafReservation)` when
   non-`None`, checked first (before the Git preflight, before any
   reservation access), rejecting with a fixed, sanitized
   `GitWorktreeError` that never echoes the rejected object's repr or
   type name. This does not, and does not claim to, prevent hostile
   in-process code from importing the private class directly — only that
   an ordinary caller's mistake (a bare path string, a `Path`, an
   unrelated object) can no longer be silently treated as trusted.
2. **Single-consumer race.** `consume()` transitioned only a plain
   `_consumed` boolean, set solely at the very end of a fully successful
   entry. Until then, nothing stopped two `GitWorktree` instances from
   being constructed against the same reservation: both would pass
   `verify_identity()` (the path had not yet been mutated), both could
   reach `git worktree add`, and the second one's own failure (a
   directory `add` refuses because the first instance already
   materialized real content there) would route into
   `_cleanup_failed_reservation_worktree`, which re-verifies identity —
   still proven, since the swap this method actually defends against is
   a *different* threat — and would then run `git worktree remove
   --force` against the *first* instance's genuine, live worktree. Fixed
   with an explicit `_ReservationState` machine
   (`RESERVED -> CLAIMED -> CONSUMED`, `state_root.py`): `claim()`,
   protected by an internal `threading.Lock` (not a plain check-then-set
   on an attribute, which a real thread switch between the check and the
   set could still race), transitions `RESERVED -> CLAIMED` exactly once
   for exactly one caller; `GitWorktree.__enter__()` now calls it before
   any Git mutation is even considered, outside the try/except that
   handles Git-level cleanup, so a refused claim never triggers any
   cleanup logic that could touch another instance's resources;
   `consume()` is legal only from `CLAIMED`. A failed entry after a
   successful claim leaves the reservation `CLAIMED`, never resettable
   back to `RESERVED` — this slice deliberately does not design a
   proven-safe retry contract. Proven with a real two-thread
   `threading.Barrier`-synchronized test (`test_claim_state_transitions_under_a_real_thread_barrier`):
   exactly one of two genuinely concurrent threads succeeds.
3. **`rmdir`'s reported success trusted without independent
   observation.** `_remove_unconsumed_leaf_if_safe()` returned success
   immediately after `os.rmdir(name, dir_fd=parent_fd)`, contrary to
   this project's own cleanup discipline elsewhere (e.g.
   `GitWorktree.dispose()`'s explicit "only the independent final
   observation... decides whether disposal succeeded" rule). Fixed with
   a fresh, fd-relative, no-follow `os.stat` re-observation after
   `rmdir`: only a confirmed `FileNotFoundError` counts as success; an
   entry still present at that name — even a new, unrelated inode a
   same-user process created in the interim — is reported
   `CLEANUP_UNCONFIRMED` and left untouched, never removed a second
   time. The unavoidable residual race after this final observation
   itself (no POSIX interface makes "remove, then observe" atomic
   against a subsequent recreation) is documented honestly in the
   method's own docstring, not claimed as closed.
4. **A leaked descriptor and two weak tests, found while auditing every
   reservation-creation call site for a matching context exit.**
   `test_real_deterministic_worktree_registered_and_present_causes_refused_blocked`
   (`test_reconciliation.py`) obtained a reservation outside a `with`
   block and its `finally` called only `wt.dispose()`, never
   `reservation.__exit__()` — since the reservation was `consume()`d,
   nothing else would ever close its descriptors, leaking `parent_fd`/
   `leaf_fd` for the rest of the test process's life. Fixed by wrapping
   the same exact simulated-crash sequence in the reservation's own
   `with` block. Two pre-existing fault-injection tests in
   `test_state_root.py` that monkeypatched `close_confirmed` to always
   fail also leaked the real descriptors once the mock was undone in
   their own `finally` blocks (the reservation itself believed its
   descriptors were closed — `_descriptors_closed` is set
   unconditionally — but the real `os.close()` never ran); fixed by
   closing the real descriptors directly after `monkeypatch.undo()` in
   both. Separately, `test_pre_git_inode_mismatch_performs_zero_git_worktree_add`
   used `pytest.raises((RuntimeError, Exception))` — a near-meaningless
   assertion given `Exception` is the base of almost everything — fixed
   to assert the exact `LifecycleFsError`/`CLEANUP_UNCONFIRMED` reason.
   `test_inode_disagreement_after_git_mutation_never_runs_destructive_removal`
   set `reservation._consumed = True` purely to suppress the fixture's
   own cleanup after manually `git worktree remove --force`-ing the real
   worktree — a private-state lie (the reservation was never actually
   consumed) that, after finding 2's fix, would not even have worked
   (`_consumed` no longer exists). Removed entirely: by the time that
   line ran, the `mock.patch` context managers around it had already
   exited, so `reservation.verify_identity` was back to its real
   implementation, and the directory was now genuinely, confirmedly
   absent — letting the outer `with` block's own `reservation.__exit__`
   run naturally exercises finding 3's own "confirmed absent" branch for
   real, rather than hiding behind a fabricated state.

New tests added this pass (`tests/unit/test_workspace.py`): reservation
`None`/genuine-type/path-string/`Path`-object/duck-typed-impostor
acceptance or refusal (5 tests, plus one proving zero Git invocation on
a wrong-type rejection and one proving the rejection message never
echoes a hostile `__repr__`); a second `GitWorktree` sharing the same
reservation refused before any Git mutation, with the first instance's
real worktree confirmed untouched; a second claim refused after the
first worktree was preserved; claim-twice, consume-without-claim, and
entry-failure-after-claim each refused/handled correctly; the real
two-thread claim barrier test. `git diff --check` is included in the
verification below rather than repeated per finding.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`): the
fourteen-file focused set, 1,159 passed (up from 1,146), forward and
reverse file order; the complete local suite, 2,732 passed (up from
2,719), 0 skipped; `git diff --check` clean; no leftover `codeagent-*`
containers, extra worktrees, `refs/codeagent` refs, or temp state roots
afterward. Docker was already running before this pass began; it was
never started or restarted, and no administrator-access dialog
appeared. `CLAUDE.md`'s Slice 3C-2 bullet is updated in place with this
corrected design and the new totals; the ADR 0004 narrow
implementation-status note added for this slice was re-checked and
required no correction (it never claimed anything about reuse
prevention or cleanup confirmation specifics that these findings would
have overstated). This work remains uncommitted, unstaged, and unpushed.

## 2026-09-29 — Milestone 3 Slice 3C-2: second same-day correction pass (GitWorktree's own exception boundary)

A third joint Codex/Claude review round (still before this slice's
first commit) found two further real defects, both at `GitWorktree`'s
own public boundary rather than in the private reservation's internals,
plus one inaccurate comment.

1. **Raw `LifecycleFsError` escaping `GitWorktree.__enter__()`.**
   `_WorktreeLeafReservation.claim()` raised `LifecycleFsError` directly
   into `__enter__()`, and the second-claimant tests had been written to
   assert exactly that — which is itself the defect: `GitWorktree`
   never exposes `state_root`/`_lifecycle_fs` exception types to its own
   callers anywhere else in this module (every other failure path raises
   `GitWorktreeError`/`GitWorktreeCleanupError` only). Fixed by wrapping
   the `claim()` call in `try`/`except LifecycleFsError`, translating to
   a fixed, sanitized `GitWorktreeError` chained `from` the original.
   Deliberately no worktree-level cleanup is attempted for a refused
   claim: the reservation was never claimed by *this* instance, so its
   Git registration and directory (if any) belong entirely to whichever
   instance actually holds the claim — attempting cleanup here would be
   exactly the cross-claimant mutation this whole mechanism exists to
   prevent. The two sequential-second-claimant tests
   (`test_second_gitworktree_sharing_same_reservation_refused_before_any_git_mutation`,
   renamed from `..._git_call` per finding 3 below;
   `test_second_claim_after_first_worktree_preserved_performs_no_mutation`)
   now assert `GitWorktreeError`, its `__cause__` is the expected
   `LifecycleFsError` with the expected `CLEANUP_UNCONFIRMED` reason,
   zero `git worktree add`/`remove` mutation is attempted, and the first
   claimant's real worktree remains registered and present throughout.
   The private reservation's own direct `claim()`/`consume()` tests
   (`test_claim_twice_is_refused_categorically`,
   `test_consume_without_claim_is_refused`) are unchanged — they
   deliberately exercise the reservation's own API directly, not
   `GitWorktree`'s, so asserting the raw `LifecycleFsError` there remains
   correct.
2. **Ownership transfer outside cleanup protection.** `GitWorktree.__enter__()`
   called `self._reservation.consume()` *after* `self.path`/
   `self._initial_commit` were already published and *outside* the
   guarded `try`/`except` that handles reservation-aware cleanup. A
   `consume()` failure in that position would have both leaked a raw
   `LifecycleFsError` (the same class of defect as finding 1, at a
   different call site) and left the `GitWorktree` instance claiming a
   successful, usable entry it had never actually completed. Fixed by
   moving the `consume()` call inside the guarded try, immediately after
   post-Git identity/registration verification and strictly before
   `self.path`/`self._initial_commit` are set — a `consume()` failure
   there is translated to `GitWorktreeError` and falls straight into the
   existing `except BaseException` handler, running the identical
   reservation-aware exact-cleanup path (`_cleanup_failed_reservation_worktree`)
   any other post-materialization failure already uses, and leaving
   `self.path`/`self._initial_commit` unpublished. `consume()` itself
   only ever raises before mutating state (it checks `CLAIMED` first),
   so a failure here leaves the reservation exactly `claimed`, never
   `consumed` — its own outer context still performs the correct
   confirmed-absence handling via `__exit__`, proven directly rather
   than assumed. New load-bearing test
   (`test_consume_failure_after_successful_entry_translates_and_cleans_up`):
   forces `consume()` to fail via `mock.patch.object` after a real
   `git worktree add`/`read-tree`/filter-check/checkout has fully
   succeeded and post-Git verification has already passed; asserts the
   public `GitWorktreeError` and its exact `__cause__`, that
   `self.path` is `None` and `self.initial_commit` still raises, that
   the real Git registration and directory are genuinely gone (a fresh
   `git worktree list --porcelain` confirms absence — this is the
   "identity still proven, exact cleanup attempted and confirmed" path,
   since only `consume()` itself was fault-injected, nothing about the
   reservation's real identity), and that the reservation is left
   `claimed`, not `consumed`.
3. **Inaccurate comment.** One comment said `claim()` refuses "before
   any Git call," but `_rev_parse("HEAD")` against the source repository
   already runs before the reservation branch is even reached. Corrected
   throughout (`workspace.py`'s own comments, this file, `CLAUDE.md`) to
   "before any Git mutation," which is the property this mechanism
   actually protects and the one that is actually true — no behavior
   changed to satisfy the wording; the existing ordering was already
   correct, only its description was wrong. A full re-review of the
   complete Slice 3C-2 diff after this pass found no remaining raw
   `LifecycleFsError` escape from `GitWorktree`, no ownership transfer
   outside cleanup protection, no further stale "before all Git calls"
   claims, and no scope expansion beyond the same eight files this
   slice has touched throughout.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`): the
fourteen-file focused set, 1,160 passed (up from 1,159), forward and
reverse file order; the complete local suite, 2,733 passed (up from
2,732), 0 skipped; `git diff --check` clean; no leftover `codeagent-*`
containers, extra worktrees, `refs/codeagent` refs, or temp state roots
afterward. Docker was already running before this pass began; it was
never started or restarted, and no administrator-access dialog
appeared. `CLAUDE.md`'s Slice 3C-2 bullet is updated in place with
these two findings and the new totals. This work remains uncommitted,
unstaged, and unpushed.

## 2026-09-29 — Milestone 3 Slice 3C-2: Linux CI evidence reconciliation

Slice 3C-2 was committed (`7ec1fd2ce892c123491492c95ef6a990f841cbd2`)
and pushed to `main` as an ordinary fast-forward (`974035b..7ec1fd2`,
no force). This is a documentation-only pass reconciling that commit's
"Linux CI evidence... this work has not yet been pushed" placeholder
against the real, independently re-verified run.

GitHub Actions run [36642099820](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36642099820)
was independently re-fetched — run metadata via `gh run view --json`,
the complete raw step logs via `gh run view --log`, and the per-step
job breakdown via the raw `gh api repos/.../actions/jobs/<id>` endpoint,
all freshly re-queried rather than reused from any prior report — and
every fact below was verified directly against those fresh results, not
assumed from an earlier summary. The one same-artifact comparison
actually performed: the freshly re-downloaded raw log was diffed
byte-for-byte against the raw-log copy fetched during this slice's own
prior commit-finalization pass, and the two were confirmed identical;
the JSON run metadata and the jobs-API step breakdown were freshly
queried and read directly, not diffed against an earlier saved copy of
the same artifact.

- Head SHA: exactly `7ec1fd2ce892c123491492c95ef6a990f841cbd2`, matching
  the pushed commit.
- Overall conclusion: `success`; the single `Test (ubuntu-24.04, Python
  3.12)` job's steps, confirmed via the jobs API to number 13 in total
  (`Set up job`, `Check out repository`, `Set up Python 3.12`, `Install
  project and test dependencies`, `Docker preflight (mandatory)`,
  `Resolve pinned verification image`, `Pull pinned verification image
  and confirm linux/amd64`, `Run real Docker verification tests (must
  execute, not skip)`, `Run complete test suite`, `Verify no leftover
  CodeAgent verification containers (legacy and lifecycle-aware
  families)`, `Post Set up Python 3.12`, `Post Check out repository`,
  `Complete job`): every one of the 13 API-reported steps `success`.
- Runner scope, confirmed directly from the "Set up job" step's own
  log output: `Operating System: Ubuntu 24.04.5 LTS`; `Runner Image:
  ubuntu-24.04`; Python `3.12` (from the `Set up Python 3.12` step's
  own `with: python-version: 3.12`).
- Docker preflight (`docker info`): succeeded; Docker Engine - Community
  version `28.0.4` is directly present in the log.
- Pinned verification image: `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
  (from `Resolved DEFAULT_IMAGE:` in the "Resolve pinned verification
  image" step); pulled and confirmed `linux/amd64` (`Pulled image
  reports platform: linux/amd64`).
- Dedicated "Run real Docker verification tests (must execute, not
  skip)" step: exact command `python -m pytest
  tests/integration/test_slice_c.py -v`, `CODEAGENT_REQUIRE_DOCKER: 1`
  present in that step's own logged environment; result `3 passed in
  1.69s` (all three named individually as `PASSED` in the log; no
  `skipped` reported, i.e. 0 skipped). **This step is the legacy
  `test_slice_c.py` Milestone-1 suite, exactly, and is not specific
  evidence for Slice 3C-2** — this slice adds no real-Docker test of
  its own (its own three new-code-path tests are covered only inside
  the separate complete-suite step below, and are not themselves
  real-Docker-dependent — Slice 3C-2's own tests are all non-Docker,
  fd-relative/real-Git tests, not real-Docker ones).
- Separate "Run complete test suite" step: exact command `python -m
  pytest -q`, `CODEAGENT_REQUIRE_DOCKER: 1` also confirmed present in
  that step's own logged environment; result `2730 passed, 3 skipped in
  39.17s`. `2730 + 3` equals the local collected total of 2,733 reported
  in this slice's own prior commit-finalization pass.
- `pytest -q` prints no test identities, so these 3 skips are not
  identified from this run's own log and are not guessed here.
  Source inspection — explicitly labeled separately as source-based
  inference, not CI-log evidence — continues to identify the same
  three Linux-unconditional, platform/host-specific skips named in
  every earlier slice's own entry (`test_evidence.py`'s Darwin-only
  ambient-`/tmp`-symlink test, `test_lifecycle_fs.py`'s Darwin-only
  case-canonicalization test, and `test_repo_identity.py`'s
  case-insensitive-filesystem-dependent alias test) as the likely
  candidates.
- Final "Verify no leftover CodeAgent verification containers (legacy
  and lifecycle-aware families)" step: the exact anchored pattern
  `grep -E '^codeagent-(verify-|baseline-|verification-)'` was applied
  to a complete, unfiltered `docker ps -a --format '{{.Names}}'`
  listing, covering all three CodeAgent container families
  (`codeagent-verify-*`, `codeagent-baseline-*`,
  `codeagent-verification-*`); its captured output was empty
  (`CodeAgent verification containers currently present (expected:
  none):` followed by a blank line) and the step succeeded.

This is implementation/automated-test evidence only — it does not
constitute or substitute for a security review, and is GitHub-hosted
`ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
ARM64 portability claim. It does not claim `RunController`/CLI/
composition-root wiring, lifecycle-projection worktree attribution, or
any new T-E1 mitigation exists — Slice 3C-2 remains exactly what it was
committed as: an unwired prerequisite. `docs/threat-model.md` was
inspected and contains no Slice-3C-2-specific statement of any kind
(stale or otherwise) — it is unchanged, and T-E1 is not claimed
mitigated by this pass.

`CLAUDE.md`'s Slice 3C-2 bullet (previously ending "...concurrent-live-run
mitigation, and Linux CI evidence — this work has not yet been pushed")
and `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
narrow Slice 3C-2 implementation-status note (previously silent on CI
entirely) are both updated in place with the verified evidence above.
No other prose in either file — the reservation design, the state
machine, the exception-boundary translations, the cleanup ownership
rules, or the milestone/scope boundaries — was touched. Every earlier
`ENGINEERING_LOG.md` entry, including this slice's own three prior
same-day correction-pass entries (which correctly stated "uncommitted,
unstaged, and unpushed" and "Linux CI has not run for it" at the time
each was written), is left unmodified, per this project's own
established convention of never rewriting historical entries to match
later state.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, this file, and the ADR changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this
pass — the independently re-verified, completed CI run is the
evidence.

## 2026-10-02 — Milestone 3 Slice 3C-3: RunController ordinary-exception terminalization boundary

Implemented per ADR 0004's new "Amendment 9 (Accepted 2026-10-02)."
Closes a real, present gap flagged during Slice 3C-2's own review (not
the still-open worktree-transition-table or `LifecycleLease`
ownership/close-timing questions, both left exactly as deferred):
`RunController.run()` had no top-level exception boundary, so an
unexpected ordinary `Exception` from any collaborator propagated
straight out of `run()`, skipping evidence capture, worktree disposal,
checkpoint-ref deletion, and owner-state cleanup entirely.

Two planning/review passes preceded implementation (both prior to this
entry, same joint-review cadence as prior slices) and corrected the
mechanism before any code was written: (1) the fallback `_terminate()`
call must sit strictly after the `except Exception:` block exits, never
inside it, with the caught exception never bound to a name — otherwise
a teardown failure there would implicitly chain the discarded,
uncontrolled collaborator exception as `__context__`; (2) the "one-shot
guard" needed to be an actual check-and-raise
(`if self._termination_started: raise RuntimeError(...)`) at the top of
`_terminate()`, not a bare flag write with nothing checking it; (3) the
teardown-failure-escape tests must inject at `_capture_evidence` itself
(replacing the whole method), since the sink/workspace-level injection
points originally proposed are already caught internally and only
exercise existing precedence, not escape.

`src/codeagent/controller.py`: added `_termination_started`/
`_pass_index` instance fields; extracted the existing post-`RUN_STARTED`
body into `_run_after_start()` verbatim (no existing recognized-result
branch changed); `run()` now wraps that call in
`try: ... except Exception: if self._termination_started: raise` with
the fallback `_terminate(...)` call placed after the handler, using the
existing `ErrorCode.UNCLASSIFIED_FAILURE`/`ErrorDomain.INTERNAL` and a
fixed message, `"the run terminated due to an unanticipated internal
error"`; the loop body now sets `self._pass_index = pass_index` at the
top of each iteration; `_terminate()` gained its check-and-set guard as
its first two lines, otherwise unchanged.

`tests/integration/test_controller.py`: `_build()` gained optional
`verifier`/`approval`/`patch_applier` override parameters (mirroring the
existing `model`/`reader`/`workspace`/`session`/`evidence_sink`
pattern) so the new tests can inject a plain, uncategorized exception
from any collaborator method. Twelve new tests: unexpected-exception
terminalization from lifecycle-owner activation, baseline verification,
the model/read-plan phase, approval, patch application (confirmed to
occur after the checkpoint ref is genuinely established and the
worktree entry gate has passed — not "before any resource exists"),
and non-baseline verification (confirmed to occur after a real
checkpoint already exists from the same pass's patch application);
`KeyboardInterrupt`/`SystemExit` propagating unconverted; a direct
second-`_terminate()`-invocation regression proving the guard refuses
re-entry before any additional cleanup or event emission; a direct
teardown-failure-escape regression (via a replaced `_capture_evidence`)
proving no re-entry and no `RunFinished`; and a real end-to-end
regression combining an unexpected collaborator exception with a
second, independent teardown failure, proving the escaping exception's
`__context__` and `__cause__` are both `None` and the discarded
collaborator exception's text/type appears nowhere in it or in any
emitted event — all twelve passed on first run, confirming the
chaining-safety reasoning held in practice, not only on paper.

One pre-existing test required a correction, found only by running the
full file after implementation: `test_fake_model_gate_actually_fails_
without_the_marker` had asserted `pytest.raises(AssertionError)` around
`controller.run()`, relying on the fact that no boundary previously
existed to let `MarkerGatedFakeModel`'s internal `AssertionError` (an
ordinary `Exception`, used as that fake's own sanity-check mechanism)
escape raw. This is not a regression to paper over — it is the new
boundary behaving exactly as specified on a real exception-raising
fixture. The test now asserts the new, correct, intended shape: the
same `AssertionError` is caught and terminalized as
`UNCLASSIFIED_FAILURE`/`UNRECOVERABLE_ERROR`. The gate still provably
fires; it no longer escapes raw. No other existing test in the file
required any change.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`):
`py_compile` on all three changed files; `test_controller.py` alone, 60
passed; the directly affected set (`test_controller.py` +
`test_errors.py` + `test_events.py`) collected and passed together, 806
passed, in both forward and reverse file order; the established
fifteen-file focused Milestone-3 set (the prior fourteen-file set
established since Slice 3A-1, plus `test_controller.py`) collected and
passed together, 1,220 passed, in both forward and reverse file order;
the complete suite, 2,744 passed, 0 skipped, identical with
`CODEAGENT_REQUIRE_DOCKER=1` set; `git diff --check` clean; no leftover
`codeagent-*` containers (confirmed via `docker ps -a`), extra
worktrees (`git worktree list`), `refs/codeagent` refs, lingering
processes, or temp/default state roots (`~/Library/Application
Support/CodeAgent` confirmed absent) afterward. Docker was already
running before this work began; it was never started or restarted, and
no administrator-access dialog appeared.

Not implemented, unchanged scope from every prior slice:
`prepare_lifecycle()` production wiring, any composition root, the
worktree transition/combination table (or a decision to omit worktree
attribution), `LifecycleLease` ownership/close-timing, CLI/UI, signal
handling/cancellation, any Docker behavior change, reconciliation
change, or abandonment. `docs/threat-model.md` was inspected and left
unchanged — this is a controller-internal correctness fix, not a new
concurrent-run or lifecycle-attribution mitigation, and T-E1's status is
unaffected.

All changes (`src/codeagent/controller.py`,
`tests/integration/test_controller.py`,
`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
`CLAUDE.md`, this entry) are left unstaged and uncommitted for joint
review, per instruction. Linux CI has not run for any of this.

### 2026-10-02 — Milestone 3 Slice 3C-3: correction pass (lifecycle-owner cleanup, fallback precedence, chaining-regression completeness, one documentation overclaim)

A targeted review of the above found four real gaps between the
reviewed acceptance criteria and what the first implementation pass
actually tested or documented, all fixed before this slice is
considered final; none required any further change to
`src/codeagent/controller.py`'s production control flow itself.

1. **No test proved lifecycle-owner cleanup runs to completion on the
   new fallback path.** Every prior unexpected-exception test either
   had no `lifecycle_owner` at all, or was the activation-failure case
   (where `begin_cleanup()`/`complete()` are correctly never attempted,
   since `ACTIVE` was never confirmed). Added
   `test_unexpected_exception_after_activation_still_completes_
   lifecycle_owner_cleanup`: activation succeeds, a subsequent
   unexpected exception from the patch applier (after a real checkpoint
   ref is established) triggers the fallback, and
   `owner.calls == ["activate", "begin_cleanup", "complete"]` is
   asserted directly — proving the fallback path drives the identical
   owner-state sequence every other terminal path already did.

2. **No precedence test existed for the new fallback path
   specifically.** Every existing evidence-failure/cleanup-unconfirmed
   precedence test predates this slice and only ever triggers through
   one of the controller's own typed-result returns, never through the
   new `except Exception:` fallback. Added two tests:
   `test_evidence_capture_failure_overrides_unclassified_failure_on_
   fallback_path` (an unexpected collaborator exception triggers the
   fallback; `FakeEvidenceSink.capture` raises through its existing
   `raise_error` constructor parameter; the existing, unchanged
   `_capture_evidence()` internal catch converts it to the existing
   `EVIDENCE_CAPTURE_FAILED` receipt, which overrides
   `UNCLASSIFIED_FAILURE` exactly as the existing precedence rule
   already specified) and
   `test_workspace_dispose_failure_overrides_unclassified_failure_on_
   fallback_path` (same trigger; `FakeWorkspace.dispose` raises; final
   error becomes `LIFECYCLE_CLEANUP_UNCONFIRMED` with its exact
   existing sanitized message; checkpoint-ref deletion is skipped;
   `begin_cleanup()` is attempted but `complete()` is skipped; evidence
   capture still runs exactly once). Both assert neither injected
   exception's text leaks into `finished.error.message` or any emitted
   event. The previously-unused `allow_override_codes` parameter on the
   shared phase-test helper (never actually passed a non-empty tuple
   anywhere) is removed; the helper is renamed
   `_assert_unclassified_fallback` and asserts only the plain,
   non-overridden shape, since the two precedence cases above now get
   fully explicit, exact assertions of their own instead of a
   permissive parameter that could have silently accepted the wrong
   override.

3. **The fallback teardown-chain regression inferred "exactly one
   termination entry" rather than measuring it, and never scanned
   already-emitted events for the collaborator marker/type, and never
   checked escaping-instance identity.**
   `test_fallback_terminate_teardown_failure_has_no_chained_
   collaborator_context` now wraps `controller._terminate` with a
   direct call-count spy (asserted `== 1`), records the exact sentinel
   instance `_raise_sentinel` creates and asserts the exception that
   escapes `run()` `is` that same instance, and iterates every event in
   `controller.log.events` asserting neither
   `"boom-collaborator-should-never-be-chained"` nor `"RuntimeError"`
   appears in any event's `repr()` — not only in the escaped exception's
   own `str()`, as the prior version checked.

4. **Documentation overclaim.** Amendment 9's "Mechanism" section had
   stated, unqualified, that no "collaborator input" is ever persisted
   — read literally, this contradicts the fact that ordinary events
   emitted *before* the unexpected exception (e.g. `RunStarted`,
   `BaselineRecorded`, a prior successful `ToolCompleted`) still carry
   their normal, schema-approved run/plan/tool data exactly as before;
   this slice never touches those. Corrected to the precise, narrower
   claim this mechanism actually guarantees: no type, message, repr,
   traceback, marker, or other uncontrolled detail *derived from the
   caught exception itself* is ever persisted, emitted, logged,
   interpolated, or chained — explicitly distinguished from the run's
   otherwise-ordinary event trace. No other file (`CLAUDE.md`,
   `src/codeagent/controller.py`'s own comments/docstrings) was found to
   contain the same overclaim on inspection.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`;
Docker was already running and was not started or restarted; no
administrator-access dialog appeared): `test_controller.py` alone, 63
passed (up from 60 — three new tests; the fourth change was a
pre-existing test's regression, now more thorough, not a new test
count); the directly affected set (`test_controller.py` +
`test_errors.py` + `test_events.py`), 809 passed (up from 806), in both
forward and reverse file order; the fifteen-file focused Milestone-3
set, 1,223 passed (up from 1,220), in both forward and reverse file
order; the complete suite, 2,747 passed (up from 2,744), 0 skipped,
with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check` clean; no
leftover `codeagent-*` containers, extra worktrees, `refs/codeagent`
refs, lingering processes, or temp/default state roots afterward.

No scope change: `prepare_lifecycle()` production wiring, any
composition root, the worktree transition table, `LifecycleLease`
ownership/close-timing, CLI/UI, and signal handling remain exactly as
unimplemented and out of scope as the original 3C-3 entry above states.
`docs/threat-model.md` was re-inspected and remains unchanged — nothing
in this correction pass alters T-E1 or any other listed threat. All
changes remain unstaged and uncommitted; Linux CI has not run for any
of this.

## 2026-10-02 — Milestone 3 Slice 3C-3: Linux CI evidence reconciliation

Slice 3C-3 (including its same-day correction pass) was committed
(`bd90a992a001a2f9e9aed42ea65138802065f5cb`) and pushed to `main` as an
ordinary fast-forward (`07a0231..bd90a99`, no force). This is a
documentation-only pass reconciling `CLAUDE.md`'s and ADR 0004
Amendment 9's "not yet committed, pushed, or Linux-CI-confirmed" /
"Linux CI confirmation is pending ... nothing has been pushed"
placeholders against the real, independently re-verified run. No
production code, test, workflow, configuration, dependency, accepted
behavior, or `docs/threat-model.md` status was changed.

GitHub Actions run [37052793645](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37052793645)
was independently re-fetched — fresh run metadata via
`gh api repos/.../actions/runs/37052793645`, a fresh per-job/per-step
breakdown via `gh api repos/.../actions/runs/37052793645/jobs`, and the
complete raw step logs via `gh run view --log`, all freshly re-queried
rather than reused from the prior commit-finalization report — and
every fact below was verified directly against those fresh results.

Verified: `head_sha` exactly `bd90a992a001a2f9e9aed42ea65138802065f5cb`;
`conclusion` `success`; `status` `completed`; exactly one job ("Test
(ubuntu-24.04, Python 3.12)"), itself `success`, with exactly 13
API-reported steps, every one `success` (Set up job; Check out
repository; Set up Python 3.12; Install project and test dependencies;
Docker preflight (mandatory); Resolve pinned verification image; Pull
pinned verification image and confirm linux/amd64; Run real Docker
verification tests (must execute, not skip); Run complete test suite;
Verify no leftover CodeAgent verification containers (legacy and
lifecycle-aware families); Post Set up Python 3.12; Post Check out
repository; Complete job). GitHub-hosted `ubuntu-24.04` x86_64, Python
3.12, confirmed directly from the job name and the raw log's own
"Operating System: Ubuntu 24.04.5 LTS" line. Docker preflight: Docker
Engine - Community, version `28.0.4`, directly present in the raw log;
step succeeded. Pinned verification image resolved as
`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
and, after `docker pull`, `docker inspect --format '{{.Os}}/{{.Architecture}}'`
reported exactly `linux/amd64`. The dedicated "Run real Docker
verification tests (must execute, not skip)" step ran exactly
`python -m pytest tests/integration/test_slice_c.py -v` with
`CODEAGENT_REQUIRE_DOCKER=1` confirmed present in that step's own
logged environment, and the verbose output shows all three of its own
tests individually `PASSED` (no `SKIPPED` line anywhere in that step),
concluding `3 passed in 2.37s` — this is this file's own legacy
Milestone-1 suite, exactly, and is explicitly **not** Slice-3C-3-specific
evidence, since this slice adds no real-Docker test of its own. The
separate "Run complete test suite" step ran exactly
`python -m pytest -q` with `CODEAGENT_REQUIRE_DOCKER=1` also confirmed
present in that step's own logged environment, concluding
`2744 passed, 3 skipped in 49.20s`. `pytest -q` prints no test
identities anywhere in this log, so these 3 skips are not identified or
guessed from the log — source inspection (not CI-log evidence)
separately continues to identify the same three Linux-unconditional,
platform/host-specific skips named in every earlier slice's own entry
(`test_evidence.py`'s Darwin-only ambient-`/tmp`-symlink test,
`test_lifecycle_fs.py`'s Darwin-only case-canonicalization test, and
`test_repo_identity.py`'s case-insensitive-filesystem-dependent alias
test) as the likely candidates, labeled explicitly as inference, not
something this run's own log proves. `2744 + 3` equals the local
macOS collected total of 2,747 reported in the correction-pass entry
above. The final leftover-container-check step ran the anchored
`grep -E '^codeagent-(verify-|baseline-|verification-)'` pattern over a
complete, unfiltered `docker ps -a --format '{{.Names}}'` listing,
covering all three CodeAgent container families (the legacy
`codeagent-verify-*` and the deterministic lifecycle-aware
`codeagent-baseline-*`/`codeagent-verification-*` families); its
captured output was empty, and the step succeeded.

`CLAUDE.md`'s Slice 3C-3 bullet (previously ending "...is implemented
and locally verified on macOS (2026-10-02); **not yet committed,
pushed, or Linux-CI-confirmed**" at its opening, and "Still not
committed, pushed, or Linux-CI-confirmed; scope and
`docs/threat-model.md`'s unchanged status are unaffected" at its
correction-pass closing) and `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
Amendment 9 evidence text (previously "Linux CI confirmation is pending
as of this commit (not yet pushed)" after the first pass's own evidence
paragraph, and "Linux CI confirmation remains pending; nothing has been
pushed" at the correction pass's own closing) are all updated in place
with the verified evidence above, following this project's established
convention (see, e.g., the 2026-09-29 Slice 3C-2 Linux CI evidence
reconciliation entry above) of correcting a now-stale present-tense
status statement in these two living documents, as distinct from
`ENGINEERING_LOG.md` itself. No other prose in either file — the
control-flow structure, the exception/precedence semantics, the
lifecycle-owner-cleanup and precedence tests, the chaining-regression
strengthening, or the milestone/scope boundaries — was touched.

Every earlier `ENGINEERING_LOG.md` entry, including this slice's own
two same-day entries above (which correctly stated "Linux CI has not
run for any of this" and "nothing has been pushed," respectively, *at
the time each was written*), is left unmodified, per this project's
own established convention of never rewriting historical entries to
match later state.

`docs/threat-model.md` was inspected and contains no Slice-3C-3-specific
statement of any kind (stale or otherwise) to correct — it remains
unchanged. No threat is claimed newly mitigated by this pass: Slice
3C-3 remains a controller-internal correctness change (an ordinary-
exception terminalization boundary inside `RunController`), not
production lifecycle composition or concurrent-run protection, and
T-E1's existing partial-mitigation status is unaffected.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, the ADR, and this file changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this
pass — the independently re-verified, completed CI run is the
evidence.

## 2026-10-02 — Milestone 3 worktree-attribution substrate slice: expected_head semantics corrected and implemented

A joint review of the proposed "Slice 3C-4" plan found the two
originally-proposed `worktree.expected_head` designs both incomplete
before any implementation began. This entry records the finding and the
resulting implementation, per ADR 0004's new "Amendment 10 (Accepted
2026-10-02)."

**The finding, confirmed by direct code trace, not assumption**:
`controller.py`'s `_dispatch_apply_patch` calls
`self._patch_applier.apply(...)` (Step 5, line ~1127) — which commits
directly into the worktree, moving its real `HEAD` from `A` to `B` —
*before* calling `self._session.advance(result.commit_hash)` (Step 7,
line ~1150). `checkpoint_session.py`'s `advance()` then durably publishes
`CheckpointTransition(intent=ADVANCING, accepted_sha=A,
expected_old_sha=A, proposed_new_sha=B)` *before* attempting the Git
compare-and-swap. So `checkpoint_ref.accepted_sha == A` while the real
worktree `HEAD` is already `B` is the **normal window on every
successful patch application** — not a crash edge case. A proposed
cross-field rule ("worktree HEAD must equal checkpoint_ref.accepted_sha")
would misclassify this routine window as a conflict. Separately, a
confirmed-`UNCHANGED` Git CAS failure recovers `checkpoint_ref` to
`PRESENT(A)` while the real worktree remains contaminated at `B` until
disposal — already correctly handled by ADR 0003's own entry/resume
gate, independent of any worktree-projection field. Independently
republishing `expected_head` on every patch (the other original
candidate) was confirmed to require new `RunController`/
`CheckpointSession` ordering and failure-interleaving — not an
independent substrate change.

**Resolved interpretation**: `worktree.expected_head` is the immutable
materialization/origin commit for one worktree incarnation — fixed at
`creating`, retained unchanged through `present`/`disposing`, cleared at
`disposing->absent`; never republished on checkpoint advances; never
compared against live `HEAD` for ownership (ADR 0004 section 8's own
already-accepted text never required `HEAD`-matching — only
deterministic path, safe identity/type, exact Git registration, and
persisted non-absent intent). An exploratory reconciler-table row
(raised only during review, never published) refusing a `present`/
`disposing` worktree on live-`HEAD` disagreement does not survive this
analysis and is explicitly withdrawn, not part of Amendment 10.

**Implementation**: `src/codeagent/worktree_lifecycle.py` (new,
dependency-light, stdlib-only, mirroring `container_lifecycle.py`/
`lifecycle_owner.py` exactly) defines `WorktreeIntent` (moved from
`lifecycle_store.py`, re-imported there for source compatibility — the
identical move `ContainerIntent` made in Slice 3B-6),
`WorktreeTransition` (a validated value object — chosen over a
container-style `publish(intent, expected_head)` API because the
worktree's combination rule, like `checkpoint_ref`'s own, is a real
cross-field invariant, not a simple per-intent-nullable value;
reused directly as `LifecycleProjection.worktree`'s own field type,
retiring `WorktreeAttribution` — the identical consolidation
`CheckpointTransition` already made for `checkpoint_ref`),
`WorktreeTransitionPublisher` Protocol (with `lifecycle_id`/
`state_root_id` identity properties for a future integration's benefit,
mirroring `ContainerTransitionPublisher`'s own correction-pass
rationale), and `WorktreePublicationFailure`/`WorktreePublicationError`
(the identical 10-member taxonomy and shape as the container/owner-state
precedent).

`src/codeagent/lifecycle_store.py`: `_validate_worktree_shape` extended
from "absent only" to the full four-shape table, with object-format-
aware OID validation reusing the existing `_is_valid_oid_for_format`
helper (no new validation logic, only a new call site, identical
rejection behavior to `checkpoint_ref`'s own fields: malformed,
uppercase, wrong length, all-zero, cross-format); `_validate_worktree_edge`
implementing the legal owner edges (`absent->creating`,
`creating->present`, `creating->absent` [caller-confirmed recovery only],
`present->disposing`, `disposing->absent`) plus immutable-OID-continuity
(`creating->present` and `present->disposing` must each retain the exact
same `expected_head`); `_LifecycleProjectionWriter.
record_worktree_transition()` with the identical lock-scope/fresh-
authoritative-read/stale-check/state-gate/shape-validation/edge-
validation/publish ordering every other resource-transition method
already uses (lock → state gate → type-check → no-op check → OID-format
check → edge validation → publish, mirroring
`record_checkpoint_ref_transition` exactly); `LifecycleWorktreePublisher`
(identical `__init__`/`_from_cursor`/`publish`/`refresh` shape to
`LifecycleCheckpointRefPublisher`) with its own exhaustive
`_WORKTREE_PUBLICATION_FAILURE_MAP` (test asserts its keys equal the
complete `LifecycleStoreFailure` enum); `SharedLifecyclePublishers`/
`create_shared_lifecycle_publishers` extended to a 4th `worktree_publisher`
field sharing the existing `LifecycleProjectionCursor` — avoiding the
Slice 3B-7 independent-stale-cursor bug by construction, for this 4th
facade exactly as for the first three, proven by a new shared-cursor
interleaving test touching all four facades together.

Four pre-existing tests required correction, not weakening, since
stopping the categorical refusal of non-absent worktree shapes is this
slice's entire point:
`test_worktree_non_absent_intent_categorically_refused` split into
`test_worktree_non_absent_intent_with_valid_oid_accepted` (now correctly
asserting acceptance for a valid OID) and
`test_worktree_non_absent_intent_without_expected_head_refused`
(preserving the still-correct combination-rule refusal when
`expected_head` is `None`); and
`test_cleaning_to_complete_worktree_dirty_shape_is_unloadable` was
renamed `test_cleaning_to_complete_worktree_dirty_shape_is_refused_by_
clean_final_guard` and updated to assert `ILLEGAL_TRANSITION` (the
clean-final guard's own worktree check, genuinely reachable for the
first time) rather than `SCHEMA_INVALID` (which predated this
amendment and could never have been exercised for real, since no
non-absent shape could previously be loaded at all). The one external
reference to the retired `lifecycle_store.WorktreeAttribution` name
(`tests/unit/test_lifecycle_store.py`) was updated directly to
`WorktreeTransition` — its full replacement, not a kept alias nothing
else needs, since the two types have the identical two-field shape and
`WorktreeTransition`'s added `__post_init__` validation is the entire
point of the consolidation.

New tests: `tests/unit/test_worktree_lifecycle.py` (27 tests — value-
object construction for every valid shape and every malformed
`expected_head` variant, the publication-failure/-error pair, static
AST-based proof of stdlib-only dependency-lightness mirroring
`container_lifecycle.py`'s own precedent); `tests/unit/
test_lifecycle_store.py` gained 46 new tests (256 total, up from 210):
schema-level OID-shape acceptance/rejection for every intent and both
object formats, every legal owner edge (full happy-path round trip,
the caller-confirmed `creating->absent` recovery edge), every illegal
edge (parametrized, reached via the correct legal setup path for each
case so the edge under test is isolated), immutable-OID-continuity
refusal for both continuity-bearing edges, exact-no-op zero-publication-
I/O, lock-scope/stale/state-gate ordering (mirroring the existing
checkpoint-ref correction-pass tests), reconciler-owned-state refusal
including no-ops, the exhaustive publication-failure map and its
`UNCLASSIFIED`-reachability test, the durability-unconfirmed installed-
vs-cursor split with explicit `refresh()` recovery and no automatic
retry, a shared-cursor interleaving test driving all four publishers
together with a fault-injected durability-unconfirmed failure and
cross-facade refresh recovery, updated clean-final/reconciliation-
eligibility predicate tests (confirming unchanged `absent` behavior and
new, correct `non-absent` rejection), a real writer-driven
`CLEANING->COMPLETE` refusal reached via `record_worktree_transition`
itself (not only the pre-existing `_publish_raw` bypass test), and a
static source-level proof that `workspace.py`/`controller.py`/
`reconciliation.py` import nothing from this slice and call none of its
new functions.

Explicitly out of scope, consistent with every prior substrate-only
slice's own stated boundary: any `workspace.py`/`GitWorktree`
publication call (no production integration — proven via direct
writer/publisher calls using object-format-valid synthetic SHA-1/
SHA-256 OIDs (repeated-hex-character test fixtures, e.g. `"a" * 40`),
never a real `git rev-parse`-derived commit and never a real worktree),
any `RunController`/composition-root wiring, any
`checkpoint_session.py` change, any `reconciliation.py` change or
worktree removal (`is_projection_reconciliation_eligible_shape` still
requires worktree `ABSENT`, unchanged), the `StateRoot.
reserve_worktree_leaf()`/lifecycle-projection crash-gap interaction
(named, not resolved), CLI, signal handling, abandonment, or model/UI
work.

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`;
Docker was already running and was not started or restarted; no
administrator-access dialog appeared): `py_compile` on all four
changed/new files; `test_worktree_lifecycle.py` alone, 27 passed;
`test_lifecycle_store.py` alone, 256 passed (up from 210); the
sixteen-file focused Milestone-3 set (the prior fifteen-file set
established since Slice 3A-1, plus the new file) collected and passed
together, 1,299 passed, in both forward and reverse file order; the
complete suite, 2,823 passed, 0 skipped, with `CODEAGENT_REQUIRE_DOCKER=1`;
`git diff --check` clean; no leftover `codeagent-*` containers, extra
worktrees, `refs/codeagent` refs, lingering processes, or temp/default
state roots afterward.

`docs/threat-model.md` was inspected and left unchanged — this slice
adds no production wiring of any kind, so T-E1 and T-F2 remain exactly
as already stated ("not implemented"/"none yet" for worktree removal),
unaffected; no threat is claimed newly mitigated.

Separately, this investigation reconfirmed an existing documentation-
debt item, not corrected in this pass: CLAUDE.md's two "SHA-256
object-format coverage is still pending" statements (near the slice
2B-1/2B-2 entries) sit inside dated historical narrative but carry no
explicit temporal qualifier, unlike several other passages in the same
file; real SHA-256-repository coverage has existed for several slices
now. A separate, documentation-only correction pass is recommended for
this; it is not performed here.

All changes (`src/codeagent/worktree_lifecycle.py`, `src/codeagent/
lifecycle_store.py`, `tests/unit/test_worktree_lifecycle.py`,
`tests/unit/test_lifecycle_store.py`, `docs/adr/0004-owned-resource-
lifecycle-and-reconciliation.md`, `CLAUDE.md`, this entry) are left
unstaged and uncommitted for joint review, per instruction. Linux CI has
not run for any of this. The pre-existing untracked `uv.lock` was left
entirely untouched throughout — not edited, staged, or deleted.

## 2026-10-02 — Milestone 3 worktree-attribution substrate slice: correction pass

A targeted review found five issues in the substrate slice's first pass
(entry above), all independently confirmed against the actual code
before any fix was made. No change to the accepted `expected_head`
semantics or transition table; `workspace.py`, `controller.py`,
`checkpoint_session.py`, `reconciliation.py`, `StateRoot` behavior,
Docker behavior, and `docs/threat-model.md` remain untouched.

1. **`lifecycle_store.WorktreeAttribution`'s retirement was too
   aggressive.** It was a public, non-underscored class; a repository-
   wide grep finding only one internal reference cannot prove no
   external caller imports it. Restored as `WorktreeAttribution =
   WorktreeTransition` — an exact identity alias, never a second
   dataclass or a wrapper, so there is no duplicated validation to
   maintain. Construction through the alias is now deliberately
   **stricter** than the retired dataclass was: the old type had no
   `__post_init__`, so `WorktreeAttribution(intent=PRESENT,
   expected_head=None)` was previously constructible; the identical
   call now raises `ValueError`. New test:
   `test_worktree_attribution_is_exact_identity_alias_for_worktree_transition`
   (asserts `is` identity) and
   `test_worktree_attribution_construction_is_now_deliberately_stricter`.

2. **`WorktreePublicationError`'s docstring overclaimed.** "Raised only
   by `LifecycleWorktreePublisher.publish()`" is too strong for a
   public exception belonging to a structural Protocol — a test double
   or a future alternate `WorktreeTransitionPublisher` implementation
   may raise it directly. Rewritten to mirror
   `CheckpointPublicationError`/`OwnerStatePublicationError`'s own
   already-correct wording exactly: it is the public error a conforming
   publisher may raise; `LifecycleWorktreePublisher` is the sole
   current *production* translation boundary. No behavior change.

3. **A dangling reference to a nonexistent constant.**
   `_validate_worktree_edge()`'s `creating->absent` branch had a comment
   pointing at `_WORKTREE_TRANSITION_EDGES` — a frozenset that was
   written in an earlier draft, found to be unused dead code (this
   function is a pure conditional chain, unlike the container table),
   and removed, but the comment referencing it was accidentally left
   behind. Corrected to point at the function's own docstring and ADR
   0004 Amendment 10 instead. No redundant constant was added merely to
   satisfy the comment.

4. **"Real Git-derived SHAs" was an inaccurate evidence claim.**
   Confirmed by direct inspection: the test helpers `_sha`/`_sha256`
   construct object-format-shape-valid *synthetic* OIDs from repeated
   hex characters (`("1" * 40)[:40]`, etc.) — never from `git rev-parse`
   or a real repository. The claim appeared in `CLAUDE.md`'s and this
   file's own prior entries (both corrected in place, this session's
   own unstaged material, not a historical entry predating this
   session) and in ADR 0004 Amendment 10's own text. All three
   corrected to "object-format-valid synthetic SHA-1/SHA-256 OIDs." No
   new real-Git tests were added merely to preserve the old phrase —
   none of this slice's own claimed behavioral properties require one;
   this remains, accurately, not real `GitWorktree` integration or
   crash-ordering evidence.

5. **The field-renaming migration-cost rationale was imprecise.**
   Amendment 10's text had said keeping `expected_head` meant "there is
   no migration cost either way" — false: every existing schema-v1
   `lifecycle.json` already persists the `worktree` object with this
   exact key, in its `absent`/`null` shape, so renaming the *key* would
   require reader/writer compatibility handling or a schema-version
   decision, independent of whether any non-absent *value* has ever
   shipped. Corrected in the ADR to state the precise rationale: the
   key already exists in every v1 document; keeping it avoids
   unnecessary schema-compatibility work; no migration of non-absent
   values is needed only because those shapes were previously refused
   outright. `CLAUDE.md` and this file's own prior entries did not
   repeat this specific claim verbatim, so only the ADR required this
   fix.

Three more new narrow tests added for finding 6 (none expanding into
`workspace`/`controller` integration — together with the two already
named under finding 1 above, this pass adds exactly five new tests in
total, matching the 283->288 / 1,299->1,304 / 2,823->2,828 deltas below
exactly):
`test_worktree_publisher_identity_properties_match_cursor_projection`
(`lifecycle_id`/`state_root_id` equal the cursor's own projection
identity, including after an ordinary resource transition);
`test_worktree_publication_error_message_never_leaks_cause_detail_or_enum_spelling`
(the fixed production message contains neither the injected
`LifecycleStoreError`'s own detail text nor the categorical enum
member's spelling, in either case); and
`test_worktree_publisher_wrong_type_transition_translated_without_raw_leakage`
(five representative bad values — `None`, a string, a dict, an int, a
bare `object()` — each translates to `WorktreePublicationError`/
`ILLEGAL_TRANSITION`, never a raw `AttributeError`/`TypeError`).

The complete list of all five new tests this correction pass added:

1. `test_worktree_attribution_is_exact_identity_alias_for_worktree_transition`
2. `test_worktree_attribution_construction_is_now_deliberately_stricter`
3. `test_worktree_publisher_identity_properties_match_cursor_projection`
4. `test_worktree_publication_error_message_never_leaks_cause_detail_or_enum_spelling`
5. `test_worktree_publisher_wrong_type_transition_translated_without_raw_leakage`

Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`;
Docker was already running and was not started or restarted; no
administrator-access dialog appeared): `py_compile` on all four
changed files; `test_worktree_lifecycle.py` + `test_lifecycle_store.py`
together, 288 passed (up from 283); the sixteen-file focused
Milestone-3 set, 1,304 passed (up from 1,299), in both forward and
reverse file order; the complete suite, 2,828 passed (up from 2,823), 0
skipped, with `CODEAGENT_REQUIRE_DOCKER=1`; `git diff --check` clean; no
leftover `codeagent-*` containers, extra worktrees, `refs/codeagent`
refs, lingering processes, or temp/default state roots afterward.

No new file was added to the dirty set beyond what the first pass
already introduced; the pre-existing untracked `uv.lock` remains
exactly as it was — not edited, staged, or incorporated. All changes
remain unstaged and uncommitted for joint review.

## 2026-10-02 — Worktree-attribution substrate slice: Linux CI evidence reconciliation, and a SHA-256 documentation erratum

Slice (including its correction pass) was committed
(`f41da250bb7e67ea2b42113e17e25f3ec8143436`) and pushed to `main` as an
ordinary fast-forward (`0d956ff..f41da25`, no force). This is a
documentation-only pass reconciling `CLAUDE.md`'s and ADR 0004
Amendment 10's "not yet committed, pushed, or Linux-CI-confirmed" /
"Linux CI confirmation is pending ... nothing has been pushed"
placeholders against the real, independently re-verified run, plus a
separate, unrelated documentation erratum for two long-stale "SHA-256
object-format coverage is still pending" statements this pass happened
to also inspect. No production code, test, workflow, configuration,
dependency, `uv.lock`, or `docs/threat-model.md` status was changed.

### Linux CI evidence

GitHub Actions run [37060981509](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37060981509)
was independently re-fetched — fresh run metadata via
`gh api repos/.../actions/runs/37060981509`, a fresh per-job/per-step
breakdown via `gh api repos/.../actions/runs/37060981509/jobs`, and the
complete raw step logs via `gh run view --log`, all freshly re-queried
rather than reused from the prior finalization report — and every fact
below was verified directly against those fresh results.

Verified: `head_sha` exactly `f41da250bb7e67ea2b42113e17e25f3ec8143436`;
`conclusion` `success`; `status` `completed`; exactly one job ("Test
(ubuntu-24.04, Python 3.12)"), itself `success`, with exactly 13
API-reported steps, every one `success`. GitHub-hosted `ubuntu-24.04`
x86_64, Python 3.12, confirmed directly from the job name. Docker
preflight: Docker Engine - Community, version `28.0.4`, directly
present in the raw log; step succeeded. Pinned verification image
resolved as
`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
and, after `docker pull`, confirmed exactly `linux/amd64`. The
dedicated "Run real Docker verification tests (must execute, not
skip)" step ran exactly `python -m pytest tests/integration/
test_slice_c.py -v` with `CODEAGENT_REQUIRE_DOCKER=1` confirmed present
in that step's own logged environment, and the verbose output shows all
three of its own tests individually `PASSED` (no `SKIPPED` line
anywhere in that step), concluding `3 passed in 2.31s` — this is this
repository's own legacy Milestone-1 suite, exactly, and is explicitly
**not** specific evidence for this slice's own worktree-attribution
substrate, which adds no real-Docker test of its own. The separate "Run
complete test suite" step ran exactly `python -m pytest -q` with
`CODEAGENT_REQUIRE_DOCKER=1` also confirmed present in that step's own
logged environment, concluding `2825 passed, 3 skipped in 48.81s`.
`2825 + 3` equals the local macOS collected total of 2,828 reported in
this slice's own prior correction-pass entry above. This slice's new
worktree tests (`test_worktree_lifecycle.py`'s 27 tests and the
additions to `test_lifecycle_store.py`) executed through this step, not
through the dedicated Docker step. `pytest -q` prints no test
identities anywhere in this log, so these 3 skips are not identified or
guessed from the log — source inspection (not CI-log evidence)
separately continues to identify the same three Linux-unconditional,
platform/host-specific skips named in every earlier slice's own entry
as the likely candidates, labeled explicitly as inference, not
something this run's own log proves. The final leftover-container-
check step ran the anchored `grep -E
'^codeagent-(verify-|baseline-|verification-)'` pattern over a
complete, unfiltered `docker ps -a --format '{{.Names}}'` listing,
covering all three CodeAgent container families; its captured output
was empty, and the step succeeded.

`CLAUDE.md`'s worktree-attribution-substrate-slice bullet (previously
ending its opening sentence "...is implemented and locally verified on
macOS (2026-10-02); **not yet committed, pushed, or
Linux-CI-confirmed**") and `docs/adr/0004-owned-resource-lifecycle-and-
reconciliation.md`'s Amendment 10 evidence text (previously "Linux CI
confirmation is pending as of this commit; nothing has been pushed"
after its own evidence paragraph) are both updated in place with the
verified evidence above, following this project's established
convention (see, e.g., the 2026-09-29 Slice 3C-2 and 2026-10-02 Slice
3C-3 Linux CI evidence reconciliation entries above) of correcting a
now-stale present-tense status statement in these two living documents,
as distinct from `ENGINEERING_LOG.md` itself. No other prose in either
file — the accepted `expected_head` semantics, the persisted-shape
table, the legal owner-edge table, or the milestone/scope boundaries —
was touched.

`docs/threat-model.md` was inspected and contains no Amendment-10-
specific statement of any kind (stale or otherwise) to correct — the
one SHA-256 mention it contains (§5.x, an ADR 0006 end-to-end-run
reference) is unrelated to this slice or to object-format CI evidence.
It remains unchanged. No threat is claimed newly mitigated by this
pass: the worktree-attribution substrate remains unwired production-
wise, and T-E1/T-F2's existing status is unaffected.

### SHA-256 documentation erratum (unrelated finding, same pass)

Separately, this investigation independently verified the current
state of SHA-256 object-format test coverage, distinguishing three
categories before concluding anything: (1) real SHA-256-repository
tests — a genuine `git init --object-format=sha256` repository
exercised end to end; (2) schema/value-shape tests that merely pass
`object_format="sha256"` or construct 64-character synthetic OIDs
without any real repository; (3) tests that gracefully `pytest.skip()`
on a Git build lacking `--object-format=sha256` support (every real-
repository test in category 1 also falls into this category — the two
are not mutually exclusive).

Confirmed, by direct source inspection, that category-1 (real
repository) coverage genuinely exists today in:
`tests/unit/test_checkpoint_ref.py::test_sha256_repository_object_format`
(full `CheckpointRef` `create`/`advance`/`delete` compare-and-swap cycle
against a real sha256 repo), `tests/unit/test_git_safety.py::
test_detect_object_format_sha256` (real sha256-repository object-format
detection only), `tests/unit/test_repo_identity.py::
test_discover_repository_identity_sha256_or_skipped` (real sha256
repository, full identity discovery), `tests/unit/test_lifecycle_store.py::
test_prepare_lifecycle_sha256_or_skipped` (real sha256 repository, the
complete `prepare_lifecycle()` composition), and `tests/unit/
test_patch.py::test_apply_runs_end_to_end_in_a_sha256_repository` (real
sha256 repository, a full patch-apply end-to-end run) — each gated by a
graceful skip on an old Git build. This is genuinely distinct from
(and does not include) this slice's own new OID-shape tests
(`test_worktree_present_valid_sha256`,
`test_worktree_sha1_length_refused_against_sha256_repo`, and similar),
which are category-2 only: they pass `object_format="sha256"` directly
to the pure schema validator and construct a 64-character synthetic
OID, with no real repository involved at all, and were never cited as
real-repository evidence anywhere in this slice's own material.

`CLAUDE.md` carries two statements, both inside the dated "Milestone 2
Slice 2B-2 is implemented (2026-09-17)" historical narrative, saying
SHA-256 object-format coverage "is still pending" / "**Not yet done**:
SHA-256 object-format exercise" — true of that slice's own completion
date, but carrying no explicit temporal qualifier (unlike several other
passages in the same file that do mark themselves "at the time..."),
so each reads as a bare, misleadingly-current claim if skimmed in
isolation. Both are corrected in place, preserving the original
historical claim as accurate for its own date and adding an explicit,
clearly-labeled "status correction (2026-10-02, documentation-only)"
clause immediately after each, naming the exact five current test files
above and explicitly distinguishing them from this slice's own
synthetic-OID-only tests. The original historical sentences themselves
are not deleted or rewritten — only given the missing temporal
qualifier and a forward pointer to the current, real state.

A third occurrence of the identical claim exists in `ENGINEERING_LOG.md`
itself, inside its own historical "Milestone 2 slice 2B-2: checkpoint/
evidence integration implemented" entry (predating this session by many
slices). Per this project's own established convention, that historical
entry is **not** rewritten here — this paragraph serves as the erratum/
status note the convention calls for: as of 2026-10-02, real SHA-256-
repository coverage exists (the same five test files named above),
closing the gap that 2B-2 entry's own "still pending" statement
correctly described as true at the time it was written.

Verified: `git diff --check` clean on the resulting documentation-only
diff; only `CLAUDE.md`, the ADR, and this file changed — no production
code, test, workflow, configuration, or dependency file touched;
nothing staged. No test run or Docker session was needed for this
pass — the independently re-verified, completed CI run plus read-only
source/test inspection are the evidence. The pre-existing untracked
`uv.lock` remains exactly as it was throughout — not edited, staged, or
incorporated.

## 2026-10-02 — Milestone 3: optional `GitWorktree` worktree-transition publication (ADR 0004 Amendment 11)

An optional, **unwired** producer seam: `GitWorktree(...,
worktree_publisher=None)` publishes `creating`/`present`/`disposing`/
`absent` when given a publisher. No production composition path
supplies one; lifecycle-aware composition is forbidden until worktree
reconciliation/removal exist. Crashes currently block admission rather
than recover; the reservation/projection gap remains open; T-E1/T-F2
are not newly mitigated. Implementation/test evidence only, not a
security review.

Trade-offs decided:
- **Failed `present` always retains the worktree.** `DURABILITY_UNCONFIRMED`
  cannot tell whether `creating` or `present` is installed, so no
  physical cleanup depends on the reason. Mirrors the container
  precedent (`UNCONFIRMED_DEFERRED`).
- **Separate `lifecycle_error` latch** instead of overloading
  `cleanup_error`; re-raised as the same instance before every
  idempotent return; a body exception is never masked.
- **Identity binding includes `repo_key`** (part of the leaf path), so
  the Protocol gained a `repo_key` property — a contract change.
- **`observe_leaf()` only in publisher mode.** Scratchpad probes showed
  `Path.exists()` reports a dangling symlink as absent, APFS keeps
  `st_nlink == 2` on an unlinked directory's held fd, and fd numbers are
  reused after close — so absence is fd-relative `ENOENT` only, guarded
  by the closed flag. The no-publisher `Path.exists()` false-absence
  risk is unchanged and left for a separate slice.

Disclosed test corrections: Amendment 10's static
workspace-integration test narrowed (workspace may import the
`worktree_lifecycle` leaf, never `lifecycle_store`; controller/
reconciliation still forbidden); the Protocol-shape test now uses exact
equality as its docstring always claimed.

Verified (macOS, Docker already running, `CODEAGENT_REQUIRE_DOCKER=1`):
`test_workspace.py` 158 passed (92 existing unmodified + 66 new);
`test_state_root.py` 66; `test_worktree_lifecycle.py` +
`test_lifecycle_store.py` 288; `tests/integration/test_worktree_publication.py`
4 (incl. a real SHA-256 repository and two real SIGKILL →
`RECONCILIATION_BLOCKED` tests); 17-file focused set 1,380 passed,
forward and reverse; full suite 2,904 passed, 0 skipped (up from
2,828). No leftover containers, worktrees, `refs/codeagent` refs,
processes, or state roots; `git diff --check` clean. Linux CI pending
(not pushed). `uv.lock` untouched.

## 2026-10-02 — Amendment 11 (optional worktree transition publication): Linux CI evidence reconciliation

Documentation-only. The implementation entry above is kept as the
historical pre-push record. Evidence below was independently
re-fetched from the GitHub API and raw logs; no tests or Docker were
rerun for this pass.

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

`docs/threat-model.md` was inspected and has no Amendment-11-specific
stale statement (its one "Linux CI validation is pending" sentence
concerns ADR 0006's `patch.py` hardening), so it is unchanged; T-E1/
T-F2 are not newly mitigated. `uv.lock` untouched.

## 2026-10-02 — Milestone 3: reconciling a dead `creating` worktree with an empty reservation (ADR 0004 Amendment 12)

The first narrow worktree reconciliation row. A dead lifecycle whose
worktree record is `creating` and whose deterministic leaf is an empty,
private, unregistered directory with no Git admin entry is now removed by
automatic pre-run reconciliation and recorded `RECONCILED`. Everything else
about worktrees still blocks admission. Unwired to any CLI or controller
path; T-E1 unchanged; T-F2 partially addressed at the reconciler level only.

Trade-offs decided during planning (several review rounds):
- **Narrow row, not ADR-only or primitive-only.** A removal primitive with no
  consumer would prove nothing; the attribution evidence (record, locks,
  Git) lives in reconciliation.
- **Bounded Git admin scan.** A scratch probe showed `git worktree list`
  omits an admin directory whose `gitdir` file was never written, so the
  listing alone can't prove Git holds nothing for the path. The scan reads
  names only, is bounded (4,096 entries, 262,144 bytes), and refuses any
  `<lifecycle_id>[0-9]*` match.
- **Mode exactly 0700, and containers absent** both in the record and in a
  live listing.
- **Post-removal observation compared with the held descriptor.** A
  successful `rmdir` report never proves absence; the result distinguishes
  the original still present (`FAILED`) from a replacement (`REFUSED`).
  A held descriptor keeps its inode from being reused (probe-confirmed), but
  the same-user swap race before `rmdir` remains (A4 scope).
- **Post-mutation `REFUSED`/`SUBSTRATE_UNAVAILABLE`** follow the container
  reconciler's precedent; `leaf_outcome` in the trace records what actually
  happened.
- **Close-once descriptor handling.** After an unconfirmed close, descriptor
  numbers are never touched again (they may be reused); one latched error is
  re-raised, chained latched → `close_confirmed` error → body exception.
- **Trace stays `schema_version` 1**, with additive fields; no consumer
  exists.

Implementation notes:
- The shared removal primitive replaced `_WorktreeLeafReservation`'s inline
  sequence; a mapping keeps its messages identical. All existing reservation
  and workspace tests passed unmodified.
- `reconciliation.py` imports `WorktreeIntent` through `lifecycle_store`, so
  the Amendment 10/11 static test forbidding `worktree_lifecycle` there still
  holds unchanged. Its static ban on filesystem-removal calls also still
  holds: the `rmdir` lives in `state_root.py`.
- One existing integration test changed by design:
  `test_real_sigkill_after_durable_creating_blocks_admission` became
  `…_is_reconciled_by_next_admission`. Blocking admission was the honest
  outcome before this row existed; the kill-after-`present` test still
  asserts blocking.

Verified (macOS, Docker 29.8.0 already running, not started or restarted,
`CODEAGENT_REQUIRE_DOCKER=1`): `test_state_root.py` 116 (66 existing + 50);
`test_lifecycle_store.py` 275 (261 + 14); `test_reconciliation.py` 207
(138 + 69); `test_worktree_publication.py` 7 (4 + 3); the 17-file focused set
1,516 passed forward and reverse; full suite 3,040 passed, 0 skipped (up
from 2,904). No leftover containers, worktrees, `refs/codeagent` refs, Git
admin directories, processes, or default state root; `git diff --check`
clean. Linux CI pending (not pushed). `uv.lock` untouched (checksum
unchanged).

Same-day correction pass (before commit), two real defects in the admin scan
plus one documentation contradiction:
- A failed `_assert_cloexec` on either scan descriptor was converted to
  `INSPECTION_FAILED`, contrary to the accepted contract and losing the error
  from the cause chain when cleanup also failed. It now propagates; every
  opened descriptor is closed once via `_dominant_cleanup`, so the exact
  CLOEXEC error surfaces, or `CLEANUP_UNCONFIRMED` dominates chained from it.
- The lifecycle-id match ran before the entry-count and byte bounds, so a
  matching entry at the first over-limit position bypassed `LIMIT_EXCEEDED`.
  Bounds are now enforced before any name is interpreted.
- T-F2's residual-risk paragraph still said the threat stays open "until the
  mechanisms are implemented"; it now says why it stays open (the row is
  unwired; `present`/`disposing`/general orphan cleanup are unimplemented).
11 new regressions (4 CLOEXEC propagation, 1 entry-level, 6 bounds); a
temporary revert of the fixes made the bound-crossing and common-directory
CLOEXEC regressions fail, then the fixed file was restored byte-for-byte.
The totals above are the post-correction ones.
