"""ADR 0004 Amendment 20, D5/C-1 Step 6: the controller's recreation seam.

`_recreation_can_continue()` is hard False until D5 integration adds the
repair transition (N4), so `run()` never calls the recreator. Tests that
exercise the seam enable that predicate explicitly and drive a fake
recreator; nothing here creates a reservation, artifact or real worktree.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
from pathlib import Path

import pytest

from codeagent import controller as ctl
from codeagent import domain, events
from codeagent.checkpoint_ref import CheckpointRefError, CheckpointRefFailure, MutationOutcome
from codeagent.checkpoint_session import CheckpointIntent
from codeagent.controller import (
    PatchFailureStage,
    RecreationResult,
    RunConfig,
    RunController,
    WorkspaceRecreator,
    classify_recreation_trigger,
)
from codeagent.errors import ErrorCode, OperationalError
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakeLifecycleOwnerPublisher,
    FakeModel,
    FakePatchApplier,
    FakeRepositoryTools,
    FakeVerifier,
    FakeWorkspace,
    SteppingClock,
    make_plan,
)

_LID = "a" * 32
_H0 = "a" * 40
_NEXT = "c" * 40
O = events.RecreationOutcome


def _receipt(sequence: int = 1, complete: bool = True) -> dict:
    return dict(
        artifact_id=f"{_LID}.recreation-{sequence:02d}",
        sha256_payload="d" * 64,
        bytes_written=10,
        evidence_complete=complete,
    )


def _error(code: ErrorCode) -> OperationalError:
    return OperationalError(code=code, error_id=f"rec-{code.value}", message="fixed recreation failure")


def _result(outcome: events.RecreationOutcome, workspace=None) -> RecreationResult:
    if outcome is O.RECREATED:
        return RecreationResult(outcome=outcome, workspace=workspace, **_receipt())
    if outcome is O.EVIDENCE_NOT_CONFIRMED:
        return RecreationResult(outcome=outcome, error=_error(ErrorCode.EVIDENCE_ARTIFACT_COLLISION))
    return RecreationResult(
        outcome=outcome, workspace=workspace, error=_error(ErrorCode.WORKSPACE_RECREATION_FAILED), **_receipt()
    )


class FakeRecreator:
    def __init__(self, *, sequence: int | None = 1, result=None, on_recreate=None) -> None:
        self.sequence = sequence
        self.result = result
        self.on_recreate = on_recreate
        self.begin_calls: list[dict] = []
        self.recreate_calls: list[int] = []

    def begin(self, *, trigger_code, checkpoint_id):
        self.begin_calls.append({"trigger_code": trigger_code, "checkpoint_id": checkpoint_id})
        return self.sequence

    def recreate(self, sequence):
        self.recreate_calls.append(sequence)
        if self.on_recreate is not None:
            self.on_recreate()
        return self.result


class _FlagToken:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def reason(self):
        return None


def _apply_failure() -> FakePatchApplier:
    return FakePatchApplier(True, failure_code=ErrorCode.PATCH_APPLICATION_FAILED)


def _build(
    *,
    recreator=None,
    patch_applier=None,
    workspace=None,
    session=None,
    sink=None,
    owner=None,
    cancellation=None,
    run_id="r-rec",
) -> RunController:
    kwargs = {}
    if cancellation is not None:
        kwargs["cancellation"] = cancellation
    return RunController(
        RunConfig(
            run_id=run_id,
            task_statement="fix retry bug",
            approval_mode=domain.ApprovalMode.INTERACTIVE,
            lifecycle_id=_LID,
            max_repair_iterations=3,
            max_plan_revisions=2,
        ),
        FakeModel(make_plan()),
        FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        FakeVerifier((events.VerificationOutcome.PASSED,), baseline_outcome=events.VerificationOutcome.TEST_FAILURE),
        patch_applier if patch_applier is not None else _apply_failure(),
        FakeRepositoryTools(),
        workspace if workspace is not None else FakeWorkspace(initial_commit=_H0),
        session if session is not None else FakeCheckpointSession(),
        sink if sink is not None else FakeEvidenceSink(),
        clock=SteppingClock(),
        lifecycle_owner=owner,
        workspace_recreator=recreator,
        **kwargs,
    )


def _run(controller):
    """`run()` with any escaped ordinary exception reported as an explicit
    assertion failure, never as an incidental error. A `BaseException`
    still propagates, for the raise-path tests."""
    caught = None
    try:
        return controller.run()
    except Exception as exc:  # noqa: BLE001 - converted into an explicit assertion below
        caught = exc
    assert caught is None, f"run() raised {type(caught).__name__}"


@pytest.fixture
def seam_enabled(monkeypatch):
    """Stand-in for D5 integration's repair transition (N4): lets `run()`
    reach the recreation seam. Production keeps the predicate False."""
    monkeypatch.setattr(RunController, "_recreation_can_continue", lambda self: True)


def _types(controller) -> list[str]:
    return [type(e).__name__ for e in controller.log.events]


def _recreation_events(controller):
    return [
        e for e in controller.log.events
        if isinstance(e, (events.WorkspaceRecreationStarted, events.WorkspaceRecreationFinished))
    ]


def _last_recreation_event(controller):
    found = _recreation_events(controller)
    assert found, "expected recreation events"
    return found[-1]


def _shape(controller):
    return [(type(e).__name__, getattr(getattr(e, "error", None), "code", None)) for e in controller.log.events]


# --- Structure -------------------------------------------------------------


def test_protocol_result_and_seam_are_structurally_pinned() -> None:
    begin = inspect.signature(WorkspaceRecreator.begin)
    assert list(begin.parameters) == ["self", "trigger_code", "checkpoint_id"]
    assert all(begin.parameters[n].kind is inspect.Parameter.KEYWORD_ONLY for n in ("trigger_code", "checkpoint_id"))
    assert list(inspect.signature(WorkspaceRecreator.recreate).parameters) == ["self", "sequence"]
    assert {n for n in vars(WorkspaceRecreator) if not n.startswith("_")} == {"begin", "recreate"}
    assert [f.name for f in dataclasses.fields(RecreationResult)] == [
        "outcome", "workspace", "artifact_id", "sha256_payload", "bytes_written", "evidence_complete", "error",
    ]
    params = inspect.signature(RunController.__init__).parameters
    assert list(params)[-1] == "workspace_recreator" and params["workspace_recreator"].default is None
    assert {s.name: s.value for s in PatchFailureStage} == {
        "ENTRY_GATE": "entry_gate", "ESTABLISH": "establish", "APPLY": "apply",
        "POSTCONDITION": "postcondition", "ADVANCE": "advance",
    }


def test_recreation_can_continue_is_false_until_d5_integration() -> None:
    assert _build()._recreation_can_continue() is False


# --- Q1 classification: every stage x code x intent -------------------------

_LEGAL = {
    (PatchFailureStage.APPLY, ErrorCode.PATCH_APPLICATION_FAILED),
    (PatchFailureStage.ADVANCE, ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED),
    (PatchFailureStage.ADVANCE, ErrorCode.CHECKPOINT_REF_OPERATION_FAILED),
    (PatchFailureStage.ENTRY_GATE, ErrorCode.WORKSPACE_ENTRY_GATE_FAILED),
}


@pytest.mark.parametrize(
    "stage, code, intent",
    list(itertools.product([None, *PatchFailureStage], [None, *ErrorCode], CheckpointIntent)),
    ids=lambda v: getattr(v, "value", "none"),
)
def test_q1_classification_matrix(stage, code, intent) -> None:
    expected = code if intent is CheckpointIntent.PRESENT and (stage, code) in _LEGAL else None
    assert classify_recreation_trigger(stage, code, intent) is expected


def test_q1_never_recreates_an_establish_failure_even_with_the_advance_codes() -> None:
    for code in (ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED, ErrorCode.CHECKPOINT_REF_OPERATION_FAILED):
        for intent in CheckpointIntent:
            assert classify_recreation_trigger(PatchFailureStage.ESTABLISH, code, intent) is None


# --- With the predicate False (production): nothing changes ------------------


@pytest.mark.parametrize("with_recreator", [False, True])
def test_production_path_never_calls_the_recreator(with_recreator) -> None:
    recreator = FakeRecreator(result=_result(O.UNCONFIRMED))
    controller = _build(recreator=recreator if with_recreator else None)
    finished = _run(controller)
    assert recreator.begin_calls == [] and recreator.recreate_calls == []
    assert _recreation_events(controller) == []
    assert finished.error.code is ErrorCode.PATCH_APPLICATION_FAILED


@pytest.mark.parametrize(
    "applier",
    [lambda: FakePatchApplier(), lambda: FakePatchApplier(True), _apply_failure],
    ids=["success", "validation-failure", "application-failure"],
)
def test_runs_are_identical_with_and_without_an_unused_recreator(applier) -> None:
    without = _build(patch_applier=applier())
    with_rec = _build(patch_applier=applier(), recreator=FakeRecreator(result=_result(O.UNCONFIRMED)))
    a, b = _run(without), _run(with_rec)
    assert _shape(without) == _shape(with_rec)
    assert (a.terminal_reason, a.error) == (b.terminal_reason, b.error)


# --- Routing (seam enabled) ---------------------------------------------------


def _session_present() -> FakeCheckpointSession:
    session = FakeCheckpointSession()
    session.intent = CheckpointIntent.PRESENT
    session.accepted_sha = _H0
    return session


def _advance_unchanged(reason: CheckpointRefFailure) -> tuple[FakePatchApplier, FakeCheckpointSession]:
    session = FakeCheckpointSession()
    session.advance_error = CheckpointRefError(reason, "forced", outcome=MutationOutcome.UNCHANGED)
    return FakePatchApplier(), session


def _gate_failure():
    workspace = FakeWorkspace(initial_commit=_H0)
    workspace.entry_gate_error = RuntimeError("dirty")
    return dict(patch_applier=FakePatchApplier(), workspace=workspace, session=_session_present())


@pytest.mark.parametrize(
    "setup, trigger",
    [
        (lambda: dict(patch_applier=_apply_failure()), ErrorCode.PATCH_APPLICATION_FAILED),
        (lambda: dict(zip(("patch_applier", "session"), _advance_unchanged(CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED))),
         ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED),
        (lambda: dict(zip(("patch_applier", "session"), _advance_unchanged(CheckpointRefFailure.GIT_COMMAND_TIMEOUT))),
         ErrorCode.CHECKPOINT_REF_OPERATION_FAILED),
        (_gate_failure, ErrorCode.WORKSPACE_ENTRY_GATE_FAILED),
    ],
    ids=["apply", "advance-cas", "advance-operation", "entry-gate"],
)
def test_every_q1_candidate_reaches_the_recreator(seam_enabled, setup, trigger) -> None:
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, **setup())
    _run(controller)
    assert recreator.begin_calls == [{"trigger_code": trigger, "checkpoint_id": _H0}]
    assert _recreation_events(controller), "expected recreation events"
    started = _recreation_events(controller)[0]
    assert (started.trigger_code, started.checkpoint_id, started.recreation_sequence) == (trigger, _H0, 1)


def _establish_failure():
    session = FakeCheckpointSession()
    session.establish_error = CheckpointRefError(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "forced", outcome=MutationOutcome.UNCHANGED
    )
    return dict(patch_applier=FakePatchApplier(), session=session)


def _gate_failure_while_absent():
    workspace = FakeWorkspace(initial_commit=_H0)
    workspace.entry_gate_error = RuntimeError("dirty")
    return dict(patch_applier=FakePatchApplier(), workspace=workspace)


def _advance_unknown():
    session = FakeCheckpointSession()
    session.advance_error = CheckpointRefError(
        CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN, "forced", outcome=MutationOutcome.UNKNOWN
    )
    return dict(patch_applier=FakePatchApplier(), session=session)


@pytest.mark.parametrize(
    "setup",
    [
        lambda: dict(patch_applier=FakePatchApplier(True)),
        lambda: dict(patch_applier=FakePatchApplier(True, failure_code=ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE)),
        lambda: dict(patch_applier=FakePatchApplier(True, failure_code=ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE)),
        lambda: dict(patch_applier=FakePatchApplier(changed_paths=("unrelated_file.py",))),
        _establish_failure,
        _gate_failure_while_absent,
        _advance_unknown,
    ],
    ids=["validation", "substrate", "objects", "postcondition", "establish", "gate-absent", "advance-unknown"],
)
def test_non_candidates_never_reach_the_recreator(seam_enabled, setup) -> None:
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, **setup())
    reference = _build(**setup())
    finished, expected = _run(controller), _run(reference)
    assert recreator.begin_calls == [] and _recreation_events(controller) == []
    assert _shape(controller) == _shape(reference)
    assert (finished.terminal_reason, finished.error) == (expected.terminal_reason, expected.error)


def test_no_recreator_means_no_attempt_even_with_the_seam_enabled(seam_enabled) -> None:
    controller, reference = _build(), _build()
    finished = _run(controller)
    monkey_free = _run(reference)
    assert _recreation_events(controller) == [] and _shape(controller) == _shape(reference)
    assert finished.error.code is ErrorCode.PATCH_APPLICATION_FAILED == monkey_free.error.code


def test_an_unconfirmed_verifier_cleanup_prevents_recreation(seam_enabled) -> None:
    holder = {}

    class _FlagThenFail(FakePatchApplier):
        def apply(self, *args, **kwargs):
            holder["c"]._any_verifier_cleanup_unconfirmed = True
            return super().apply(*args, **kwargs)

    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, patch_applier=_FlagThenFail(True, failure_code=ErrorCode.PATCH_APPLICATION_FAILED))
    holder["c"] = controller
    _run(controller)
    assert recreator.begin_calls == [] and _recreation_events(controller) == []


def test_q2_q6_refusal_is_not_an_attempt(seam_enabled) -> None:
    recreator = FakeRecreator(sequence=None, result=_result(O.DISPOSAL_FAILED))
    workspace, session, owner = FakeWorkspace(initial_commit=_H0), FakeCheckpointSession(), FakeLifecycleOwnerPublisher()
    controller = _build(recreator=recreator, workspace=workspace, session=session, owner=owner)
    finished = _run(controller)
    assert len(recreator.begin_calls) == 1 and recreator.recreate_calls == []
    assert _recreation_events(controller) == []
    failed_tool = [e for e in controller.log.events if isinstance(e, events.ToolCompleted) and not e.success][-1]
    assert finished.error == failed_tool.error  # the triggering error, same occurrence
    assert workspace.disposed and session.delete_calls == 1 and owner.calls[-1] == "complete"


# --- Outcomes ---------------------------------------------------------------


def _run_outcome(outcome, *, adopt=True, on_recreate=None, owner=None, sink=None):
    old = FakeWorkspace(initial_commit=_H0, path=Path("/fake/n"))
    new = FakeWorkspace(initial_commit=_NEXT, path=Path("/fake/n1"))
    adopts = outcome in (O.RECREATED, O.ADOPTED_THEN_FAILED)
    recreator = FakeRecreator(result=_result(outcome, new if adopts and adopt else None), on_recreate=on_recreate)
    session = FakeCheckpointSession()
    sink = sink or FakeEvidenceSink()
    owner = owner or FakeLifecycleOwnerPublisher()
    controller = _build(recreator=recreator, workspace=old, session=session, sink=sink, owner=owner)
    finished = _run(controller)
    return controller, finished, recreator, old, new, session, sink, owner


def test_event_order_and_fields_for_a_failed_outcome(seam_enabled) -> None:
    def recreate_check():
        assert type(controller_holder["c"].log.events[-1]) is events.WorkspaceRecreationStarted

    controller_holder = {}
    old = FakeWorkspace(initial_commit=_H0)
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED), on_recreate=recreate_check)
    controller = _build(recreator=recreator, workspace=old)
    controller_holder["c"] = controller
    finished = _run(controller)
    names = _types(controller)
    failed = max(i for i, e in enumerate(controller.log.events) if isinstance(e, events.ToolCompleted))
    assert names[failed + 1: failed + 3] == ["WorkspaceRecreationStarted", "WorkspaceRecreationFinished"]
    assert names.index("EvidenceCaptured") > failed + 2 and names[-1] == "RunFinished"
    assert len(_recreation_events(controller)) == 2, "expected one Started and one Finished"
    started, done = _recreation_events(controller)
    assert started.state is done.state is domain.RunState.EXECUTE
    assert (done.recreation_sequence, done.outcome) == (1, O.DISPOSAL_FAILED)
    assert done.error == recreator.result.error == finished.error
    assert recreator.recreate_calls == [1]
    events.validate_recreation_trace(controller.log.events)


@pytest.mark.parametrize("outcome", [o for o in O if o is not O.RECREATED], ids=lambda o: o.value)
def test_every_failed_outcome_ends_the_run_through_its_finished_error(seam_enabled, outcome) -> None:
    controller, finished, recreator, old, new, session, sink, owner = _run_outcome(outcome)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    done = _last_recreation_event(controller)
    expected = {
        O.UNCONFIRMED: ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED,
    }.get(outcome, done.error.code)
    assert finished.error.code is expected
    events.validate_recreation_trace(controller.log.events)


def test_evidence_not_confirmed_keeps_and_disposes_incarnation_n(seam_enabled) -> None:
    controller, finished, _, old, new, session, sink, owner = _run_outcome(O.EVIDENCE_NOT_CONFIRMED)
    assert old.disposed and not new.disposed and session.delete_calls == 1 and owner.calls[-1] == "complete"
    assert sink.last_call_kwargs["worktree_path"] == Path("/fake/n")
    assert finished.error.code is ErrorCode.EVIDENCE_ARTIFACT_COLLISION


def test_disposal_failed_leaves_n_current_for_ordinary_teardown(seam_enabled) -> None:
    controller, finished, _, old, new, session, sink, owner = _run_outcome(O.DISPOSAL_FAILED)
    assert controller._workspace is old and old.disposed and not new.disposed
    assert finished.error.code is ErrorCode.WORKSPACE_RECREATION_FAILED


def test_unconfirmed_skips_disposal_ref_deletion_and_complete(seam_enabled) -> None:
    controller, finished, _, old, new, session, sink, owner = _run_outcome(O.UNCONFIRMED)
    assert controller._recreation_cleanup_unconfirmed is True
    assert not old.disposed and not old.preserved and not new.disposed
    assert session.delete_calls == 0
    assert "complete" not in owner.calls and owner.calls[-1] == "begin_cleanup"
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


@pytest.mark.parametrize("outcome", [O.DISPOSAL_FAILED, O.FAILED_NOTHING_OWNED, O.EVIDENCE_NOT_CONFIRMED, O.ADOPTED_THEN_FAILED])
def test_only_unconfirmed_sets_the_cleanup_flag(seam_enabled, outcome) -> None:
    controller = _run_outcome(outcome)[0]
    assert controller._recreation_cleanup_unconfirmed is False


def test_failed_nothing_owned_with_no_worktree_uses_the_fixed_fallback(seam_enabled) -> None:
    holder = {}

    def dispose_n():
        holder["old"].path = None
        holder["old"].disposed = True

    old = FakeWorkspace(initial_commit=_H0)
    holder["old"] = old
    sink, session, owner = FakeEvidenceSink(), FakeCheckpointSession(), FakeLifecycleOwnerPublisher()
    recreator = FakeRecreator(result=_result(O.FAILED_NOTHING_OWNED), on_recreate=dispose_n)
    controller = _build(recreator=recreator, workspace=old, sink=sink, session=session, owner=owner)
    finished = _run(controller)
    assert sink.capture_calls == 0
    captured = [e for e in controller.log.events if isinstance(e, events.EvidenceCaptured)][-1]
    assert captured.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert captured.error.message == "no worktree exists to capture terminal evidence from"
    assert session.delete_calls == 1 and owner.calls[-1] == "complete"
    assert finished.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED  # unchanged precedence over the original


def test_a_missing_path_without_a_recreation_attempt_keeps_the_old_evidence_path() -> None:
    workspace = FakeWorkspace(initial_commit=_H0)
    workspace.path = None
    sink = FakeEvidenceSink()
    _run(_build(workspace=workspace, sink=sink))
    assert sink.capture_calls == 1  # the fallback is reachable only after an attempt


def test_adopted_then_failed_tears_down_the_new_incarnation_with_h0_evidence(seam_enabled) -> None:
    controller, finished, _, old, new, session, sink, owner = _run_outcome(O.ADOPTED_THEN_FAILED)
    assert controller._workspace is new and new.disposed and not old.disposed
    assert sink.last_call_kwargs["worktree_path"] == Path("/fake/n1")
    assert sink.last_call_kwargs["initial_commit"] == _H0 != new.initial_commit
    assert finished.error.code is ErrorCode.WORKSPACE_RECREATION_FAILED


def test_recreated_cannot_continue_before_d5_integration(seam_enabled) -> None:
    controller, finished, _, old, new, session, sink, owner = _run_outcome(O.RECREATED)
    assert controller._workspace is new and new.disposed
    assert _last_recreation_event(controller).outcome is O.RECREATED
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert sink.last_call_kwargs["initial_commit"] == _H0
    events.validate_recreation_trace(controller.log.events)


def test_h0_is_the_run_origin_not_the_current_incarnation(seam_enabled) -> None:
    controller = _run_outcome(O.ADOPTED_THEN_FAILED)[0]
    assert controller._origin_commit == _H0 and controller._workspace.initial_commit == _NEXT


@pytest.mark.parametrize(
    "outcome, workspace",
    [(O.DISPOSAL_FAILED, "new"), (O.RECREATED, None), (O.ADOPTED_THEN_FAILED, None)],
)
def test_a_malformed_result_is_refused_before_bookkeeping_or_finished(seam_enabled, outcome, workspace) -> None:
    old = FakeWorkspace(initial_commit=_H0)
    new = FakeWorkspace(initial_commit=_NEXT)
    result = dataclasses.replace(_result(outcome), workspace=new if workspace == "new" else None)
    recreator = FakeRecreator(result=result)
    controller = _build(recreator=recreator, workspace=old)
    finished = _run(controller)
    assert recreator.recreate_calls == [1]
    assert [type(e).__name__ for e in _recreation_events(controller)] == ["WorkspaceRecreationStarted"]
    assert controller._workspace is old and old.disposed
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE


@pytest.mark.parametrize("bad", ["not a result", None])
def test_a_non_result_is_refused(seam_enabled, bad) -> None:
    controller = _build(recreator=FakeRecreator(result=bad))
    assert _run(controller).error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert [type(e).__name__ for e in _recreation_events(controller)] == ["WorkspaceRecreationStarted"]


# --- Emission failures (section 6.1) ------------------------------------------


def _fail_emission(controller, event_type, exc=None):
    real = controller.log.append
    attempts = {"n": 0}

    def append(event):
        if isinstance(event, event_type):
            attempts["n"] += 1
            raise exc or RuntimeError("emission failed")
        real(event)

    controller.log.append = append
    return attempts


def test_a_failed_started_emission_stops_before_phase_e(seam_enabled) -> None:
    old, session, owner = FakeWorkspace(initial_commit=_H0), FakeCheckpointSession(), FakeLifecycleOwnerPublisher()
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, workspace=old, session=session, owner=owner)
    attempts = _fail_emission(controller, events.WorkspaceRecreationStarted)
    finished = _run(controller)
    assert attempts["n"] == 1 and len(recreator.begin_calls) == 1 and recreator.recreate_calls == []
    assert _recreation_events(controller) == []
    assert old.disposed and session.delete_calls == 1 and owner.calls[-1] == "complete"
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    events.validate_recreation_trace(controller.log.events)


@pytest.mark.parametrize("outcome", list(O), ids=lambda o: o.value)
def test_a_failed_finished_emission_never_repeats_and_tears_down_the_current_objects(seam_enabled, outcome) -> None:
    old = FakeWorkspace(initial_commit=_H0, path=Path("/fake/n"))
    new = FakeWorkspace(initial_commit=_NEXT, path=Path("/fake/n1"))
    adopts = outcome in (O.RECREATED, O.ADOPTED_THEN_FAILED)
    recreator = FakeRecreator(result=_result(outcome, new if adopts else None))
    session, owner = FakeCheckpointSession(), FakeLifecycleOwnerPublisher()
    controller = _build(recreator=recreator, workspace=old, session=session, owner=owner)
    attempts = _fail_emission(controller, events.WorkspaceRecreationFinished)
    finished = _run(controller)
    assert attempts["n"] == 1 and len(recreator.begin_calls) == 1 and recreator.recreate_calls == [1]
    assert [type(e).__name__ for e in _recreation_events(controller)] == ["WorkspaceRecreationStarted"]
    current = new if adopts else old
    assert controller._workspace is current
    if outcome is O.UNCONFIRMED:
        assert not old.disposed and session.delete_calls == 0 and "complete" not in owner.calls
        assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    else:
        assert current.disposed and session.delete_calls == 1
        assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    if adopts:
        assert not old.disposed
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    events.validate_recreation_trace(controller.log.events)


def test_a_base_exception_at_finished_propagates_after_bookkeeping(seam_enabled) -> None:
    old, new = FakeWorkspace(initial_commit=_H0), FakeWorkspace(initial_commit=_NEXT)
    recreator = FakeRecreator(result=_result(O.ADOPTED_THEN_FAILED, new))
    controller = _build(recreator=recreator, workspace=old)
    interrupt = KeyboardInterrupt()
    _fail_emission(controller, events.WorkspaceRecreationFinished, interrupt)
    caught = None
    try:
        controller.run()
    except BaseException as exc:  # noqa: BLE001 - the exact instance is asserted below
        caught = exc
    assert caught is interrupt
    assert controller._workspace is new and recreator.recreate_calls == [1]


class _StartedInterrupt(BaseException):
    """A non-`Exception` raised while emitting WorkspaceRecreationStarted."""


_SENTINEL = "SENTINEL-started-interrupt-text"


@pytest.mark.parametrize("where", ["append", "construction"])
def test_a_base_exception_at_started_is_primary_and_stops_before_phase_e(seam_enabled, monkeypatch, where) -> None:
    """Amendment 20 section 6.1: the Started emission fails after Q1 and
    `begin()` (k consumed), before Phase E or any lifecycle mutation. The
    exact instance propagates; the controller's own teardown never runs, so
    cleanup stays with the composition's raise path."""
    interrupt = _StartedInterrupt(_SENTINEL)
    old, session, owner, sink = (
        FakeWorkspace(initial_commit=_H0), FakeCheckpointSession(), FakeLifecycleOwnerPublisher(), FakeEvidenceSink()
    )
    recreator = FakeRecreator(sequence=7, result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, workspace=old, session=session, owner=owner, sink=sink)
    attempts = {"n": 0}
    seen = {}
    if where == "append":
        real_append = controller.log.append

        def append(event):
            if isinstance(event, events.WorkspaceRecreationStarted):
                attempts["n"] += 1
                seen["attempted_flag"] = controller._recreation_attempted
                raise interrupt
            real_append(event)

        controller.log.append = append
    else:
        real_started = events.WorkspaceRecreationStarted

        def construct(**kwargs):
            attempts["n"] += 1
            seen["attempted_flag"] = controller._recreation_attempted
            seen["kwargs"] = dict(kwargs)
            raise interrupt

        monkeypatch.setattr(events, "WorkspaceRecreationStarted", construct)

    caught = None
    try:
        controller.run()
    except BaseException as exc:  # noqa: BLE001 - the exact instance is asserted below
        caught = exc
    if where == "construction":
        monkeypatch.setattr(events, "WorkspaceRecreationStarted", real_started)

    assert caught is interrupt
    assert attempts["n"] == 1
    assert recreator.begin_calls == [{"trigger_code": ErrorCode.PATCH_APPLICATION_FAILED, "checkpoint_id": _H0}]
    assert seen["attempted_flag"] is True and controller._recreation_attempted is True
    assert recreator.recreate_calls == []
    assert _recreation_events(controller) == []
    assert not any(isinstance(e, events.RunFinished) for e in controller.log.events)
    assert controller._termination_started is False  # no controller teardown: the raise path owns cleanup
    assert not old.disposed and not old.preserved
    assert session.delete_calls == 0 and sink.capture_calls == 0
    assert owner.calls == ["activate"]
    assert controller._workspace is old and controller._recreation_cleanup_unconfirmed is False
    assert all(_SENTINEL not in repr(e) for e in controller.log.events)
    if where == "construction":
        assert seen["kwargs"]["recreation_sequence"] == 7


# --- Cancellation and existing precedence -------------------------------------


def test_cancellation_before_any_patch_never_reaches_the_seam(seam_enabled) -> None:
    token = _FlagToken()
    token.cancelled = True
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    finished = _run(_build(recreator=recreator, cancellation=token))
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED
    assert recreator.begin_calls == []


def test_cancellation_during_recreation_does_not_override_the_outcome(seam_enabled) -> None:
    token = _FlagToken()
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED), on_recreate=lambda: setattr(token, "cancelled", True))
    finished = _run(_build(recreator=recreator, cancellation=token))
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is ErrorCode.WORKSPACE_RECREATION_FAILED


def test_a_teardown_cleanup_failure_still_dominates_a_recreation_error(seam_enabled) -> None:
    old = FakeWorkspace(initial_commit=_H0)
    old.dispose_error = RuntimeError("cannot dispose")
    recreator = FakeRecreator(result=_result(O.DISPOSAL_FAILED))
    controller = _build(recreator=recreator, workspace=old)
    finished = _run(controller)
    assert [e.outcome for e in _recreation_events(controller)[1:]] == [O.DISPOSAL_FAILED]
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


def test_the_controller_module_creates_no_reservation_artifact_or_worktree() -> None:
    """Step 6 boundary: the controller mints no sequence, captures no
    recreation artifact, reserves nothing and materializes nothing."""
    source = inspect.getsource(ctl)
    for forbidden in ("reserve_worktree_leaf", "GitWorktree(", "materialize_commit", "object_format", "recreation_sequence=None"):
        assert forbidden not in source
    capture = source[source.index("self._evidence_sink.capture("):]
    assert "recreation_sequence" not in capture[: capture.index(")")]
