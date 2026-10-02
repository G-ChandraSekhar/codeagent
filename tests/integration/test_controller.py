"""Milestone 1, slice A: integration tests for RunController.

Targeted at the correction-pass findings (index conflation, the
endless-revision-loop bug, iterations_used semantics, fixture
validation, audit-trace completeness, determinism, error_id reuse) —
not a Cartesian sweep. Each fixed behavior gets the smallest test that
actually distinguishes "fixed" from "still broken."
"""

from __future__ import annotations

import pytest

from codeagent import domain, events
from codeagent.checkpoint_ref import CheckpointRefError, CheckpointRefFailure, MutationOutcome
from codeagent.checkpoint_session import (
    CheckpointPublicationError,
    CheckpointPublicationFailure,
    CheckpointSessionError,
    CheckpointSessionFailure,
)
from codeagent.controller import (
    EventLog,
    ModelClient,
    PlanProposal,
    RunConfig,
    RunController,
    VerificationResult,
)
from codeagent.errors import ErrorCode, OperationalError
from codeagent.lifecycle_owner import OwnerStatePublicationError, OwnerStatePublicationFailure
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakeLifecycleOwnerPublisher,
    FakeModel,
    FakePatchApplier,
    FakeRepositoryReader,
    FakeVerifier,
    FakeWorkspace,
    MarkerGatedFakeModel,
    SteppingClock,
)

_LIFECYCLE_ID = "a" * 32

PLAN = PlanProposal(
    problem_hypothesis="idempotency key dropped on retry",
    proposed_file_paths=("jobs/worker.py",),
    verification_intent="python3 -B -m unittest tests.test_worker",
)


def _build(
    run_id: str,
    *,
    approval_decisions: tuple[domain.ApprovalDecision, ...] = (domain.ApprovalDecision.APPROVED,),
    verification_outcomes: tuple[events.VerificationOutcome, ...] = (
        events.VerificationOutcome.PASSED,
    ),
    baseline_outcome: events.VerificationOutcome = events.VerificationOutcome.TEST_FAILURE,
    max_repair_iterations: int = 3,
    max_plan_revisions: int = 2,
    patch_should_fail: bool = False,
    patch_failure_code: ErrorCode = ErrorCode.PATCH_VALIDATION_FAILED,
    approval_mode: domain.ApprovalMode = domain.ApprovalMode.INTERACTIVE,
    model: ModelClient | None = None,
    approval: object | None = None,
    verifier: object | None = None,
    patch_applier: object | None = None,
    reader: object | None = None,
    workspace: object | None = None,
    session: object | None = None,
    evidence_sink: object | None = None,
    lifecycle_owner: object | None = None,
) -> RunController:
    config = RunConfig(
        run_id=run_id,
        task_statement="fix retry bug",
        approval_mode=approval_mode,
        lifecycle_id=_LIFECYCLE_ID,
        max_repair_iterations=max_repair_iterations,
        max_plan_revisions=max_plan_revisions,
    )
    return RunController(
        config,
        model if model is not None else FakeModel(PLAN),
        approval if approval is not None else FakeApprovalProvider(approval_decisions),
        verifier
        if verifier is not None
        else FakeVerifier(verification_outcomes, baseline_outcome=baseline_outcome),
        patch_applier
        if patch_applier is not None
        else FakePatchApplier(patch_should_fail, failure_code=patch_failure_code),
        reader if reader is not None else FakeRepositoryReader(),
        workspace if workspace is not None else FakeWorkspace(),
        session if session is not None else FakeCheckpointSession(),
        evidence_sink if evidence_sink is not None else FakeEvidenceSink(),
        clock=SteppingClock(),
        lifecycle_owner=lifecycle_owner,
    )


def _assert_monotonic_sequence(controller: RunController) -> None:
    sequences = [e.sequence for e in controller.log.events]
    assert sequences == list(range(len(sequences)))


def _assert_no_stray_errors(controller: RunController) -> None:
    for event in controller.log.events:
        error = getattr(event, "error", None)
        if error is not None:
            raise AssertionError(f"unexpected error on {type(event).__name__}: {error!r}")


# --------------------------------------------------------------------
# EventLog: monotonic sequence enforcement in isolation
# --------------------------------------------------------------------


def test_event_log_rejects_out_of_order_sequence() -> None:
    log = EventLog()
    clock_event = events.PolicyDecisionRecorded(
        run_id="r",
        sequence=0,
        timestamp=SteppingClock().now(),
        state=domain.RunState.EXPLORE,
        iteration=0,
        tool=domain.ToolName.READ_FILE,
        tool_call_id="t",
        allowed=True,
        reason="ok",
    )
    log.append(clock_event)
    assert log.next_sequence() == 1
    with pytest.raises(AssertionError):
        log.append(clock_event)  # sequence 0 again


# --------------------------------------------------------------------
# Happy path (baseline correctness, and the iterations_used fix)
# --------------------------------------------------------------------


def test_happy_path_reports_iterations_used_as_a_count_not_an_index() -> None:
    """Finding #3: a successful first pass must report iterations_used
    == 1, not 0 (the previous bug reported the zero-based index)."""
    controller = _build("r-happy")
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    _assert_no_stray_errors(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert finished.iterations_used == 1
    assert finished.error is None


def test_repair_then_pass_reports_iterations_used_as_two() -> None:
    controller = _build(
        "r-repair",
        verification_outcomes=(
            events.VerificationOutcome.TEST_FAILURE,
            events.VerificationOutcome.PASSED,
        ),
    )
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    _assert_no_stray_errors(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert finished.iterations_used == 2  # two passes, not "1 repair"


def test_plan_rejected_never_reaches_execute_or_verify() -> None:
    controller = _build("r-rejected", approval_decisions=(domain.ApprovalDecision.REJECTED,))
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    _assert_no_stray_errors(controller)
    assert finished.terminal_reason is domain.TerminalReason.PLAN_REJECTED
    assert finished.iterations_used == 1
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.VerificationCompleted) for e in controller.log.events)


# --------------------------------------------------------------------
# Finding #1: plan revision must not consume repair budget or skip a
# verifier outcome.
# --------------------------------------------------------------------


def test_plan_revision_does_not_consume_repair_budget_or_skip_verification() -> None:
    controller = _build(
        "r-revision-independent",
        approval_decisions=(
            domain.ApprovalDecision.REVISION_REQUESTED,
            domain.ApprovalDecision.APPROVED,
        ),
        verification_outcomes=(events.VerificationOutcome.PASSED,),
        max_repair_iterations=0,  # would immediately budget-exceed if a
        # revision were wrongly counted as a repair
    )
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    _assert_no_stray_errors(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED

    verification_events = [
        e for e in controller.log.events if isinstance(e, events.VerificationCompleted)
    ]
    assert len(verification_events) == 1
    assert verification_events[0].outcome is events.VerificationOutcome.PASSED


# --------------------------------------------------------------------
# Finding #2: repeated REVISION_REQUESTED must terminate via
# BudgetExceeded(PLAN_REVISIONS), never loop forever.
# --------------------------------------------------------------------


def test_repeated_plan_revision_terminates_via_budget_exceeded() -> None:
    controller = _build(
        "r-endless-revision-guard",
        approval_decisions=(domain.ApprovalDecision.REVISION_REQUESTED,),  # repeats forever
        max_plan_revisions=2,
    )
    finished = controller.run()  # must return, not hang

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert finished.error is None
    _assert_no_stray_errors(controller)

    budget_events = [e for e in controller.log.events if isinstance(e, events.BudgetExceeded)]
    assert len(budget_events) == 1
    assert budget_events[0].kind is domain.BudgetKind.PLAN_REVISIONS
    assert budget_events[0].state is domain.RunState.EXPLORE
    assert budget_events[0].observed_value >= budget_events[0].limit_value
    # 2 allowed revisions + the initial pass = 3 EXPLORE passes before abort
    assert finished.iterations_used == 3
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)


# --------------------------------------------------------------------
# Finding #3 (boundary form): max_repair_iterations semantics.
# --------------------------------------------------------------------


def test_max_repair_iterations_zero_permits_initial_attempt_but_no_repair() -> None:
    controller = _build(
        "r-repair-boundary-zero",
        verification_outcomes=(events.VerificationOutcome.TEST_FAILURE,),
        max_repair_iterations=0,
    )
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert finished.iterations_used == 1
    verification_events = [
        e for e in controller.log.events if isinstance(e, events.VerificationCompleted)
    ]
    assert len(verification_events) == 1  # the initial attempt, no repair


def test_max_repair_iterations_two_permits_initial_attempt_plus_two_repairs() -> None:
    controller = _build(
        "r-repair-boundary-two",
        verification_outcomes=(events.VerificationOutcome.TEST_FAILURE,) * 3,
        max_repair_iterations=2,
    )
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert finished.iterations_used == 3  # initial + 2 repairs
    verification_events = [
        e for e in controller.log.events if isinstance(e, events.VerificationCompleted)
    ]
    assert len(verification_events) == 3
    assert all(
        e.outcome is events.VerificationOutcome.TEST_FAILURE for e in verification_events
    )


# --------------------------------------------------------------------
# Finding #5: fixture/config validation at construction.
# --------------------------------------------------------------------


def test_fake_approval_provider_rejects_empty_decisions() -> None:
    with pytest.raises(ValueError):
        FakeApprovalProvider(())


def test_fake_verifier_rejects_empty_outcomes() -> None:
    with pytest.raises(ValueError):
        FakeVerifier(())


def test_fake_verifier_rejects_unsupported_outcome() -> None:
    with pytest.raises(ValueError):
        FakeVerifier((events.VerificationOutcome.TIMEOUT,))


@pytest.mark.parametrize("field", ["max_repair_iterations", "max_plan_revisions"])
def test_run_config_rejects_negative_budget_limits(field: str) -> None:
    kwargs = dict(
        run_id="r",
        task_statement="x",
        approval_mode=domain.ApprovalMode.NONE,
        lifecycle_id=_LIFECYCLE_ID,
        max_repair_iterations=1,
        max_plan_revisions=1,
    )
    kwargs[field] = -1
    with pytest.raises(ValueError):
        RunConfig(**kwargs)


def test_fake_verifier_rejects_unsupported_baseline_outcome() -> None:
    """baseline_outcome moved from RunConfig to FakeVerifier in slice C
    — the fake still only fabricates PASSED/TEST_FAILURE; a real
    DockerVerifier is not restricted this way (see test_executor.py)."""
    with pytest.raises(ValueError):
        FakeVerifier(
            (events.VerificationOutcome.PASSED,),
            baseline_outcome=events.VerificationOutcome.TIMEOUT,
        )


# --------------------------------------------------------------------
# Finding #6: PolicyDecisionRecorded for every dispatched tool request,
# request -> policy -> completion ordering preserved.
# --------------------------------------------------------------------


def test_every_tool_dispatch_emits_policy_decision_between_request_and_completion() -> None:
    controller = _build("r-policy-order")
    controller.run()

    event_types = [type(e).__name__ for e in controller.log.events]
    tool_requested_indices = [i for i, t in enumerate(event_types) if t == "ToolRequested"]
    assert len(tool_requested_indices) == 3  # read_file, propose_plan, apply_patch
    for i in tool_requested_indices:
        assert event_types[i : i + 3] == ["ToolRequested", "PolicyDecisionRecorded", "ToolCompleted"]

    policy_events = [e for e in controller.log.events if isinstance(e, events.PolicyDecisionRecorded)]
    tool_requested_events = [e for e in controller.log.events if isinstance(e, events.ToolRequested)]
    assert [e.tool_call_id for e in policy_events] == [e.tool_call_id for e in tool_requested_events]
    assert all(e.allowed for e in policy_events)


# --------------------------------------------------------------------
# Finding #7: CheckpointCreated precedes/matches every PatchApplied;
# no dangling checkpoint references.
# --------------------------------------------------------------------


def test_every_patch_applied_has_a_preceding_checkpoint_created() -> None:
    controller = _build(
        "r-checkpoints",
        verification_outcomes=(
            events.VerificationOutcome.TEST_FAILURE,
            events.VerificationOutcome.PASSED,
        ),
    )
    controller.run()

    checkpoint_ids_seen: set[str] = set()
    for event in controller.log.events:
        if isinstance(event, events.CheckpointCreated):
            checkpoint_ids_seen.add(event.checkpoint_id)
        if isinstance(event, events.PatchApplied):
            # Must already exist by the time PatchApplied references it —
            # proves CheckpointCreated is emitted first, not just present
            # somewhere in the log.
            assert event.checkpoint_id in checkpoint_ids_seen

    patch_applied_events = [e for e in controller.log.events if isinstance(e, events.PatchApplied)]
    checkpoint_created_events = [
        e for e in controller.log.events if isinstance(e, events.CheckpointCreated)
    ]
    assert len(checkpoint_created_events) == len(patch_applied_events) == 2
    # Checkpoints chain: the second's parent is the first's id.
    # The first checkpoint's parent is always the workspace's pinned
    # initial commit now (ADR 0003 Amendment 2) — never None, since
    # every run has a workspace with a real initial_commit.
    assert checkpoint_created_events[0].parent_checkpoint_id == "a" * 40
    assert checkpoint_created_events[1].parent_checkpoint_id == checkpoint_created_events[0].checkpoint_id


# --------------------------------------------------------------------
# Finding #8: deterministic clock — identical traces across independent
# runs with a fresh SteppingClock, no reliance on wall-clock time.
# --------------------------------------------------------------------


def test_two_independent_runs_with_stepping_clock_produce_identical_timestamps() -> None:
    controller_a = _build("r-clock")
    controller_b = _build("r-clock")
    controller_a.run()
    controller_b.run()

    assert [e.timestamp for e in controller_a.log.events] == [
        e.timestamp for e in controller_b.log.events
    ]


# --------------------------------------------------------------------
# Finding #5 (author decision): error_id reversal — the same failure
# occurrence is referenced by both ToolCompleted and RunFinished.
# --------------------------------------------------------------------


def test_patch_failure_reuses_the_same_error_id_across_events() -> None:
    controller = _build("r-patch-fail", patch_should_fail=True)
    finished = controller.run()

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.PATCH_VALIDATION_FAILED

    failed_tool_completions = [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success
    ]
    assert len(failed_tool_completions) == 1
    tool_error = failed_tool_completions[0].error
    assert tool_error is not None

    # The whole point of the error_id reversal: one real occurrence,
    # referenced identically from both events.
    assert tool_error.error_id == finished.error.error_id
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.CheckpointCreated) for e in controller.log.events)
    assert not any(isinstance(e, events.VerificationCompleted) for e in controller.log.events)


@pytest.mark.parametrize(
    "failure_code",
    [
        ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE,
        ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE,
    ],
)
def test_patch_failure_propagates_the_identical_error_object_for_new_adr0006_codes(
    failure_code: ErrorCode,
) -> None:
    """ADR 0006's patch.py-hardening slice adds two new ErrorCodes. This
    proves the same object-identity/error_id-reuse guarantee already
    established for PATCH_VALIDATION_FAILED above also holds for both
    new codes: _dispatch_apply_patch's returned OperationalError is the
    exact same object forwarded into both ToolCompleted.error and (via
    _finish) RunFinished.error -- not merely an equal-by-value copy."""
    controller = _build(
        "r-patch-fail-adr0006", patch_should_fail=True, patch_failure_code=failure_code
    )
    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is failure_code

    failed_tool_completions = [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success
    ]
    assert len(failed_tool_completions) == 1
    tool_error = failed_tool_completions[0].error
    assert tool_error is not None
    assert tool_error.code is failure_code

    # Object identity, not just equal error_id: the controller must
    # forward the exact OperationalError PatchApplier.apply returned,
    # never reconstruct or copy it along the way.
    assert tool_error is finished.error
    assert tool_error.error_id == finished.error.error_id


# --------------------------------------------------------------------
# Milestone 3: a Docker-confirmed OOM kill (ErrorCode.EXECUTOR_OOM_KILLED)
# during verification is an operational failure like any other
# TIMEOUT/ENVIRONMENT_FAILURE outcome, not an ordinary TEST_FAILURE — it
# must abort the run outright rather than entering the repair loop, and
# must never surface as BudgetExceeded. FakeVerifier is deliberately
# restricted to PASSED/TEST_FAILURE (see fakes.py), so this uses a
# small test-local Verifier double instead of weakening that fake.
# --------------------------------------------------------------------


class _OomVerifier:
    """Test-local Verifier double: a real executor (e.g. DockerVerifier)
    is the only thing that can genuinely produce ENVIRONMENT_FAILURE
    with EXECUTOR_OOM_KILLED — this fabricates that exact shape without
    touching Docker, to prove the controller propagates it correctly
    end-to-end."""

    def __init__(self) -> None:
        self._command = ("python3", "-B", "-m", "unittest", "tests.test_worker")
        self.oom_error = OperationalError(
            code=ErrorCode.EXECUTOR_OOM_KILLED,
            error_id="oom-occurrence-1",
            message="verification container was killed for exceeding its memory limit",
        )

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def run_baseline(self) -> VerificationResult:
        return VerificationResult(
            outcome=events.VerificationOutcome.TEST_FAILURE,
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr="",
            cleanup_status=events.ContainerCleanupStatus.CONFIRMED_ABSENT,
        )

    def run(self, attempt_index: int) -> VerificationResult:
        return VerificationResult(
            outcome=events.VerificationOutcome.ENVIRONMENT_FAILURE,
            exit_code=137,
            duration_seconds=0.01,
            stdout="",
            stderr="",
            cleanup_status=events.ContainerCleanupStatus.CONFIRMED_ABSENT,
            error=self.oom_error,
        )


def test_confirmed_oom_kill_during_verification_aborts_the_run_without_repair_or_budget() -> None:
    config = RunConfig(
        run_id="r-oom",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id=_LIFECYCLE_ID,
        max_repair_iterations=3,
        max_plan_revisions=2,
    )
    verifier = _OomVerifier()
    controller = RunController(
        config,
        FakeModel(PLAN),
        FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verifier,
        FakePatchApplier(),
        FakeRepositoryReader(),
        FakeWorkspace(),
        FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
    )

    finished = controller.run()

    _assert_monotonic_sequence(controller)

    # Baseline TEST_FAILURE is an expected outcome, so the run proceeds
    # into exploration/plan/approve/patch/verify as normal...
    assert any(
        isinstance(e, events.BaselineRecorded)
        and e.outcome is events.VerificationOutcome.TEST_FAILURE
        for e in controller.log.events
    )

    # ...but the OOM-killed verification aborts the run outright.
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.EXECUTOR_OOM_KILLED

    verification_completions = [
        e for e in controller.log.events if isinstance(e, events.VerificationCompleted)
    ]
    assert len(verification_completions) == 1
    completed = verification_completions[0]
    assert completed.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert completed.error is not None
    assert completed.error.code is ErrorCode.EXECUTOR_OOM_KILLED

    # Same real failure occurrence, referenced identically from both
    # events -- the same discipline as the patch-failure error_id
    # reversal above.
    assert completed.error.error_id == finished.error.error_id == verifier.oom_error.error_id

    # Never treated as an ordinary repairable test failure: only one
    # verification attempt happened, and no BudgetExceeded was ever
    # emitted (repair budget was never consumed, let alone exhausted).
    assert not any(isinstance(e, events.BudgetExceeded) for e in controller.log.events)


# --------------------------------------------------------------------
# Plan-scope consistency: a patch reporting files outside what the
# approved plan declared must abort the run, not be treated as a
# legitimate step. See controller.py's _dispatch_apply_patch docstring
# for exactly where this check runs relative to the real mutation and
# why it can't run *before* the mutation in this slice's architecture.
# --------------------------------------------------------------------


def test_patch_reporting_files_outside_the_approved_plan_aborts_the_run() -> None:
    config = RunConfig(
        run_id="r-scope-violation",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id=_LIFECYCLE_ID,
    )
    # PLAN approves only "jobs/worker.py"; the patch applier (mis)reports
    # having changed a different file entirely.
    controller = RunController(
        config,
        FakeModel(PLAN),
        FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        FakeVerifier((events.VerificationOutcome.PASSED,)),
        FakePatchApplier(changed_paths=("unrelated_file.py",)),
        FakeRepositoryReader(),
        FakeWorkspace(),
        FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.INTERNAL_INVARIANT_VIOLATION

    # ADR 0003 Amendment 1 point 4: the approved-path postcondition now
    # runs *before* ToolCompleted(success=True)/CheckpointCreated/
    # PatchApplied are ever emitted, so none of them appear for a scope
    # violation — the transaction was never accepted.
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.CheckpointCreated) for e in controller.log.events)
    assert not any(isinstance(e, events.VerificationCompleted) for e in controller.log.events)
    failed_tool_completions = [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success
    ]
    assert len(failed_tool_completions) == 1
    assert failed_tool_completions[0].error is finished.error


def test_patch_reporting_a_subset_of_approved_files_is_accepted() -> None:
    """The check is issubset, not equality — a plan may approve several
    files while a given patch only touches one of them."""
    config = RunConfig(
        run_id="r-scope-subset",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id=_LIFECYCLE_ID,
    )
    plan_with_two_files = PlanProposal(
        problem_hypothesis=PLAN.problem_hypothesis,
        proposed_file_paths=("jobs/worker.py", "jobs/other.py"),
        verification_intent=PLAN.verification_intent,
    )
    controller = RunController(
        config,
        FakeModel(plan_with_two_files),
        FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        FakeVerifier((events.VerificationOutcome.PASSED,)),
        FakePatchApplier(changed_paths=("jobs/worker.py",)),
        FakeRepositoryReader(),
        FakeWorkspace(),
        FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert finished.error is None


# --------------------------------------------------------------------
# Milestone 1 completion: the implementation guide's exact requirement
# is "Using a deterministic fake model and a real fixture repository,
# perform a full read-plan-approve-patch-verify-report flow" — these
# tests target the read step specifically (fast, fake-repository
# versions of what tests/integration/test_slice_c.py demonstrates for
# real against a real worktree and real Docker).
# --------------------------------------------------------------------


def test_read_file_occurs_before_propose_plan() -> None:
    controller = _build("r-read-before-plan")
    controller.run()

    read_requested = next(
        e
        for e in controller.log.events
        if isinstance(e, events.ToolRequested) and e.tool is domain.ToolName.READ_FILE
    )
    read_completed = next(
        e
        for e in controller.log.events
        if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.READ_FILE
    )
    plan_requested = next(
        e
        for e in controller.log.events
        if isinstance(e, events.ToolRequested) and e.tool is domain.ToolName.PROPOSE_PLAN
    )
    plan_proposed = next(e for e in controller.log.events if isinstance(e, events.PlanProposed))

    assert read_requested.sequence < read_completed.sequence < plan_requested.sequence
    assert plan_requested.sequence < plan_proposed.sequence
    assert read_completed.success
    assert plan_proposed.evidence_refs == (read_requested.tool_call_id,)


def test_read_file_result_summary_never_contains_raw_content() -> None:
    """Only a bounded summary and the tool_call_id (already a distinct
    event field, i.e. the evidence reference) are persisted — never the
    file's actual content."""
    secret_content = "SECRET_MARKER_never_persisted"
    controller = _build(
        "r-read-no-leak", reader=FakeRepositoryReader(content=secret_content)
    )
    controller.run()

    read_completed = next(
        e
        for e in controller.log.events
        if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.READ_FILE
    )
    assert secret_content not in read_completed.result_summary
    for event in controller.log.events:
        for field_value in vars(event).values():
            if isinstance(field_value, str):
                assert secret_content not in field_value


def test_fake_model_derives_plan_only_after_seeing_real_read_evidence() -> None:
    """MarkerGatedFakeModel.propose_plan raises unless it was actually
    given the expected marker in real ReadResult content — proving the
    controller passes genuine evidence through rather than the model
    proposing its plan independently of the read it triggered."""
    marker = "BUG-MARKER-XYZ"
    gated_model = MarkerGatedFakeModel(
        read_path="jobs/worker.py", marker=marker, plan=PLAN
    )
    controller = _build(
        "r-evidence-gated",
        model=gated_model,
        reader=FakeRepositoryReader(content=f"...\n{marker}\n..."),
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert gated_model.last_read_result is not None
    assert gated_model.last_read_result.success
    assert marker in gated_model.last_read_result.content


def test_fake_model_gate_actually_fails_without_the_marker() -> None:
    """Sanity check for the test above: MarkerGatedFakeModel must
    actually be capable of failing when evidence is missing — otherwise
    the "proves evidence was passed" claim is vacuous.

    Updated by Milestone 3 Slice 3C-3 (ADR 0004 Amendment 9): before
    this slice, MarkerGatedFakeModel's internal AssertionError (an
    ordinary Exception) propagated out of `run()` raw, since nothing
    caught it. Now `run()`'s ordinary-exception terminalization
    boundary catches exactly this kind of unanticipated Exception and
    converts it into a terminalized UNCLASSIFIED_FAILURE RunFinished —
    this is the new boundary behaving correctly on a real
    exception-raising fixture, not a weakened assertion: the gate still
    provably fires (the run still never passes), it just now surfaces
    through the same sanitized terminal path every other unanticipated
    collaborator exception does, instead of escaping raw."""
    gated_model = MarkerGatedFakeModel(
        read_path="jobs/worker.py", marker="MARKER_NOT_PRESENT", plan=PLAN
    )
    controller = _build(
        "r-evidence-gate-fails",
        model=gated_model,
        reader=FakeRepositoryReader(content="content without the marker"),
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert finished.error.message == (
        "the run terminated due to an unanticipated internal error"
    )


def test_illegal_read_path_aborts_the_run_without_reading_host_content() -> None:
    """A read failure (bad path, symlink escape, oversized, non-UTF-8,
    etc.) is an operational failure: the run aborts as
    UNRECOVERABLE_ERROR without ever reaching PROPOSE_PLAN, PLAN, or
    APPROVAL, and without consuming any repair/revision budget."""
    controller = _build("r-read-fails", reader=FakeRepositoryReader(should_fail=True))

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.TOOL_INPUT_INVALID
    assert not any(isinstance(e, events.PlanProposed) for e in controller.log.events)
    assert not any(isinstance(e, events.ApprovalRecorded) for e in controller.log.events)
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)

    read_completed = next(
        e
        for e in controller.log.events
        if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.READ_FILE
    )
    assert not read_completed.success
    assert read_completed.error is not None


# --------------------------------------------------------------------
# Correction pass: an EvidenceSink that raises unexpectedly must never
# block workspace/ref cleanup or RunFinished (defect 2).
# --------------------------------------------------------------------


def test_raising_evidence_sink_does_not_block_later_teardown() -> None:
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink(raise_error=RuntimeError("sink exploded"))

    controller = _build(
        "r-evidence-raises", workspace=workspace, session=session, evidence_sink=evidence_sink
    )
    finished = controller.run()

    # The sink really was called (proving this is the path under test),
    # and RunFinished was still reached with a sanitized, categorized
    # failure rather than an uncaught RuntimeError propagating out of
    # controller.run() and never producing a terminal event at all.
    assert evidence_sink.capture_calls == 1
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert "sink exploded" not in finished.error.message

    # Later teardown genuinely executed: the workspace was disposed and
    # the (ABSENT, so no-op) session delete was still invoked.
    assert workspace.disposed is True
    assert workspace.preserved is False
    assert session.delete_calls == 1

    evidence_captured = next(
        e for e in controller.log.events if isinstance(e, events.EvidenceCaptured)
    )
    assert evidence_captured.success is False
    assert evidence_captured.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_raising_evidence_sink_with_unconfirmed_verifier_still_preserves_workspace() -> None:
    """Both failure classes at once: the evidence sink raises AND the
    verifier's cleanup is unconfirmed. Teardown must still reach
    preserve() and RunFinished."""
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink(raise_error=RuntimeError("sink exploded again"))

    controller = _build(
        "r-evidence-raises-preserve",
        verification_outcomes=(events.VerificationOutcome.PASSED,),
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )
    # Force the verifier-cleanup-unconfirmed flag the same way the real
    # controller sets it, without needing a real Docker-shaped fake.
    controller._any_verifier_cleanup_unconfirmed = True

    finished = controller.run()

    assert evidence_sink.capture_calls == 1
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert workspace.preserved is True
    assert workspace.disposed is False
    assert session.delete_calls == 0


# ---------------------------------------------------------------------------
# Milestone 3 Slice 3B-7 (ADR 0004 Amendment 7): controller handling of
# `CheckpointPublicationError` at both checkpoint call sites.
# ---------------------------------------------------------------------------


_EXPECTED_PUBLICATION_MESSAGE = "the checkpoint lifecycle projection transition could not be confirmed"


def test_checkpoint_publication_error_at_establish_is_caught_and_mapped() -> None:
    session = FakeCheckpointSession()
    session.establish_error = CheckpointPublicationError(
        CheckpointPublicationFailure.DURABILITY_UNCONFIRMED,
        "forced-injected-detail-that-must-not-leak",
    )
    workspace = FakeWorkspace()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-checkpoint-pub-establish", session=session, workspace=workspace, evidence_sink=evidence_sink
    )

    finished = controller.run()

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED
    assert finished.error.message == _EXPECTED_PUBLICATION_MESSAGE
    assert "forced-injected-detail-that-must-not-leak" not in finished.error.message
    assert "DURABILITY_UNCONFIRMED" not in finished.error.message

    failed_tool_completions = [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success
    ]
    assert len(failed_tool_completions) == 1
    tool_error = failed_tool_completions[0].error
    assert tool_error is not None
    assert tool_error.message == _EXPECTED_PUBLICATION_MESSAGE
    # Not merely matching error_id -- the identical OperationalError
    # object, exactly like every other checkpoint/patch failure.
    assert tool_error is finished.error
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.CheckpointCreated) for e in controller.log.events)

    # Ordinary teardown still runs to completion: evidence capture was
    # attempted, the workspace was disposed (not preserved -- this
    # failure never sets `_any_verifier_cleanup_unconfirmed`), and
    # checkpoint deletion was attempted (a no-op from ABSENT, since
    # establish() never succeeded).
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert workspace.preserved is False
    assert session.delete_calls == 1


def test_checkpoint_publication_error_at_advance_is_caught_and_mapped() -> None:
    """`advance()` runs unconditionally right after a successful
    `establish()` on the very first `apply_patch` (ADR 0003 Amendment 2:
    establish records the starting commit, advance immediately moves it
    to the first real patch's commit) — no repair iteration is needed to
    reach this call site."""
    session = FakeCheckpointSession()
    session.advance_error = CheckpointPublicationError(
        CheckpointPublicationFailure.STALE_EXPECTATION,
        "forced-injected-detail-that-must-not-leak",
    )
    workspace = FakeWorkspace()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-checkpoint-pub-advance", session=session, workspace=workspace, evidence_sink=evidence_sink
    )

    finished = controller.run()

    _assert_monotonic_sequence(controller)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED
    assert finished.error.message == _EXPECTED_PUBLICATION_MESSAGE
    assert "forced-injected-detail-that-must-not-leak" not in finished.error.message
    assert "STALE_EXPECTATION" not in finished.error.message

    failed_tool_completions = [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success
    ]
    assert len(failed_tool_completions) == 1
    tool_error = failed_tool_completions[0].error
    assert tool_error is not None
    assert tool_error.message == _EXPECTED_PUBLICATION_MESSAGE
    assert tool_error is finished.error
    # The patch itself succeeded, but the transaction is not accepted
    # until advance() also succeeds -- no success-shaped event for it.
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.CheckpointCreated) for e in controller.log.events)

    # Ordinary teardown still runs to completion: evidence capture was
    # attempted, the workspace was disposed, and checkpoint deletion was
    # attempted. Because establish() succeeded, the fake session remains
    # PRESENT; FakeCheckpointSession.delete() therefore executes rather
    # than taking its ABSENT no-op path.
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert workspace.preserved is False
    assert session.delete_calls == 1


def test_checkpoint_ref_error_mapping_remains_unchanged_at_establish() -> None:
    """Regression: this slice adds a new `elif` branch to
    `_map_checkpoint_error` — the existing `CheckpointRefError` mapping
    must be completely unaffected."""
    session = FakeCheckpointSession()
    session.establish_error = CheckpointRefError(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "forced", outcome=MutationOutcome.UNCHANGED
    )
    controller = _build("r-checkpoint-ref-establish", session=session)

    finished = controller.run()

    assert finished.error is not None
    assert finished.error.code is ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED


def test_checkpoint_session_error_mapping_remains_unchanged_at_establish() -> None:
    """Regression: `CheckpointSessionError` still falls through to the
    generic invariant-violation branch, unaffected by the new
    `CheckpointPublicationError` branch inserted before it."""
    session = FakeCheckpointSession()
    session.establish_error = CheckpointSessionError(
        CheckpointSessionFailure.INCOMPATIBLE_OPERATION, "forced"
    )
    controller = _build("r-checkpoint-session-establish", session=session)

    finished = controller.run()

    assert finished.error is not None
    assert finished.error.code is ErrorCode.INTERNAL_INVARIANT_VIOLATION


def test_delete_time_publication_error_still_maps_to_lifecycle_cleanup_unconfirmed() -> None:
    """`session.delete()` sits under `_terminate()`'s own broad
    `except Exception` teardown catch, already folding any delete-time
    failure into `LIFECYCLE_CLEANUP_UNCONFIRMED` -- this slice must not
    create a second, conflicting mapping for a `CheckpointPublicationError`
    raised from `delete()` specifically."""
    session = FakeCheckpointSession()
    session.delete_error = CheckpointPublicationError(CheckpointPublicationFailure.CLEANUP_UNCONFIRMED, "forced")
    workspace = FakeWorkspace()
    controller = _build("r-checkpoint-pub-delete", session=session, workspace=workspace)

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert session.delete_calls == 1


# ---------------------------------------------------------------------------
# Milestone 3 Slice 3C-1 (ADR 0004 Amendment 8): the optional
# `lifecycle_owner` collaborator and its exact ordering/precedence.
# ---------------------------------------------------------------------------


def test_omitted_lifecycle_owner_preserves_existing_behavior() -> None:
    """The default (no `lifecycle_owner`) must behave byte-for-byte as
    before this slice: no activate()/begin_cleanup()/complete() call is
    ever attempted, and the happy path is completely unaffected."""
    controller = _build("r-no-lifecycle-owner")
    assert controller._lifecycle_owner is None
    assert controller._lifecycle_owner_active is False

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert finished.error is None
    _assert_no_stray_errors(controller)


def test_activation_precedes_baseline_and_all_other_run_work() -> None:
    owner = FakeLifecycleOwnerPublisher()
    order: list[str] = []
    real_activate = owner.activate

    def _activate() -> None:
        order.append("activate")
        real_activate()

    owner.activate = _activate  # type: ignore[method-assign]

    original_run_baseline = FakeVerifier.run_baseline

    def _run_baseline(self):  # type: ignore[no-untyped-def]
        order.append("run_baseline")
        return original_run_baseline(self)

    try:
        FakeVerifier.run_baseline = _run_baseline  # type: ignore[assignment]
        controller = _build("r-activate-first", lifecycle_owner=owner)
        controller.run()
    finally:
        FakeVerifier.run_baseline = original_run_baseline  # type: ignore[assignment]

    assert order == ["activate", "run_baseline"]
    assert controller._lifecycle_owner_active is True


def test_activation_failure_performs_no_begin_cleanup_or_complete() -> None:
    owner = FakeLifecycleOwnerPublisher()
    owner.activate_error = OwnerStatePublicationError(
        OwnerStatePublicationFailure.NOT_INSTALLED, "forced-injected-detail-that-must-not-leak"
    )
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    controller = _build(
        "r-activate-fails", lifecycle_owner=owner, workspace=workspace, session=session
    )

    finished = controller.run()

    assert owner.calls == ["activate"]
    assert controller._lifecycle_owner_active is False
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED
    assert finished.error.message == "the run's lifecycle projection could not be confirmed active"
    assert "forced-injected-detail-that-must-not-leak" not in finished.error.message
    assert "NOT_INSTALLED" not in finished.error.message
    assert "not_installed" not in finished.error.message
    # Ordinary teardown still ran (evidence capture, dispose, delete) --
    # activation failing does not skip real resource cleanup.
    assert workspace.disposed is True
    assert session.delete_calls == 1
    # No PatchApplied/CheckpointCreated -- nothing but activate() was
    # ever attempted.
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.BaselineRecorded) for e in controller.log.events)


def test_begin_cleanup_precedes_resource_teardown() -> None:
    order: list[str] = []
    owner = FakeLifecycleOwnerPublisher()
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()

    real_begin_cleanup = owner.begin_cleanup
    real_dispose = workspace.dispose
    real_delete = session.delete

    def _begin_cleanup() -> None:
        order.append("begin_cleanup")
        real_begin_cleanup()

    def _dispose() -> None:
        order.append("dispose")
        real_dispose()

    def _delete() -> None:
        order.append("delete")
        real_delete()

    owner.begin_cleanup = _begin_cleanup  # type: ignore[method-assign]
    workspace.dispose = _dispose  # type: ignore[method-assign]
    session.delete = _delete  # type: ignore[method-assign]

    controller = _build(
        "r-begin-cleanup-order", lifecycle_owner=owner, workspace=workspace, session=session
    )
    controller.run()

    assert order == ["begin_cleanup", "dispose", "delete"]


def test_begin_cleanup_failure_does_not_stop_subsequent_cleanup() -> None:
    owner = FakeLifecycleOwnerPublisher()
    owner.begin_cleanup_error = OwnerStatePublicationError(
        OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED, "forced-injected-detail-that-must-not-leak"
    )
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-begin-cleanup-fails",
        lifecycle_owner=owner,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup"]  # complete() never attempted
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert finished.error.message == (
        "a verifier container, worktree, or checkpoint-ref cleanup step, "
        "or the run's lifecycle-projection bookkeeping, could not be confirmed"
    )
    assert "forced-injected-detail-that-must-not-leak" not in finished.error.message
    assert "DURABILITY_UNCONFIRMED" not in finished.error.message
    assert "durability_unconfirmed" not in finished.error.message


def test_complete_occurs_only_after_confirmed_resource_cleanup() -> None:
    """A confirmed worktree-disposal failure must prevent `complete()`
    from ever being attempted -- an unconfirmed owned resource means
    the projection genuinely cannot reach `COMPLETE`."""
    owner = FakeLifecycleOwnerPublisher()
    workspace = FakeWorkspace()
    workspace.dispose_error = RuntimeError("disposal failed")
    session = FakeCheckpointSession()
    controller = _build(
        "r-complete-gated-on-cleanup", lifecycle_owner=owner, workspace=workspace, session=session
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup"]
    assert "complete" not in owner.calls
    assert session.delete_calls == 0  # skipped: worktree disposal unconfirmed
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


def test_evidence_failure_does_not_block_complete() -> None:
    """Corrected rule (ADR 0004 Amendment 8): `lifecycle.json` is an
    operational resource-recovery projection, not the audit/evidence
    record. Evidence-capture failure must never prevent `complete()`
    from being attempted once `begin_cleanup()` and all owned-resource
    cleanup are confirmed."""
    owner = FakeLifecycleOwnerPublisher()
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink(raise_error=RuntimeError("sink exploded"))
    controller = _build(
        "r-evidence-fails-complete-ok",
        lifecycle_owner=owner,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup", "complete"]
    assert workspace.disposed is True
    assert session.delete_calls == 1
    # complete() genuinely succeeded -- the lifecycle projection reaches
    # COMPLETE -- while RunFinished still carries the evidence error, not
    # LIFECYCLE_CLEANUP_UNCONFIRMED. The two facts coexist and must never
    # be conflated.
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_evidence_failure_and_complete_failure_together_are_dominated_by_lifecycle_cleanup_unconfirmed() -> None:
    """Load-bearing combined-failure dominance test: when both an
    evidence-capture failure AND a `complete()` publication failure
    occur together, `LIFECYCLE_CLEANUP_UNCONFIRMED` must dominate the
    evidence error under the existing precedence -- the evidence
    failure is not lost, merely outranked."""
    owner = FakeLifecycleOwnerPublisher()
    owner.complete_error = OwnerStatePublicationError(
        OwnerStatePublicationFailure.ILLEGAL_TRANSITION, "forced"
    )
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink(raise_error=RuntimeError("sink exploded"))
    controller = _build(
        "r-evidence-and-complete-fail",
        lifecycle_owner=owner,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup", "complete"]
    assert evidence_sink.capture_calls == 1
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


def test_original_run_failure_combined_with_lifecycle_publication_failure_is_dominated() -> None:
    """An original, non-UNRECOVERABLE_ERROR terminal reason
    (`BUDGET_EXCEEDED`) occurring together with a terminal-path
    lifecycle-publication failure must still be overridden by
    `LIFECYCLE_CLEANUP_UNCONFIRMED`, exactly as it already is for
    verifier/worktree/ref cleanup failures today."""
    owner = FakeLifecycleOwnerPublisher()
    owner.complete_error = OwnerStatePublicationError(
        OwnerStatePublicationFailure.SUBSTRATE_UNAVAILABLE, "forced"
    )
    controller = _build(
        "r-budget-exceeded-and-lifecycle-fail",
        approval_decisions=(domain.ApprovalDecision.REVISION_REQUESTED,),
        max_plan_revisions=2,
        lifecycle_owner=owner,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup", "complete"]
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


@pytest.mark.parametrize(
    "reason",
    [OwnerStatePublicationFailure.NOT_INSTALLED, OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED],
)
def test_activate_failure_maps_identically_regardless_of_underlying_reason(
    reason: OwnerStatePublicationFailure,
) -> None:
    """The controller never differentiates by `OwnerStatePublicationFailure.
    reason` in `RunFinished` -- every reason folds into the same
    `LIFECYCLE_STATE_PUBLICATION_FAILED` code, whether the underlying
    write was never installed or installed-but-durability-unconfirmed."""
    owner = FakeLifecycleOwnerPublisher()
    owner.activate_error = OwnerStatePublicationError(reason, "forced")
    controller = _build("r-activate-reason-parity", lifecycle_owner=owner)

    finished = controller.run()

    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED


@pytest.mark.parametrize(
    "reason",
    [OwnerStatePublicationFailure.NOT_INSTALLED, OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED],
)
def test_complete_failure_maps_identically_regardless_of_underlying_reason(
    reason: OwnerStatePublicationFailure,
) -> None:
    owner = FakeLifecycleOwnerPublisher()
    owner.complete_error = OwnerStatePublicationError(reason, "forced")
    controller = _build("r-complete-reason-parity", lifecycle_owner=owner)

    finished = controller.run()

    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


def test_no_automatic_refresh_or_retry() -> None:
    """`RunController` never calls a collaborator method more than once
    per opportunity -- no automatic retry after any owner-state
    publication failure, and no `refresh()` call at all (the
    `LifecycleOwnerPublisher` Protocol has no `refresh()` method,
    structurally preventing the controller from ever calling it)."""
    owner = FakeLifecycleOwnerPublisher()
    controller = _build("r-no-retry", lifecycle_owner=owner)

    controller.run()

    # Each transition attempted at most once across the whole run.
    assert owner.calls.count("activate") == 1
    assert owner.calls.count("begin_cleanup") == 1
    assert owner.calls.count("complete") == 1


# --------------------------------------------------------------------
# Milestone 3 Slice 3C-3 (ADR 0004 Amendment 9): RunController
# ordinary-exception terminalization boundary.
#
# An unanticipated `Exception` escaping normal run execution after
# RunStarted/RUN_STARTED is routed exactly once through `_terminate()`,
# producing RunFinished(terminal_reason=UNRECOVERABLE_ERROR,
# error.code=ErrorCode.UNCLASSIFIED_FAILURE). BaseException subclasses
# that are not Exception (KeyboardInterrupt/SystemExit/GeneratorExit),
# signal/cancellation semantics (ADR 0005, still unimplemented), and a
# failure inside _terminate() itself are explicitly NOT covered.
# --------------------------------------------------------------------

_UNCLASSIFIED_MESSAGE = "the run terminated due to an unanticipated internal error"


class _UnexpectedCollaboratorFailure(Exception):
    """A plain, uncategorized exception type -- never any of this
    taxonomy's own typed errors -- standing in for a genuine,
    unanticipated bug in a collaborator."""


class _SentinelTeardownFailure(Exception):
    """A plain, uncategorized exception type used only to prove a
    failure escaping _terminate() itself is never swallowed, converted,
    or re-terminalized."""


_INJECTED_MARKER = "fixture-injected-unexpected-collaborator-failure"


def _raise_unexpected(*_args: object, **_kwargs: object) -> None:
    raise _UnexpectedCollaboratorFailure(_INJECTED_MARKER)


def _assert_exactly_one_terminal_pair(controller: RunController) -> None:
    finishes = [e for e in controller.log.events if isinstance(e, events.RunFinished)]
    assert len(finishes) == 1
    terminal_transitions = [
        e
        for e in controller.log.events
        if isinstance(e, events.StateTransitioned) and e.to_state is domain.RunState.DONE
    ]
    assert len(terminal_transitions) == 1


def _assert_no_marker_leak(controller: RunController) -> None:
    for event in controller.log.events:
        text = repr(event)
        assert _INJECTED_MARKER not in text
        assert "_UnexpectedCollaboratorFailure" not in text
        assert "RuntimeError" not in text


def _assert_unclassified_fallback(finished: events.RunFinished) -> None:
    """Asserts the plain fallback shape: no existing cleanup/evidence
    precedence override fired, so the terminal error is exactly
    UNCLASSIFIED_FAILURE with the fixed message. The precedence-override
    cases (evidence-capture failure, cleanup-unconfirmed) get their own
    tests below with fully explicit, exact assertions instead of a
    permissive "allow this other code too" parameter here -- a
    permissive helper could silently accept the wrong override."""
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert finished.error.message == _UNCLASSIFIED_MESSAGE


def test_unexpected_exception_from_lifecycle_owner_activate_is_terminalized() -> None:
    """Phase: lifecycle-owner activation, before normal baseline/model/
    tool execution begins. A non-OwnerStatePublicationError from
    activate() is not caught by the existing narrow handler and must
    reach the new fallback boundary instead."""
    owner = FakeLifecycleOwnerPublisher()
    owner.activate_error = _UnexpectedCollaboratorFailure(_INJECTED_MARKER)
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-unexpected-activate",
        lifecycle_owner=owner,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate"]
    assert controller._lifecycle_owner_active is False
    _assert_unclassified_fallback(finished)
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert not any(isinstance(e, events.BaselineRecorded) for e in controller.log.events)


def test_unexpected_exception_from_baseline_verifier_is_terminalized() -> None:
    """Phase: baseline verification."""
    verifier = FakeVerifier((events.VerificationOutcome.PASSED,))
    verifier.run_baseline = _raise_unexpected  # type: ignore[method-assign]
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-unexpected-baseline",
        verifier=verifier,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    _assert_unclassified_fallback(finished)
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert not any(isinstance(e, events.BaselineRecorded) for e in controller.log.events)


def test_unexpected_exception_from_model_propose_plan_is_terminalized() -> None:
    """Phase: explore/plan (the model's propose_plan call, after a
    successful read)."""
    model = FakeModel(PLAN)
    model.propose_plan = _raise_unexpected  # type: ignore[method-assign]
    controller = _build("r-unexpected-plan", model=model)

    finished = controller.run()

    _assert_unclassified_fallback(finished)
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)


def test_unexpected_exception_from_approval_provider_is_terminalized() -> None:
    """Phase: approval."""
    approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
    approval.decide = _raise_unexpected  # type: ignore[method-assign]
    controller = _build("r-unexpected-approval", approval=approval)

    finished = controller.run()

    _assert_unclassified_fallback(finished)
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert not any(isinstance(e, events.ApprovalRecorded) for e in controller.log.events)


def test_unexpected_exception_from_patch_applier_after_checkpoint_establishment_is_terminalized() -> (
    None
):
    """Phase: patch application, after the entry gate has passed and
    the checkpoint ref has already been established (intent now
    PRESENT) against the already-materialized worktree, but before
    advance() -- mid-patch with a real established checkpoint ref and
    an existing worktree, not "before any resource exists."""
    patch_applier = FakePatchApplier()
    patch_applier.apply = _raise_unexpected  # type: ignore[method-assign]
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    establish_calls: list[str] = []
    real_establish = session.establish

    def _establish(initial_sha: str) -> None:
        establish_calls.append(initial_sha)
        real_establish(initial_sha)

    session.establish = _establish  # type: ignore[method-assign]

    controller = _build(
        "r-unexpected-patch",
        patch_applier=patch_applier,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    # The checkpoint ref was genuinely established and the worktree
    # entry gate passed before the patch applier's injected failure.
    assert establish_calls == [workspace.initial_commit]
    assert workspace.entry_gate_calls == [workspace.initial_commit]
    _assert_unclassified_fallback(finished)
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    # Since the ref was established (intent moved off ABSENT), the
    # existing teardown order disposes the worktree and then deletes
    # the now-PRESENT checkpoint ref exactly once -- no new precedence
    # introduced by this slice.
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)


def test_unexpected_exception_from_verification_after_a_checkpoint_exists_is_terminalized() -> None:
    """Phase: verification (non-baseline), after the preceding patch
    application in this same loop pass has already established and
    advanced a real checkpoint."""
    verifier = FakeVerifier((events.VerificationOutcome.PASSED,))
    verifier.run = _raise_unexpected  # type: ignore[method-assign]
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-unexpected-verify",
        verifier=verifier,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    _assert_unclassified_fallback(finished)
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)


def test_unexpected_exception_after_activation_still_completes_lifecycle_owner_cleanup() -> None:
    """Activation succeeds (ACTIVE confirmed) and is followed by an
    unexpected ordinary Exception from the patch applier, after the
    checkpoint ref has already been established -- confirming that the
    fallback-terminalization path exercises the SAME owner-state
    sequence as every other terminal path: activate() first, then
    begin_cleanup() and (since every owned resource's cleanup is
    confirmed here -- no injected workspace/session/evidence failure)
    complete() last, each exactly once. This is distinct from
    `test_unexpected_exception_from_lifecycle_owner_activate_is_
    terminalized`, which keeps that test's own existing expectation
    that begin_cleanup()/complete() are never attempted when activate()
    itself is what failed (ACTIVE was never confirmed)."""
    owner = FakeLifecycleOwnerPublisher()
    patch_applier = FakePatchApplier()
    patch_applier.apply = _raise_unexpected  # type: ignore[method-assign]
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-unexpected-patch-with-owner",
        lifecycle_owner=owner,
        patch_applier=patch_applier,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup", "complete"]
    assert controller._lifecycle_owner_active is True
    _assert_unclassified_fallback(finished)
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)


def test_evidence_capture_failure_overrides_unclassified_failure_on_fallback_path() -> None:
    """Precedence test for the new fallback path specifically: an
    unexpected collaborator Exception triggers fallback termination,
    and FakeEvidenceSink.capture raises an ordinary Exception through
    its existing `raise_error` mechanism. `_capture_evidence()`'s
    existing internal catch converts that into the existing
    EVIDENCE_CAPTURE_FAILED receipt, which -- per `_terminate()`'s
    existing, unchanged precedence -- overrides the fallback's own
    UNCLASSIFIED_FAILURE. No new precedence rule is introduced by this
    slice; this proves the existing rule still applies correctly when
    the triggering failure is the new fallback path rather than one of
    the controller's own typed-result returns."""
    model = FakeModel(PLAN)
    model.propose_plan = _raise_unexpected  # type: ignore[method-assign]
    evidence_sink = FakeEvidenceSink(raise_error=RuntimeError("sink-boom-should-never-leak"))
    workspace = FakeWorkspace()
    session = FakeCheckpointSession()
    controller = _build(
        "r-fallback-evidence-failure",
        model=model,
        evidence_sink=evidence_sink,
        workspace=workspace,
        session=session,
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert finished.error.message == "the evidence capture step failed unexpectedly (sink-raised)"
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is True
    assert session.delete_calls == 1
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert "sink-boom-should-never-leak" not in finished.error.message
    for event in controller.log.events:
        assert "sink-boom-should-never-leak" not in repr(event)


def test_workspace_dispose_failure_overrides_unclassified_failure_on_fallback_path() -> None:
    """Precedence test for the new fallback path specifically: an
    unexpected collaborator Exception triggers fallback termination,
    and FakeWorkspace.dispose then raises. Per `_terminate()`'s
    existing, unchanged precedence, the final error becomes
    LIFECYCLE_CLEANUP_UNCONFIRMED (overriding the fallback's own
    UNCLASSIFIED_FAILURE); checkpoint-ref deletion is skipped (an
    unconfirmed worktree may still reference it); a lifecycle owner's
    begin_cleanup() is still attempted, but complete() is skipped
    (cleanup wasn't confirmed); evidence capture is still attempted
    exactly once regardless."""
    owner = FakeLifecycleOwnerPublisher()
    model = FakeModel(PLAN)
    model.propose_plan = _raise_unexpected  # type: ignore[method-assign]
    workspace = FakeWorkspace()
    workspace.dispose_error = RuntimeError("dispose-boom-should-never-leak")
    session = FakeCheckpointSession()
    evidence_sink = FakeEvidenceSink()
    controller = _build(
        "r-fallback-dispose-failure",
        lifecycle_owner=owner,
        model=model,
        workspace=workspace,
        session=session,
        evidence_sink=evidence_sink,
    )

    finished = controller.run()

    assert owner.calls == ["activate", "begin_cleanup"]  # complete() skipped
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert finished.error.message == (
        "a verifier container, worktree, or checkpoint-ref cleanup step, "
        "or the run's lifecycle-projection bookkeeping, could not be confirmed"
    )
    assert evidence_sink.capture_calls == 1
    assert workspace.disposed is False
    assert session.delete_calls == 0  # skipped: disposal unconfirmed
    _assert_exactly_one_terminal_pair(controller)
    _assert_no_marker_leak(controller)
    assert "dispose-boom-should-never-leak" not in finished.error.message
    for event in controller.log.events:
        assert "dispose-boom-should-never-leak" not in repr(event)


@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_base_exception_not_an_exception_propagates_unconverted(exc_cls: type[BaseException]) -> None:
    """KeyboardInterrupt/SystemExit do not inherit from Exception, so
    `except Exception:` cannot intercept them by construction -- no
    special-case code exists or is needed. ADR 0005 (accepted,
    unimplemented) remains the sole owner of cancellation semantics;
    this slice makes no claim about them."""
    model = FakeModel(PLAN)

    def _raise_base(*_a: object, **_kw: object) -> None:
        raise exc_cls()

    model.propose_plan = _raise_base  # type: ignore[method-assign]
    controller = _build("r-base-exception", model=model)

    with pytest.raises(exc_cls):
        controller.run()

    assert not any(isinstance(e, events.RunFinished) for e in controller.log.events)


def test_second_terminate_invocation_is_refused_before_any_additional_cleanup() -> None:
    """A genuine one-shot guard: the check-and-raise at the top of
    `_terminate()` refuses a second call before any additional evidence
    capture, resource cleanup, lifecycle publication, transition, or
    event emission -- not merely before returning."""
    controller = _build("r-double-terminate")
    controller._terminate(
        0,
        domain.Trigger.UNRECOVERABLE_ERROR,
        error=OperationalError(
            code=ErrorCode.UNCLASSIFIED_FAILURE,
            error_id="r-double-terminate-first",
            message=_UNCLASSIFIED_MESSAGE,
        ),
    )

    evidence_sink = controller._evidence_sink
    workspace = controller._workspace
    session = controller._session
    calls_before = (evidence_sink.capture_calls, workspace.disposed, session.delete_calls)
    finishes_before = sum(1 for e in controller.log.events if isinstance(e, events.RunFinished))

    with pytest.raises(RuntimeError, match="run termination has already started"):
        controller._terminate(
            0,
            domain.Trigger.UNRECOVERABLE_ERROR,
            error=OperationalError(
                code=ErrorCode.UNCLASSIFIED_FAILURE,
                error_id="r-double-terminate-second",
                message=_UNCLASSIFIED_MESSAGE,
            ),
        )

    calls_after = (evidence_sink.capture_calls, workspace.disposed, session.delete_calls)
    finishes_after = sum(1 for e in controller.log.events if isinstance(e, events.RunFinished))
    assert calls_after == calls_before
    assert finishes_after == finishes_before == 1


def test_direct_terminate_teardown_failure_propagates_without_reentry() -> None:
    """A failure arising inside `_terminate()` itself (simulated here by
    replacing `_capture_evidence` entirely, since the real method
    already catches and sanitizes every sink/event-construction failure
    internally -- the only way to simulate a genuine bug in
    `_terminate()`'s own teardown logic is to bypass that internal catch
    completely) propagates unchanged: no RunFinished, no re-entry."""
    controller = _build("r-teardown-sentinel-direct")

    def _raise_sentinel(_pass_index: int) -> None:
        raise _SentinelTeardownFailure("sentinel-teardown-failure")

    controller._capture_evidence = _raise_sentinel  # type: ignore[method-assign]

    with pytest.raises(_SentinelTeardownFailure):
        controller._terminate(
            0,
            domain.Trigger.UNRECOVERABLE_ERROR,
            error=OperationalError(
                code=ErrorCode.UNCLASSIFIED_FAILURE,
                error_id="r-teardown-sentinel-direct",
                message=_UNCLASSIFIED_MESSAGE,
            ),
        )

    assert controller._termination_started is True
    assert not any(isinstance(e, events.RunFinished) for e in controller.log.events)
    assert controller._workspace.disposed is False
    assert controller._session.delete_calls == 0


def test_fallback_terminate_teardown_failure_has_no_chained_collaborator_context() -> None:
    """Proves the chaining fix directly: when an unexpected collaborator
    exception triggers run()'s fallback _terminate() call, and that
    fallback call's own teardown then fails (again via a replaced
    `_capture_evidence`), the escaping teardown failure must not carry
    the discarded collaborator exception as __context__/__cause__, and
    the collaborator exception's own text/type must not appear anywhere
    in the escaping exception or in any already-emitted event.
    `_terminate()`'s entry count is measured directly with a call-count
    spy, not inferred from side effects, and escaping-instance identity
    is checked explicitly against the exact sentinel object raised."""
    patch_applier = FakePatchApplier()

    def _raise_collaborator(*_a: object, **_kw: object) -> None:
        raise RuntimeError("boom-collaborator-should-never-be-chained")

    patch_applier.apply = _raise_collaborator  # type: ignore[method-assign]
    controller = _build("r-teardown-sentinel-fallback", patch_applier=patch_applier)

    created_sentinels: list[_SentinelTeardownFailure] = []

    def _raise_sentinel(_pass_index: int) -> None:
        sentinel = _SentinelTeardownFailure("sentinel-teardown-failure")
        created_sentinels.append(sentinel)
        raise sentinel

    controller._capture_evidence = _raise_sentinel  # type: ignore[method-assign]

    terminate_call_count = {"n": 0}
    original_terminate = controller._terminate

    def _counting_terminate(*args: object, **kwargs: object) -> events.RunFinished:
        terminate_call_count["n"] += 1
        return original_terminate(*args, **kwargs)  # type: ignore[arg-type]

    controller._terminate = _counting_terminate  # type: ignore[method-assign]

    with pytest.raises(_SentinelTeardownFailure) as excinfo:
        controller.run()

    escaped = excinfo.value
    assert len(created_sentinels) == 1
    assert escaped is created_sentinels[0]
    assert escaped.__context__ is None
    assert escaped.__cause__ is None
    assert terminate_call_count["n"] == 1
    assert "boom-collaborator-should-never-be-chained" not in str(escaped)
    assert controller._termination_started is True
    assert not any(isinstance(e, events.RunFinished) for e in controller.log.events)
    assert controller._workspace.disposed is False
    assert controller._session.delete_calls == 0
    for event in controller.log.events:
        text = repr(event)
        assert "boom-collaborator-should-never-be-chained" not in text
        assert "RuntimeError" not in text
