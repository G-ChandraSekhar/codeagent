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


def _code(finished):
    """The terminal error code; a missing error is an explicit assertion failure."""
    assert finished.error is not None, f"no error on {finished.terminal_reason}"
    return finished.error.code

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
    """`begin()` returns `sequence`; `recreate()` returns `result`, or fails
    the test when no result is configured (a path that must never recreate)."""

    def __init__(self, sequence=1, result=None) -> None:
        self.begin_calls = 0
        self.recreate_calls = 0
        self._sequence, self._result = sequence, result

    def begin(self, *, trigger_code, checkpoint_id):
        self.begin_calls += 1
        return self._sequence

    def recreate(self, sequence):
        self.recreate_calls += 1
        if self._result is None:
            raise AssertionError("this path must never recreate")
        return self._result


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
            # ADR 0007 Amendment 2: the recreation form is the larger one.
            "worktree_recreated": True,
            "recreation_outcome": "recreated",
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
def test_t3_to_t5_without_a_recreator_terminate_before_c28(case) -> None:
    """ADR 0004 Amendment 21 decision 1 (renamed from
    `test_t3_to_t5_terminate_in_slice_a`, behaviour unchanged): with no
    recreator, a T3-T5 failure ends the run with `E` before C28, with no
    budget check, recreation event, transition or feedback."""
    setup, code = MAPPED[case]
    controller = _build(**setup())
    _assert_terminated_by(controller, controller.run(), code)
    assert not _of(controller, events.BudgetExceeded)
    assert not _of(controller, events.WorkspaceRecreationStarted)


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


def _disposal_failed_result():
    from codeagent.controller import RecreationResult

    return RecreationResult(
        outcome=events.RecreationOutcome.DISPOSAL_FAILED,
        error=OperationalError(code=ErrorCode.WORKSPACE_RECREATION_FAILED, error_id="rec-1", message="fixed"),
        artifact_id=f"{_LID}.recreation-01", sha256_payload="d" * 64, bytes_written=10, evidence_complete=True,
    )


@pytest.mark.parametrize("refuse", [True, False], ids=["begin-refuses", "recreation-fails"])
def test_contaminating_failure_never_continues_without_recreated(refuse) -> None:
    """ADR 0004 Amendment 21 (migrated from D5-A's
    `test_slice_a_contaminating_failure_still_terminates`, which asserted the
    recreator is never called): a post-write failure (T4) with a recreator now
    reaches `begin()`, and never continues unless the outcome is `RECREATED`."""
    recreator = FakeRecreator(sequence=None) if refuse else FakeRecreator(result=_disposal_failed_result())
    controller = _build(applier=SequencedApplier((ErrorCode.PATCH_APPLICATION_FAILED,)), recreator=recreator)
    finished = controller.run()
    assert recreator.begin_calls == 1
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert not _patch_failed_transitions(controller) and not _feedback(controller)
    expected = ErrorCode.PATCH_APPLICATION_FAILED if refuse else ErrorCode.WORKSPACE_RECREATION_FAILED
    assert _code(finished) is expected


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
    # ADR 0004 Amendment 21: Gate zero also precedes the recreation classifier.
    real_recreation = ctl.classify_recreation_trigger
    monkeypatch.setattr(ctl, "classify_recreation_trigger", lambda *a: calls.append(a) or real_recreation(*a))
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


# ===========================================================================
# D5-B (ADR 0004 Amendment 21): recreation continues the repair loop, real Git
# ===========================================================================

from types import SimpleNamespace  # noqa: E402

from codeagent.controller import VerificationResult  # noqa: E402
from tests.support.scripted_model import search_text_call  # noqa: E402

_CURRENT_PASS = [0]


class _RecordingTools(RepoTools):
    """The real toolkit, recording each instance and the pass of each call."""

    instances: list = []

    def __init__(self, path):
        super().__init__(path)
        self.calls: list[int] = []
        _RecordingTools.instances.append(self)

    def read_file(self, args, should_stop=lambda: False):
        self.calls.append(_CURRENT_PASS[0])
        return super().read_file(args, should_stop)

    def search_text(self, args, should_stop=lambda: False):
        self.calls.append(_CURRENT_PASS[0])
        return super().search_text(args, should_stop)


class _ContaminatingApplier(GitPatchApplier):
    """The real applier, except that its first `contaminate` calls write into
    the worktree and report a post-write failure (T4). It holds a descriptor
    to the first incarnation and records, after each real commit, the commit,
    its parent and whether the path still names that first incarnation."""

    contaminate = 1
    records: list = []
    first_fd: list = []

    def apply(self, run_id, iteration, approved_paths, operations):
        path = self._worktree_path
        if _ContaminatingApplier.contaminate:
            _ContaminatingApplier.contaminate -= 1
            if not _ContaminatingApplier.first_fd:
                _ContaminatingApplier.first_fd.append(os.open(path, os.O_RDONLY))
            target = path / "jobs" / "worker.py"
            target.write_text(target.read_text() + "# contaminated\n")
            return PatchResult(
                success=False, operation_count=0, changed_paths=(), diff_bytes=0,
                error=OperationalError(code=ErrorCode.PATCH_APPLICATION_FAILED, error_id=f"{run_id}-c", message="post-write"),
            )
        result = super().apply(run_id, iteration, approved_paths, operations)
        if result.success:
            git = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
            first = _ContaminatingApplier.first_fd[0]
            _ContaminatingApplier.records.append(SimpleNamespace(
                head=git("rev-parse", "HEAD"), parent=git("rev-parse", "HEAD^"), commit=result.commit_hash,
                same_as_first=os.path.samestat(os.fstat(first), os.stat(path)),
            ))
        return result


class _PathVerifier:
    """Stands in for DockerVerifier on the composition's path: passes only if
    the corrected content is present at that path (so it proves which
    incarnation the retained verifier reads)."""

    seen: list = []

    def __init__(self, path, **kwargs):
        self._path = Path(path)

    @property
    def command(self):
        return FIXTURE_VERIFY_COMMAND

    def _result(self, ok: bool) -> VerificationResult:
        return VerificationResult(
            outcome=events.VerificationOutcome.PASSED if ok else events.VerificationOutcome.TEST_FAILURE,
            exit_code=0 if ok else 1, duration_seconds=0.01, stdout="", stderr="",
            cleanup_status=events.ContainerCleanupStatus.CONFIRMED_ABSENT,
        )

    def run_baseline(self):
        return self._result(False)

    def run(self, attempt_index):
        content = (self._path / "jobs" / "worker.py").read_text()
        _PathVerifier.seen.append(content)
        return self._result(REAL_FIX.new_text in content and "# contaminated" not in content)


class _FixEveryPass:
    """Reads in every pass and proposes REAL_FIX citing that pass's read; after
    a recreation it requires the ADR 0007 Amendment 2 feedback. `search` adds
    a pass-0 paginated search and a pass-1 replay of its stale cursor."""

    model_name = "fake-model-v1"

    def __init__(self, *, search: bool = False) -> None:
        self.search = search
        self.cursor: list[str] = []
        self.feedback: list[dict] = []

    def respond(self, context):
        _CURRENT_PASS[0] = context.pass_index
        current = [i for i in context.transcript if i.kind is FeedbackKind.TOOL_RESULT and i.pass_index == context.pass_index]
        denied = [i for i in context.transcript if i.kind is FeedbackKind.TOOL_DENIED and i.pass_index == context.pass_index]
        if self.search and context.pass_index == 0 and not current:
            return turn(search_text_call("job", ".", limit=1))
        if self.search and context.pass_index == 0 and len(current) == 1:
            self.cursor.append(current[0].body["payload"]["next_cursor"])
            return turn(read_file_call("jobs/worker.py"))
        if self.search and context.pass_index == 1 and not denied and not current:
            return turn(search_text_call("job", ".", cursor=self.cursor[0], limit=1))
        reads = [i for i in current if i.body["tool"] == "read_file" and i.body["ok"]]
        if not reads:
            return turn(read_file_call("jobs/worker.py"))
        if context.pass_index >= 1:
            self.feedback.append([i.body for i in context.transcript if i.kind is FeedbackKind.PATCH_FAILED][-1])
        return turn(propose_plan_call(_real_plan(REAL_FIX, reads[-1].body["tool_call_id"])))


@pytest.fixture
def composed_t4(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "GitPatchApplier", _ContaminatingApplier)
    monkeypatch.setattr(lr, "DockerVerifier", _PathVerifier)
    monkeypatch.setattr(lr, "RepoTools", _RecordingTools)
    _ContaminatingApplier.contaminate, _ContaminatingApplier.records = 1, []
    _ContaminatingApplier.first_fd = []
    _RecordingTools.instances, _PathVerifier.seen = [], []
    with real_fixture_repo() as repo:
        yield SimpleNamespace(repo=repo, tmp=tmp_path)
    for fd in _ContaminatingApplier.first_fd:
        os.close(fd)


def _run_t4(c, model=None, **kwargs):
    log = EventLog()
    result = lr.run_lifecycle_aware(
        c.repo, run_id="r-d5b-git", task_statement="fix the retry counter", approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=c.tmp / "evidence", model=model or _FixEveryPass(),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)), event_log=log, **kwargs,
    )
    return result, log.events


def _lid(c) -> str:
    run_dir = _one((c.tmp / "state" / "repos").glob("*/runs/*"))
    return run_dir.name


def _assert_composed_clean(c) -> None:
    git = lambda *a: subprocess.run(["git", "-C", str(c.repo), *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
    assert git("for-each-ref", "refs/codeagent").strip() == ""
    assert len([l for l in git("worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]) == 1
    run_dir = _one((c.tmp / "state" / "repos").glob("*/runs/*"))
    assert json.loads((run_dir / "lifecycle.json").read_text())["state"] == "COMPLETE"
    assert not list((c.tmp / "state").glob(f"worktrees/*/{run_dir.name}"))


def test_composed_recreation_continues_to_pass(composed_t4) -> None:
    """B1, B2 and the §21.6 applier proof: T4 → recreation → C30 → reread
    through the new toolkit → the retained applier commits in n+1 → pass."""
    origin = subprocess.run(["git", "-C", str(composed_t4.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    model = _FixEveryPass()
    result, trace = _run_t4(composed_t4, model)
    finished = result.finished
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
    done = [e for e in trace if isinstance(e, events.WorkspaceRecreationFinished)]
    assert [e.outcome for e in done] == [events.RecreationOutcome.RECREATED]
    assert len(_RecordingTools.instances) == 2
    n_tools, n1_tools = _RecordingTools.instances
    assert n_tools.calls and set(n_tools.calls) == {0}, "n's toolkit is never used after recreation"
    assert n1_tools.calls and set(n1_tools.calls) == {1}
    record = _one(_ContaminatingApplier.records)
    assert record.head == record.commit and record.parent == origin and record.same_as_first is False
    applied = [e for e in trace if isinstance(e, events.PatchApplied)]
    assert [e.checkpoint_id for e in applied] == [record.commit]
    assert model.feedback and model.feedback[0].get("worktree_recreated") is True
    assert model.feedback[0].get("recreation_outcome") == "recreated"
    assert "# contaminated" not in _PathVerifier.seen[-1]
    events.validate_recreation_trace(trace)
    assert_model_pairing(trace)
    assert_tool_pairing(trace)
    lid = _lid(composed_t4)
    assert (composed_t4.tmp / "evidence" / f"{lid}.recreation-01.evidence").exists()
    assert (composed_t4.tmp / "evidence" / f"{lid}.evidence").exists()
    _assert_composed_clean(composed_t4)


def test_composed_two_recreations_then_success(composed_t4) -> None:
    _ContaminatingApplier.contaminate = 2
    result, trace = _run_t4(composed_t4)
    assert result.finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    done = [e for e in trace if isinstance(e, events.WorkspaceRecreationFinished)]
    assert [(e.recreation_sequence, e.outcome) for e in done] == [(1, events.RecreationOutcome.RECREATED), (2, events.RecreationOutcome.RECREATED)]
    assert len(_RecordingTools.instances) == 3
    lid = _lid(composed_t4)
    for k in (1, 2):
        assert (composed_t4.tmp / "evidence" / f"{lid}.recreation-{k:02d}.evidence").exists()
    assert (composed_t4.tmp / "evidence" / f"{lid}.evidence").exists()
    events.validate_recreation_trace(trace)
    _assert_composed_clean(composed_t4)


def test_composed_recreation_budget_exhausted_ends_with_the_triggering_error(composed_t4) -> None:
    _ContaminatingApplier.contaminate = 2
    result, trace = _run_t4(composed_t4, max_recreations=1)
    assert _code(result.finished) is ErrorCode.PATCH_APPLICATION_FAILED
    assert len([e for e in trace if isinstance(e, events.WorkspaceRecreationStarted)]) == 1
    _assert_composed_clean(composed_t4)


def test_composed_repair_budget_exhausted_never_recreates(composed_t4) -> None:
    result, trace = _run_t4(composed_t4, max_repair_iterations=0)
    assert result.finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert not [e for e in trace if isinstance(e, events.WorkspaceRecreationStarted)]
    _assert_composed_clean(composed_t4)


def test_old_search_cursor_refused_after_recreation(composed_t4) -> None:
    """B3: a search cursor issued by n's toolkit is unknown to n+1's."""
    model = _FixEveryPass(search=True)
    result, trace = _run_t4(composed_t4, model)
    assert result.finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert model.cursor and model.cursor[0]
    denials = [e for e in trace if isinstance(e, events.PolicyDecisionRecorded) and not e.allowed]
    assert [d.reason for d in denials] == ["cursor_invalid:chain_unknown"]
    assert denials[0].iteration == 1


# --- Real Docker (CI verbose node) ------------------------------------------

D5B_SENTINEL = "    # D5B_SENTINEL_N1\n"
REAL_FIX_MARKED = ProposedChange("jobs/worker.py", INCREMENT + CHECK, CHECK + INCREMENT + D5B_SENTINEL)
# Appended to the tracked `jobs/worker.py` in incarnation n (ADR 0004
# Amendment 21's post-acceptance correction: an untracked contaminant would
# make Phase E's evidence incomplete, so it could never reach RECREATED).
CONTAMINATION_MARKER = "# CONTAMINATION_FROM_N"


class _StaleThenFixTwice:
    """Pass 0: a stale replacement (T1). Passes 1 and 2: the marked fix,
    each citing that pass's read; records reads and feedback."""

    model_name = "fake-model-v1"

    def __init__(self) -> None:
        self.reads: dict[int, str] = {}
        self.feedback: list[dict] = []

    def respond(self, context):
        _CURRENT_PASS[0] = context.pass_index
        reads = [i for i in context.transcript
                 if i.kind is FeedbackKind.TOOL_RESULT and i.pass_index == context.pass_index and i.body["ok"]]
        if not reads:
            return turn(read_file_call("jobs/worker.py"))
        self.reads[context.pass_index] = "".join(text for _, text in reads[-1].body["payload"]["lines"])
        read_id = reads[-1].body["tool_call_id"]
        if context.pass_index == 0:
            return turn(propose_plan_call(_real_plan(REAL_STALE, read_id)))
        self.feedback.append([i.body for i in context.transcript if i.kind is FeedbackKind.PATCH_FAILED][-1])
        return turn(propose_plan_call(_real_plan(REAL_FIX_MARKED, read_id)))


class _ContaminatingApproval(FakeApprovalProvider):
    """Approves everything. During the second approval (pass 1) it appends a
    contamination marker to the tracked `jobs/worker.py` in incarnation n
    (an uncommitted modification) and holds a descriptor to n; during the
    third (pass 2) it records what the path now names."""

    def __init__(self, state: Path) -> None:
        super().__init__((domain.ApprovalDecision.APPROVED,))
        self.state, self.held, self.pass2 = state, [], {}

    def _leaf(self) -> Path:
        leaf = _one(self.state.glob("worktrees/*/*"))
        return leaf

    def decide(self, request):
        n = len(self.requests) + 1
        if n == 2:
            leaf = self._leaf()
            target = leaf / "jobs" / "worker.py"
            target.write_text(target.read_text() + CONTAMINATION_MARKER + "\n")
            self.held.append(os.open(leaf, os.O_RDONLY))
        elif n == 3:
            leaf = self._leaf()
            git = lambda *a: subprocess.run(["git", "-C", str(leaf), *a], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
            self.pass2 = {
                "same_as_n": os.path.samestat(os.fstat(self.held[0]), os.stat(leaf)),
                "head": git("rev-parse", "HEAD"),
                "contamination_visible": CONTAMINATION_MARKER in (leaf / "jobs" / "worker.py").read_text(),
            }
        return super().decide(request)


@requires_docker
def test_d5b_contaminated_worktree_recreates_and_passes_real_docker(tmp_path, monkeypatch) -> None:
    """ADR 0004 Amendment 21 decision 5: pass 0 establishes a PRESENT session
    through T1; pass-1 approval contaminates n, so the entry gate fails as T3;
    recreation builds n+1 at the checkpoint; pass 2 re-proposes the identical
    fix, and the retained applier and real Docker verifier act on n+1."""
    if not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available", pytrace=False)
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    monkeypatch.setattr(lr, "RepoTools", _RecordingTools)
    _RecordingTools.instances = []
    # Inside the container: the fixture tests pass, the approved change's own
    # sentinel line is present, and n's contamination marker is absent.
    verify = ("sh", "-c", "python3 -B -m unittest tests.test_worker && grep -q D5B_SENTINEL_N1 jobs/worker.py "
              f"&& ! grep -q '{CONTAMINATION_MARKER}' jobs/worker.py")
    with real_fixture_repo() as repo:
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
        origin = git("rev-parse", "HEAD")
        original = (repo / "jobs" / "worker.py").read_text()
        log = EventLog()
        model = _StaleThenFixTwice()
        approval = _ContaminatingApproval(state)
        try:
            result = lr.run_lifecycle_aware(
                repo, run_id="r-d5b-docker", task_statement="fix the retry counter for duplicate deliveries",
                approval_mode=domain.ApprovalMode.INTERACTIVE, evidence_root=tmp_path / "evidence",
                model=model, approval=approval, verify_command=verify, clock=SystemClock(), event_log=log,
            )
        finally:
            for fd in approval.held:
                os.close(fd)
            for lifecycle_dir in state.glob("repos/*/runs/*"):
                for role in ("baseline", "verification"):
                    subprocess.run(["docker", "rm", "-f", f"codeagent-{role}-{lifecycle_dir.name}"], capture_output=True)
        trace, finished = log.events, result.finished
        assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
        failed = [_code(e) for e in trace if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.APPLY_PATCH and not e.success]
        assert failed == [V, ErrorCode.WORKSPACE_ENTRY_GATE_FAILED]
        started = [e for e in trace if isinstance(e, events.WorkspaceRecreationStarted)]
        done = [e for e in trace if isinstance(e, events.WorkspaceRecreationFinished)]
        assert [(e.trigger_code, e.checkpoint_id) for e in started] == [(ErrorCode.WORKSPACE_ENTRY_GATE_FAILED, origin)]
        assert [e.outcome for e in done] == [events.RecreationOutcome.RECREATED]
        assert done[0].evidence_complete is True  # Phase E captured a complete artifact
        assert len([e for e in trace if isinstance(e, events.StateTransitioned) and e.trigger is domain.Trigger.PATCH_FAILED]) == 2
        assert len(model.feedback) == 2
        assert model.feedback[0].get("worktree_recreated") is False and "recreation_outcome" not in model.feedback[0]
        assert model.feedback[1].get("worktree_recreated") is True and model.feedback[1].get("recreation_outcome") == "recreated"
        assert len(approval.requests) == 3 and approval.requests[2].material_change is False
        assert approval.pass2 == {"same_as_n": False, "head": origin, "contamination_visible": False}
        assert len(_RecordingTools.instances) == 2
        n_tools, n1_tools = _RecordingTools.instances
        assert set(n_tools.calls) == {0, 1} and set(n1_tools.calls) == {2}
        applied = _one([e for e in trace if isinstance(e, events.PatchApplied)])
        assert git("rev-parse", f"{applied.checkpoint_id}^") == origin
        committed = git("show", f"{applied.checkpoint_id}:jobs/worker.py") + "\n"
        assert D5B_SENTINEL in committed and CONTAMINATION_MARKER not in committed
        outcomes = [e.outcome for e in trace if isinstance(e, events.VerificationCompleted)]
        assert outcomes == [events.VerificationOutcome.PASSED]
        events.validate_recreation_trace(trace)
        assert_model_pairing(trace)
        assert_tool_pairing(trace)
        assert (repo / "jobs" / "worker.py").read_text() == original
        assert git("status", "--porcelain") == "" and git("for-each-ref", "refs/codeagent") == ""
        run_dir = _one(state.glob("repos/*/runs/*"))
        assert json.loads((run_dir / "lifecycle.json").read_text())["state"] == "COMPLETE"
        names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True).stdout
        assert run_dir.name not in names
        assert not list(state.glob(f"worktrees/*/{run_dir.name}"))
