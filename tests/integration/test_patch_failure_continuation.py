"""Ledger D5-A (ADR 0007 Amendment 1): continuation after a pre-write patch
validation failure.

A stale `expected_text` (`APPLY` / `PATCH_VALIDATION_FAILED` on a `PRESENT`
session, T1) returns the run to EXPLORE with never-dropped `PATCH_FAILED`
feedback. Every other mapped failure (T2-T7) still ends the run, Gate zero
ends it first when an earlier verifier cleanup was unconfirmed, and no
recreation is ever attempted. The controller tests use fakes; one real-Git
test and one real-Docker acceptance test exercise the real substrate.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from codeagent import _lifecycle_run as lr
from codeagent import controller as ctl
from codeagent import domain, events
from codeagent.cancellation import CancellationSource
from codeagent.checkpoint_ref import CheckpointRef, CheckpointRefError, CheckpointRefFailure, MutationOutcome, new_lifecycle_id
from codeagent.checkpoint_session import CheckpointIntent, CheckpointSession
from codeagent.controller import EventLog, PatchResult, RunConfig, RunController, SystemClock
from codeagent.errors import ErrorCode, OperationalError
from codeagent.evidence import FilesystemEvidenceSink
from codeagent.model_protocol import (
    PATCH_FAILED_ITEM_MAX_BYTES,
    PATCH_FAILED_MESSAGE_MAX_BYTES,
    VERIFICATION_ITEM_MAX_BYTES,
    FeedbackItem,
    FeedbackKind,
)
from codeagent.patch import GitPatchApplier
from codeagent.repo_tools import RepoTools
from codeagent.tool_schema import ProposedChange, ProposePlanArgs
from codeagent.workspace import GitWorktree
from tests.support.fakes import (
    FIXTURE_VERIFY_COMMAND,
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakeModel,
    FakePatchApplier,
    FakeRepositoryTools,
    FakeVerifier,
    FakeWorkspace,
    SteppingClock,
    make_plan,
)
from tests.support.fixture_repo import real_fixture_repo
from tests.support.scripted_model import propose_plan_call, read_file_call, turn
from tests.support.trace_assertions import assert_model_pairing, assert_tool_pairing

_LID = "a" * 32
_H0 = "a" * 40
STALE = make_plan(expected_text="    job.retry_count += 2\n", new_text="    pass\n")
FIXED = make_plan()
V = ErrorCode.PATCH_VALIDATION_FAILED


# --------------------------------------------------------------------------
# Fakes and helpers
# --------------------------------------------------------------------------


class SequencedApplier(FakePatchApplier):
    """Returns one failure per queued code, then succeeds."""

    def __init__(self, codes=(V,), changed_paths=("jobs/worker.py",)) -> None:
        super().__init__(changed_paths=changed_paths)
        self._codes = list(codes)

    def apply(self, run_id, iteration, approved_paths, operations):
        if not self._codes:
            return super().apply(run_id, iteration, approved_paths, operations)
        self.operations.append(operations)
        self._n += 1
        return PatchResult(
            success=False,
            operation_count=0,
            changed_paths=(),
            diff_bytes=0,
            error=OperationalError(
                code=self._codes.pop(0), error_id=f"{run_id}-seq-{self._n}", message="fixture-forced patch failure"
            ),
        )


class PlanPerPass(FakeModel):
    """FakeModel proposing `plans[pass]` (the last one repeats)."""

    def __init__(self, *plans: ProposePlanArgs) -> None:
        super().__init__(plans[0])
        self._plans = plans

    def respond(self, context):
        self._plan = self._plans[min(context.pass_index, len(self._plans) - 1)]
        return super().respond(context)


class FakeRecreator:
    def __init__(self) -> None:
        self.begin_calls = 0
        self.recreate_calls = 0

    def begin(self, *, trigger_code, checkpoint_id):
        self.begin_calls += 1
        return 1

    def recreate(self, sequence):
        self.recreate_calls += 1
        raise AssertionError("D5-A never recreates")


def _build(
    *,
    model=None,
    applier=None,
    verifier=None,
    approval=None,
    workspace=None,
    session=None,
    tools=None,
    recreator=None,
    max_repair_iterations: int = 3,
    run_id: str = "r-d5a",
) -> RunController:
    config = RunConfig(
        run_id=run_id,
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id=_LID,
        max_repair_iterations=max_repair_iterations,
    )
    return RunController(
        config,
        model if model is not None else PlanPerPass(STALE, FIXED),
        approval if approval is not None else FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verifier if verifier is not None else FakeVerifier((events.VerificationOutcome.PASSED,)),
        applier if applier is not None else SequencedApplier(),
        tools if tools is not None else FakeRepositoryTools(),
        workspace if workspace is not None else FakeWorkspace(),
        session if session is not None else FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
        workspace_recreator=recreator,
    )


def _one(items):
    """Exactly one element, by explicit assertion (the strict mutation rule
    counts only assertion failures)."""
    items = list(items)
    assert len(items) == 1, f"expected exactly one, got {len(items)}"
    return items[0]


def _of(controller, cls):
    return [e for e in controller.log.events if isinstance(e, cls)]


def _sites(controller) -> list[str]:
    return [site for site, _ in controller._cancellation_sites]


def _patch_failed_transitions(controller):
    return [e for e in _of(controller, events.StateTransitioned) if e.trigger is domain.Trigger.PATCH_FAILED]


def _feedback(controller, kind=FeedbackKind.PATCH_FAILED):
    return [item for item in controller._transcript if item.kind is kind]


def _failed_patches(controller):
    return [e for e in _of(controller, events.ToolCompleted) if e.tool is domain.ToolName.APPLY_PATCH and not e.success]


def _arm(controller, site: str, action) -> None:
    """Run `action` immediately before the safe point `site` is checked."""
    real = controller._cancellation_check

    def check(s):
        if s == site:
            action()
        real(s)

    controller._cancellation_check = check


def _cancel_at(controller, site: str) -> None:
    source = CancellationSource()
    controller._cancellation = source.token
    _arm(controller, site, lambda: source.request("test"))


def _deadline_at(controller, site: str) -> None:
    """Move the (stepping) clock past the wall-clock budget just before `site`."""

    def jump():
        controller._clock._next_monotonic += controller._c.wall_clock_budget_seconds + 1

    _arm(controller, site, jump)


def _session_present() -> FakeCheckpointSession:
    session = FakeCheckpointSession()
    session.intent = CheckpointIntent.PRESENT
    session.accepted_sha = _H0
    return session


def _gate_failure() -> dict:
    workspace = FakeWorkspace(initial_commit=_H0)
    workspace.entry_gate_error = RuntimeError("dirty")
    return dict(applier=FakePatchApplier(), workspace=workspace, session=_session_present())


def _advance_unchanged() -> dict:
    session = FakeCheckpointSession()
    session.advance_error = CheckpointRefError(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "forced", outcome=MutationOutcome.UNCHANGED
    )
    return dict(applier=FakePatchApplier(), session=session)


def _establish_failure() -> dict:
    session = FakeCheckpointSession()
    session.establish_error = CheckpointRefError(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "forced", outcome=MutationOutcome.UNCHANGED
    )
    return dict(applier=FakePatchApplier(), session=session)


# Every returned, mapped failure (T1-T7) and its terminal error code in D5-A.
MAPPED = {
    "t1": (lambda: dict(applier=SequencedApplier((V,))), V),
    "t2-substrate": (
        lambda: dict(applier=SequencedApplier((ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE,))),
        ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE,
    ),
    "t2-objects": (
        lambda: dict(applier=SequencedApplier((ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE,))),
        ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE,
    ),
    "t3": (_gate_failure, ErrorCode.WORKSPACE_ENTRY_GATE_FAILED),
    "t4": (lambda: dict(applier=SequencedApplier((ErrorCode.PATCH_APPLICATION_FAILED,))), ErrorCode.PATCH_APPLICATION_FAILED),
    "t5": (_advance_unchanged, ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED),
    "t6": (_establish_failure, ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED),
    "t7": (lambda: dict(applier=FakePatchApplier(changed_paths=("unrelated_file.py",))), ErrorCode.INTERNAL_INVARIANT_VIOLATION),
}


def _assert_terminated_by(controller, finished, code) -> None:
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is code
    assert finished.error is _failed_patches(controller)[-1].error
    assert not _patch_failed_transitions(controller) and not _feedback(controller)
    assert "C28" not in _sites(controller)


# --------------------------------------------------------------------------
# T1: continuation, ordering and the event shape
# --------------------------------------------------------------------------


def test_t1_continues_to_a_revised_plan_and_passes() -> None:
    controller = _build()
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
    failed = _one(_failed_patches(controller))
    assert failed.error.code is V and failed.state is domain.RunState.EXECUTE
    transition = _one(_patch_failed_transitions(controller))
    assert (transition.from_state, transition.to_state) == (domain.RunState.EXECUTE, domain.RunState.EXPLORE)
    assert transition.iteration == 0 and transition.terminal_reason is None
    assert finished.iterations_used == 2
    assert len(_of(controller, events.PatchApplied)) == 1
    assert len(_of(controller, events.VerificationCompleted)) == 1
    assert_model_pairing(controller.log.events)
    assert_tool_pairing(controller.log.events)


def test_t1_event_order_from_failure_to_the_next_pass() -> None:
    controller = _build()
    controller.run()
    trace = controller.log.events
    failed = _failed_patches(controller)[0]
    transition = _one(_patch_failed_transitions(controller))
    between = trace[failed.sequence + 1 : transition.sequence]
    assert between == [], "nothing is emitted between the failed ToolCompleted and the transition"
    after = trace[transition.sequence + 1]
    assert isinstance(after, events.ModelRequestStarted) and after.iteration == 1


def test_t1_feedback_item_appended() -> None:
    controller = _build()
    controller.run()
    item = _one(_feedback(controller))
    assert item.pass_index == 0


def test_t1_feedback_body_exact() -> None:
    controller = _build()
    controller.run()
    item = _one(_feedback(controller))
    proposed = _of(controller, events.PlanProposed)[0]
    assert item.body == {
        "change_digest": proposed.change_digest,
        "error_code": "patch_validation_failed",
        "message": "fixture-forced patch failure",
        "plan_revision": proposed.plan_revision,
        "previous_attempt_failed": True,
        "untrusted": True,
        "worktree_recreated": False,
    }


def test_t1_feedback_reaches_the_next_pass_model_context() -> None:
    model = PlanPerPass(STALE, FIXED)
    controller = _build(model=model)
    controller.run()
    pass_one = [c for c in model.contexts if c.pass_index == 1]
    assert pass_one and any(i.kind is FeedbackKind.PATCH_FAILED for i in pass_one[0].transcript)
    assert all(i.kind is not FeedbackKind.PATCH_FAILED for c in model.contexts if c.pass_index == 0 for i in c.transcript)


def test_t1_feedback_message_is_truncated_on_a_utf8_boundary() -> None:
    class LongMessage(SequencedApplier):
        def apply(self, run_id, iteration, approved_paths, operations):
            result = super().apply(run_id, iteration, approved_paths, operations)
            if result.success:
                return result
            return dataclasses.replace(
                result, error=dataclasses.replace(result.error, message="é" * 400 + "\x07tail")
            )

    controller = _build(applier=LongMessage())
    controller.run()
    item = _one(_feedback(controller))
    message = item.body["message"]
    encoded = message.encode("utf-8")
    assert len(encoded) == PATCH_FAILED_MESSAGE_MAX_BYTES == 512
    assert message == "é" * 256
    assert item.size <= PATCH_FAILED_ITEM_MAX_BYTES


def test_t1_feedback_redaction() -> None:
    """No file content, expected_text, new_text or exception text reaches
    the body or any event."""
    secret = "SECRET-FILE-CONTENT-7f3a"
    stale = make_plan(expected_text="    STALE-EXPECTED-91c2\n", new_text="    NEW-TEXT-44be\n")
    controller = _build(model=PlanPerPass(stale, FIXED), tools=FakeRepositoryTools(content=secret))
    controller.run()
    item = _one(_feedback(controller))
    body = json.dumps(item.body)
    for needle in (secret, "STALE-EXPECTED-91c2", "NEW-TEXT-44be", "Traceback"):
        assert needle not in body
    for event in controller.log.events:
        text = repr(event)
        for needle in (secret, "STALE-EXPECTED-91c2", "NEW-TEXT-44be"):
            assert needle not in text


def test_patch_failed_item_never_dropped_under_pressure() -> None:
    """The oldest item is the PATCH_FAILED feedback; newer droppable items
    overflow the cap. Dropping is oldest-first among droppable kinds, so the
    PATCH_FAILED item survives only because it is never droppable."""
    controller = _build()
    controller._baseline_summary = {}
    item = FeedbackItem(FeedbackKind.PATCH_FAILED, 0, {"error_code": "patch_validation_failed", "untrusted": True})
    controller._transcript.append(item)
    for i in range(5):
        controller._transcript.append(
            FeedbackItem(
                FeedbackKind.TOOL_RESULT,
                1,
                {"ok": False, "tool": "list_directory", "tool_call_id": f"tc-1-{i}", "pad": "y" * (60 * 1024)},
            )
        )
    context, _ = controller._build_context(1, 0)
    assert context.dropped_count > 0
    assert context.transcript[0] is item
    assert context.size <= 224 * 1024


def test_patch_failed_bound_fits_the_shared_repair_allowance() -> None:
    assert PATCH_FAILED_ITEM_MAX_BYTES <= VERIFICATION_ITEM_MAX_BYTES
    worst = FeedbackItem(
        FeedbackKind.PATCH_FAILED,
        10**9,
        {
            "change_digest": "f" * 200,
            "error_code": max((c.value for c in ErrorCode), key=len),
            "message": '"' * PATCH_FAILED_MESSAGE_MAX_BYTES,
            "plan_revision": 10**9,
            "previous_attempt_failed": True,
            "untrusted": True,
            "worktree_recreated": False,
        },
    )
    assert worst.size <= PATCH_FAILED_ITEM_MAX_BYTES


def test_identical_reproposal_reports_failure_and_requires_approval() -> None:
    approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
    controller = _build(model=PlanPerPass(FIXED), approval=approval)
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert len(approval.requests) == 2
    first, second = approval.requests
    assert second.change_digest == first.change_digest and second.material_change is False
    item = _one(_feedback(controller))
    assert item.body.get("previous_attempt_failed") is True
    assert item.body.get("change_digest") == first.change_digest


def test_revised_plan_requires_a_fresh_approval() -> None:
    approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
    controller = _build(approval=approval)
    controller.run()
    assert len(approval.requests) == 2 and approval.requests[1].material_change is True
    assert len(_of(controller, events.ApprovalRecorded)) == 2


def test_revised_plan_cannot_cite_earlier_pass() -> None:
    """A plan in pass 1 citing pass 0's read is denied; one citing pass 1's
    fresh read is accepted."""
    first_read: dict[str, str] = {}

    class CitesOld(PlanPerPass):
        def respond(self, context):
            self.contexts.append(context)
            reads = [
                i for i in context.transcript
                if i.kind is FeedbackKind.TOOL_RESULT and i.body.get("tool") == "read_file" and i.body["ok"]
            ]
            current = [i for i in reads if i.pass_index == context.pass_index]
            if not current:
                return turn(read_file_call("jobs/worker.py"))
            if context.pass_index == 0:
                first_read["id"] = current[-1].body["tool_call_id"]
                return turn(propose_plan_call(dataclasses.replace(STALE, evidence_refs=(first_read["id"],))))
            denied = [i for i in context.transcript if i.kind is FeedbackKind.TOOL_DENIED and i.pass_index == 1]
            if not denied:
                return turn(propose_plan_call(dataclasses.replace(FIXED, evidence_refs=(first_read["id"],))))
            return turn(propose_plan_call(dataclasses.replace(FIXED, evidence_refs=(current[-1].body["tool_call_id"],))))

    controller = _build(model=CitesOld(STALE))
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    denials = [e for e in _of(controller, events.PolicyDecisionRecorded) if not e.allowed]
    assert len(denials) == 1 and "evidence_ref_invalid" in denials[0].reason
    second_plan = _of(controller, events.PlanProposed)[1]
    assert second_plan.iteration == 1


# --------------------------------------------------------------------------
# Classification (T1-T10)
# --------------------------------------------------------------------------


def test_t2_substrate_terminates() -> None:
    controller = _build(applier=SequencedApplier((ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE,)))
    _assert_terminated_by(controller, controller.run(), ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE)


def test_t2_objects_unavailable_terminates() -> None:
    controller = _build(applier=SequencedApplier((ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE,)))
    _assert_terminated_by(controller, controller.run(), ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE)


@pytest.mark.parametrize("case", ["t3", "t4", "t5"])
def test_t3_to_t5_terminate_in_slice_a(case) -> None:
    setup, code = MAPPED[case]
    controller = _build(**setup())
    _assert_terminated_by(controller, controller.run(), code)


def _validation_on_advancing_session() -> dict:
    session = FakeCheckpointSession()
    session.intent = CheckpointIntent.ADVANCING
    session.accepted_sha = _H0
    return dict(applier=SequencedApplier((V,)), session=session)


@pytest.mark.parametrize(
    "setup, code",
    [MAPPED["t6"], (_validation_on_advancing_session, V)],
    ids=["establish-failure", "validation-on-advancing-session"],
)
def test_t6_non_present_session_terminates(setup, code) -> None:
    controller = _build(**setup())
    _assert_terminated_by(controller, controller.run(), code)


def test_t6_validation_code_on_a_non_present_session_never_continues(monkeypatch) -> None:
    """A PATCH_VALIDATION_FAILED is continuable only on a PRESENT session."""
    assert not ctl.patch_failure_continues(ctl.PatchFailureStage.APPLY, V, CheckpointIntent.ABSENT)
    assert not ctl.patch_failure_continues(ctl.PatchFailureStage.APPLY, V, CheckpointIntent.ADVANCING)
    assert not ctl.patch_failure_continues(ctl.PatchFailureStage.ENTRY_GATE, V, CheckpointIntent.PRESENT)
    assert not ctl.patch_failure_continues(None, V, CheckpointIntent.PRESENT)
    assert not ctl.patch_failure_continues(ctl.PatchFailureStage.APPLY, None, CheckpointIntent.PRESENT)
    assert ctl.patch_failure_continues(ctl.PatchFailureStage.APPLY, V, CheckpointIntent.PRESENT)


def test_t7_postcondition_terminates() -> None:
    setup, code = MAPPED["t7"]
    controller = _build(**setup())
    _assert_terminated_by(controller, controller.run(), code)


def test_t8_unexpected_exception_keeps_its_path() -> None:
    class Raises(FakePatchApplier):
        def apply(self, *args):
            raise RuntimeError("boom")

    controller = _build(applier=Raises())
    finished = controller.run()
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    failed = _one(_failed_patches(controller))
    assert failed.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert not _patch_failed_transitions(controller) and "C28" not in _sites(controller)


def test_t9_interrupt_keeps_its_raise_path() -> None:
    interrupt = KeyboardInterrupt()

    class Interrupts(FakePatchApplier):
        def apply(self, *args):
            raise interrupt

    controller = _build(applier=Interrupts())
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt
    assert not _patch_failed_transitions(controller) and "C28" not in _sites(controller)


def test_slice_a_contaminating_failure_still_terminates() -> None:
    """With a recreator supplied, a post-write failure (T4) never reaches it."""
    recreator = FakeRecreator()
    controller = _build(applier=SequencedApplier((ErrorCode.PATCH_APPLICATION_FAILED,)), recreator=recreator)
    finished = controller.run()
    _assert_terminated_by(controller, finished, ErrorCode.PATCH_APPLICATION_FAILED)
    assert (recreator.begin_calls, recreator.recreate_calls) == (0, 0)
    assert not _of(controller, events.WorkspaceRecreationStarted)


def test_t1_with_a_recreator_never_recreates() -> None:
    recreator = FakeRecreator()
    controller = _build(recreator=recreator)
    assert controller.run().terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert (recreator.begin_calls, recreator.recreate_calls) == (0, 0)


def test_stage_reset_across_two_attempts() -> None:
    """A T1 in pass 0, then a T7 postcondition in pass 1: the second attempt
    is classified by its own stage, not the first's APPLY."""

    class ThenOutside(SequencedApplier):
        def apply(self, run_id, iteration, approved_paths, operations):
            if not self._codes:
                self._changed_paths = ("unrelated_file.py",)
            return super().apply(run_id, iteration, approved_paths, operations)

    controller = _build(applier=ThenOutside((V,)))
    finished = controller.run()
    assert len(_patch_failed_transitions(controller)) == 1
    assert finished.error.code is ErrorCode.INTERNAL_INVARIANT_VIOLATION
    assert controller._patch_failure_stage is ctl.PatchFailureStage.POSTCONDITION
    # The stage is reset for every transaction: after a T1 and then a
    # successful attempt, no stale APPLY remains.
    succeeded = _build()
    assert succeeded.run().terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert succeeded._patch_failure_stage is None


# --------------------------------------------------------------------------
# Gate zero (§A1.3), for every mapped failure
# --------------------------------------------------------------------------


def test_t1_after_verifier_cleanup_unconfirmed_terminates() -> None:
    workspace = FakeWorkspace()
    controller = _build(workspace=workspace)
    controller._any_verifier_cleanup_unconfirmed = True
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert _failed_patches(controller)[0].error.code is V
    assert "C28" not in _sites(controller)
    assert not _patch_failed_transitions(controller) and not _feedback(controller)
    assert workspace.preserved and not workspace.disposed


@pytest.mark.parametrize("case", list(MAPPED))
def test_gate_zero_precedes_classification_for_every_mapped_failure(case, monkeypatch) -> None:
    calls = []
    real = ctl.patch_failure_continues
    monkeypatch.setattr(ctl, "patch_failure_continues", lambda *a: calls.append(a) or real(*a))
    setup, code = MAPPED[case]
    kwargs = setup()
    kwargs.setdefault("workspace", FakeWorkspace())
    recreator = FakeRecreator()
    controller = _build(recreator=recreator, **kwargs)
    controller._any_verifier_cleanup_unconfirmed = True
    finished = controller.run()
    assert calls == [], "Gate zero must run before classification"
    assert "C28" not in _sites(controller)
    assert not _patch_failed_transitions(controller) and not _feedback(controller)
    assert not _of(controller, events.BudgetExceeded)
    assert (recreator.begin_calls, recreator.recreate_calls) == (0, 0)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert _failed_patches(controller)[-1].error.code is code
    assert kwargs["workspace"].preserved and not kwargs["workspace"].disposed


def test_classifier_is_consulted_once_per_failed_patch_without_gate_zero(monkeypatch) -> None:
    calls = []
    real = ctl.patch_failure_continues
    monkeypatch.setattr(ctl, "patch_failure_continues", lambda *a: calls.append(a) or real(*a))
    controller = _build()
    controller.run()
    assert calls == [(ctl.PatchFailureStage.APPLY, V, CheckpointIntent.PRESENT)]


# --------------------------------------------------------------------------
# Budgets and accounting (§A1.8)
# --------------------------------------------------------------------------


def test_t1_budget_zero_never_continues() -> None:
    controller = _build(max_repair_iterations=0)
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED and finished.error is None
    budget = _one(_of(controller, events.BudgetExceeded))
    assert budget.kind is domain.BudgetKind.REPAIR_ITERATIONS
    assert (budget.limit_value, budget.observed_value, budget.state) == (0, 0, domain.RunState.EXECUTE)
    assert not _patch_failed_transitions(controller) and not _feedback(controller)
    assert _sites(controller)[-1] == "C28"


@pytest.mark.parametrize("maximum", [1, 3])
def test_t1_repair_budget_exhausts(maximum) -> None:
    controller = _build(model=PlanPerPass(STALE), applier=SequencedApplier((V,) * 10), max_repair_iterations=maximum)
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert len(_failed_patches(controller)) == maximum + 1
    assert len(_patch_failed_transitions(controller)) == maximum
    assert len(_feedback(controller)) == maximum
    budget = _one(_of(controller, events.BudgetExceeded))
    assert (budget.kind, budget.limit_value, budget.observed_value) == (domain.BudgetKind.REPAIR_ITERATIONS, maximum, maximum)


def test_patch_and_verification_failures_share_one_repair_budget() -> None:
    controller = _build(
        verifier=FakeVerifier((events.VerificationOutcome.TEST_FAILURE,)),
        applier=SequencedApplier((V,)),
        max_repair_iterations=2,
    )
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    # One patch continuation, one verification repair, then the budget refuses.
    assert len(_patch_failed_transitions(controller)) == 1
    assert len(_of(controller, events.VerificationCompleted)) == 2
    budget = _one(_of(controller, events.BudgetExceeded))
    assert budget.observed_value == 2
    assert len(_feedback(controller)) + len(_feedback(controller, FeedbackKind.VERIFICATION_FAILED)) == 3


def test_patch_failure_does_not_advance_verification_attempt() -> None:
    attempts = []

    class Recording(FakeVerifier):
        def run(self, attempt_index):
            attempts.append(attempt_index)
            return super().run(attempt_index)

    controller = _build(verifier=Recording((events.VerificationOutcome.PASSED,)))
    controller.run()
    assert attempts == [0]


def test_unique_tool_call_ids_and_pairing_across_passes() -> None:
    controller = _build(applier=SequencedApplier((V, V)))
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    requested = [e.tool_call_id for e in _of(controller, events.ToolRequested)]
    assert len(requested) == len(set(requested))
    patch_ids = [e.tool_call_id for e in _of(controller, events.ToolRequested) if e.tool is domain.ToolName.APPLY_PATCH]
    assert len(patch_ids) == 3
    assert len(_of(controller, events.ToolCompleted)) == len(requested)
    assert_tool_pairing(controller.log.events)


def _timed_t1_run(seconds: float):
    """A T1 continuation whose failed apply takes `seconds` on the clock."""
    clock = SteppingClock()

    class Slow(SequencedApplier):
        def apply(self, run_id, iteration, approved_paths, operations):
            if self._codes:
                clock._next_monotonic += seconds
            return super().apply(run_id, iteration, approved_paths, operations)

    controller = _build(applier=Slow())
    controller._clock = clock
    finished = controller.run()
    return finished, _failed_patches(controller)[0]


def test_failed_patch_duration_is_counted_once() -> None:
    base, _ = _timed_t1_run(0.0)
    finished, failed = _timed_t1_run(7.0)
    assert failed.duration_seconds == pytest.approx(7.0, abs=0.01)
    assert finished.total_duration_seconds - base.total_duration_seconds == pytest.approx(7.0)


# --------------------------------------------------------------------------
# Safe points C28 and C30, and the site inventory
# --------------------------------------------------------------------------


@pytest.mark.parametrize("site", ["C28", "C30"])
def test_cancellation_at_patch_failure_sites(site) -> None:
    controller = _build()
    _cancel_at(controller, site)
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED and finished.error is None
    assert _sites(controller)[-1] == site
    assert not _patch_failed_transitions(controller) and not _feedback(controller)


@pytest.mark.parametrize("site", ["C28", "C30"])
def test_deadline_at_patch_failure_sites(site) -> None:
    controller = _build()
    _deadline_at(controller, site)
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    budget = _one(_of(controller, events.BudgetExceeded))
    assert budget.kind is domain.BudgetKind.WALL_CLOCK
    assert _sites(controller)[-1] == site
    assert not _patch_failed_transitions(controller) and not _feedback(controller)


def test_t1_cancellation_at_c30_leaves_transcript_and_counters_unchanged() -> None:
    controller = _build()
    lengths = []
    _arm(controller, "C30", lambda: lengths.append(len(controller._transcript)))
    source = CancellationSource()
    controller._cancellation = source.token
    _arm(controller, "C30", lambda: source.request("test"))
    finished = controller.run()
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED
    assert len(controller._transcript) == lengths[0]
    assert not _feedback(controller)
    assert finished.iterations_used == 1


def test_cancellation_sweep_includes_c28_c30() -> None:
    controller = _build()
    controller.run()
    sites = _sites(controller)
    assert "C28" in sites
    i = sites.index("C28")
    assert sites[i : i + 3] == ["C28", "C30", "C5"]


def test_patch_failure_site_inventory_slice_a() -> None:
    controller = _build()
    controller.run()
    failed = _failed_patches(controller)[0]
    transition = _one(_patch_failed_transitions(controller))
    entries = [(s, seq) for s, seq in controller._cancellation_sites if failed.sequence < seq <= transition.sequence]
    assert [s for s, _ in entries] == ["C28", "C30"]
    sites = _sites(controller)
    assert sites.count("C28") == 1 and sites.count("C30") == 1
    assert "C29" not in sites
    assert all(site != "C29" for site, _, _ in controller._post_return_decisions)
    assert "C29" not in ctl._TRANSITION_CHECK_SITES.values()
    assert "C29" not in ctl._DISPATCH_CHECK_SITES.values()
    assert ctl._TRANSITION_CHECK_SITES[domain.Trigger.PATCH_FAILED] == "C30"


# --------------------------------------------------------------------------
# Real Git: a stale expected_text against the real fixture, then the fix
# --------------------------------------------------------------------------

INCREMENT = "    job.retry_count += 1\n"
CHECK = "    if job.idempotency_key in already_processed:\n        return \"duplicate\"\n"
REAL_STALE = ProposedChange("jobs/worker.py", "    job.retry_count += 2\n", "    pass\n")
REAL_FIX = ProposedChange("jobs/worker.py", INCREMENT + CHECK, CHECK + INCREMENT)
BASELINE_MARKER = "CODEAGENT_FIXTURE_MARKER retry_count_after_duplicate"


def _real_plan(change: ProposedChange, read_id: str) -> ProposePlanArgs:
    return ProposePlanArgs(
        problem_hypothesis="retry_count is incremented for duplicate deliveries",
        evidence_refs=(read_id,),
        change=change,
        verification_intent="python3 -B -m unittest tests.test_worker",
        risk_notes="",
    )


class StaleThenFixModel:
    """Reads the real file each pass. Pass 0 proposes a stale change; pass 1
    proposes the fix only after seeing the PATCH_FAILED feedback and a fresh
    read showing the file unchanged."""

    model_name = "fake-model-v1"

    def __init__(self, *, require_baseline_marker: bool = False) -> None:
        self.reads: list[str] = []
        self.gates: list[str] = []
        self.require_baseline_marker = require_baseline_marker

    def respond(self, context):
        current = [
            i for i in context.transcript
            if i.kind is FeedbackKind.TOOL_RESULT and i.pass_index == context.pass_index and i.body["ok"]
        ]
        if not current:
            return turn(read_file_call("jobs/worker.py"))
        read_id = current[-1].body["tool_call_id"]
        content = "".join(text for _, text in current[-1].body["payload"]["lines"])
        self.reads.append(content)
        if context.pass_index == 0:
            if self.require_baseline_marker:
                tail = context.baseline["stdout_tail"] + context.baseline["stderr_tail"]
                if BASELINE_MARKER not in tail:
                    raise AssertionError("baseline marker missing from the model-visible baseline tail")
            return turn(propose_plan_call(_real_plan(REAL_STALE, read_id)))
        (feedback,) = [i for i in context.transcript if i.kind is FeedbackKind.PATCH_FAILED]
        body = feedback.body
        if not (body["previous_attempt_failed"] and body["error_code"] == "patch_validation_failed"):
            raise AssertionError("patch-failure feedback missing from the model context")
        if body["worktree_recreated"] or content != self.reads[0]:
            raise AssertionError("a pre-write refusal must leave the worktree unchanged")
        self.gates.append("patch_failed")
        return turn(propose_plan_call(_real_plan(REAL_FIX, read_id)))


def test_d5a_stale_expected_text_continues_real_git() -> None:
    with real_fixture_repo() as repo, tempfile.TemporaryDirectory(prefix="codeagent-evidence-") as evidence_root:
        original = (repo / "jobs" / "worker.py").read_text()
        lifecycle_id = new_lifecycle_id()
        worktree = GitWorktree(repo, run_id="r-d5a-git")
        session = CheckpointSession(CheckpointRef(repo, lifecycle_id))
        model = StaleThenFixModel()
        with worktree as path:
            controller = RunController(
                RunConfig(
                    run_id="r-d5a-git",
                    task_statement="fix retry bug",
                    approval_mode=domain.ApprovalMode.INTERACTIVE,
                    repository_path=str(path),
                    lifecycle_id=lifecycle_id,
                ),
                model,
                FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
                FakeVerifier((events.VerificationOutcome.PASSED,)),
                GitPatchApplier(path),
                RepoTools(path),
                worktree,
                session,
                FilesystemEvidenceSink(Path(evidence_root)),
                clock=SteppingClock(),
            )
            finished = controller.run()
            assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
            assert model.gates == ["patch_failed"] and model.reads[0] == model.reads[1] == original
            failed = _one(_failed_patches(controller))
            assert failed.error.code is V
            applied = _one(_of(controller, events.PatchApplied))
            assert applied.change_digest == _of(controller, events.PlanProposed)[1].change_digest
            assert len(_patch_failed_transitions(controller)) == 1
            assert_model_pairing(controller.log.events)
            assert_tool_pairing(controller.log.events)
            assert worktree.path is None and not path.exists()
        assert (repo / "jobs" / "worker.py").read_text() == original
        refs = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "refs/codeagent"], capture_output=True, text=True, check=True
        ).stdout
        assert refs.strip() == ""


# --------------------------------------------------------------------------
# Real Docker acceptance (CI verbose node)
# --------------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available() and os.environ.get("CODEAGENT_REQUIRE_DOCKER") != "1",
    reason="requires a running local Docker daemon",
)


@requires_docker
def test_d5a_stale_expected_text_continues_to_passing_verification_real_docker(tmp_path, monkeypatch) -> None:
    if not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available", pytrace=False)
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    with real_fixture_repo() as repo:
        original = (repo / "jobs" / "worker.py").read_text()
        log = EventLog()
        model = StaleThenFixModel(require_baseline_marker=True)
        approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
        try:
            result = lr.run_lifecycle_aware(
                repo,
                run_id="r-d5a-docker",
                task_statement="fix the retry counter for duplicate deliveries",
                approval_mode=domain.ApprovalMode.INTERACTIVE,
                evidence_root=tmp_path / "evidence",
                model=model,
                approval=approval,
                verify_command=FIXTURE_VERIFY_COMMAND,
                clock=SystemClock(),
                event_log=log,
            )
        finally:
            for lifecycle_dir in state.glob("repos/*/runs/*"):
                for role in ("baseline", "verification"):
                    subprocess.run(["docker", "rm", "-f", f"codeagent-{role}-{lifecycle_dir.name}"], capture_output=True)
        trace = log.events
        finished = result.finished
        assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
        assert model.gates == ["patch_failed"] and model.reads[0] == model.reads[1] == original
        failed = [e for e in trace if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.APPLY_PATCH and not e.success]
        assert [e.error.code for e in failed] == [V]
        transitions = [e for e in trace if isinstance(e, events.StateTransitioned) and e.trigger is domain.Trigger.PATCH_FAILED]
        assert len(transitions) == 1
        proposed = [e for e in trace if isinstance(e, events.PlanProposed)]
        applied = [e for e in trace if isinstance(e, events.PatchApplied)]
        assert len(proposed) == 2 and [e.change_digest for e in applied] == [proposed[1].change_digest]
        assert len(approval.requests) == 2 and approval.requests[1].material_change is True
        outcomes = [e.outcome for e in trace if isinstance(e, events.VerificationCompleted)]
        assert outcomes == [events.VerificationOutcome.PASSED]
        assert_model_pairing(trace)
        assert_tool_pairing(trace)
        # The original checkout is untouched and every owned resource is gone.
        assert (repo / "jobs" / "worker.py").read_text() == original
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
        assert git("status", "--porcelain").strip() == ""
        assert git("for-each-ref", "refs/codeagent").strip() == ""
        (run_dir,) = state.glob("repos/*/runs/*")
        assert json.loads((run_dir / "lifecycle.json").read_text())["state"] == "COMPLETE"
        names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True).stdout
        assert run_dir.name not in names
        assert not list(state.glob(f"worktrees/*/{run_dir.name}"))
