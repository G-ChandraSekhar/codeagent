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
from codeagent.controller import EventLog, ModelClient, PlanProposal, RunConfig, RunController
from codeagent.errors import ErrorCode
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeModel,
    FakePatchApplier,
    FakeRepositoryReader,
    FakeVerifier,
    MarkerGatedFakeModel,
    SteppingClock,
)

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
    approval_mode: domain.ApprovalMode = domain.ApprovalMode.INTERACTIVE,
    model: ModelClient | None = None,
    reader: object | None = None,
) -> RunController:
    config = RunConfig(
        run_id=run_id,
        task_statement="fix retry bug",
        approval_mode=approval_mode,
        max_repair_iterations=max_repair_iterations,
        max_plan_revisions=max_plan_revisions,
    )
    return RunController(
        config,
        model if model is not None else FakeModel(PLAN),
        FakeApprovalProvider(approval_decisions),
        FakeVerifier(verification_outcomes, baseline_outcome=baseline_outcome),
        FakePatchApplier(patch_should_fail),
        reader if reader is not None else FakeRepositoryReader(),
        clock=SteppingClock(),
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
    assert checkpoint_created_events[0].parent_checkpoint_id is None
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
        clock=SteppingClock(),
    )

    finished = controller.run()

    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not None
    assert finished.error.code is ErrorCode.INTERNAL_INVARIANT_VIOLATION

    # The real (fake, but truthful) PatchApplied/CheckpointCreated
    # events still appear — they genuinely happened — but verification
    # is never reached: the run is stopped before spending further
    # budget on an out-of-scope change.
    assert any(isinstance(e, events.PatchApplied) for e in controller.log.events)
    assert not any(isinstance(e, events.VerificationCompleted) for e in controller.log.events)


def test_patch_reporting_a_subset_of_approved_files_is_accepted() -> None:
    """The check is issubset, not equality — a plan may approve several
    files while a given patch only touches one of them."""
    config = RunConfig(
        run_id="r-scope-subset",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
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
    the "proves evidence was passed" claim is vacuous."""
    gated_model = MarkerGatedFakeModel(
        read_path="jobs/worker.py", marker="MARKER_NOT_PRESENT", plan=PLAN
    )
    controller = _build(
        "r-evidence-gate-fails",
        model=gated_model,
        reader=FakeRepositoryReader(content="content without the marker"),
    )

    with pytest.raises(AssertionError):
        controller.run()


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
