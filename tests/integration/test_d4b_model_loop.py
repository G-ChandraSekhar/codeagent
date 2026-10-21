"""Ledger D4b: the model-driven EXPLORE turn loop (D4 plan revisions 5 and
6, and the D4b readiness addendum's J1-J4, P1 and patch-seam rows).

Fakes only: `ScriptedModel` turns, `FakeRepositoryTools`, fake approval,
verifier, patch applier, workspace, checkpoint session and evidence sink.
Every run's trace is checked with the strict pairing helpers unless a test
says otherwise.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pathlib

import pytest

from codeagent import _lifecycle_run, controller as controller_module, domain, events
from codeagent.cancellation import CLEANUP_FAILURES_ATTRIBUTE, CancellationSource
from codeagent.checkpoint_ref import CheckpointRefError, CheckpointRefFailure, MutationOutcome
from codeagent.controller import (
    ApprovalResponse,
    PlanProposal,
    RunConfig,
    RunController,
    VerificationResult,
)
from codeagent.errors import ErrorCode
from codeagent.events import ModelResponseStatus as S
from codeagent.model_protocol import CONTEXT_CAP_BYTES, FeedbackKind, ModelTurn
from codeagent.patch import GitPatchApplier
from codeagent.report import build_report, render_text
from codeagent.tool_schema import (
    PatchOperation,
    ProposedChange,
    ProposePlanArgs,
    compute_digests,
    parse_tool_arguments,
    ToolCleanupUnconfirmedError,
    ToolOutcome,
    redact_arguments,
)
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakePatchApplier,
    FakeRepositoryTools,
    FakeVerifier,
    FakeWorkspace,
    SteppingClock,
    make_plan,
)
from tests.support.scripted_model import (
    ScriptedModel,
    last_result,
    list_directory_call,
    propose_plan_call,
    read_file_call,
    search_text_call,
    tool_call,
    turn,
)
from tests.support.trace_assertions import assert_model_pairing, assert_tool_pairing

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "codeagent"
PLAN = make_plan()
_UNCLASSIFIED = "the run terminated due to an unanticipated internal error"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class ManualClock:
    """`now()` steps 1 ms; `monotonic()` only moves when a test advances it."""

    def __init__(self) -> None:
        self._stepping = SteppingClock()
        self.t = 0.0

    def now(self):
        return self._stepping.now()

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _build(
    model,
    *,
    tools=None,
    approval=None,
    verifier=None,
    applier=None,
    workspace=None,
    session=None,
    clock=None,
    cancellation=None,
    **config,
) -> RunController:
    cfg = dict(
        run_id="r-d4b",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id="c" * 32,
    )
    cfg.update(config)
    kwargs = {} if cancellation is None else {"cancellation": cancellation}
    return RunController(
        RunConfig(**cfg),
        model,
        approval if approval is not None else FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verifier if verifier is not None else FakeVerifier((events.VerificationOutcome.PASSED,)),
        applier if applier is not None else FakePatchApplier(),
        tools if tools is not None else FakeRepositoryTools("    job.retry_count += 1\n"),
        workspace if workspace is not None else FakeWorkspace(),
        session if session is not None else FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=clock if clock is not None else SteppingClock(),
        **kwargs,
    )


def _run(controller: RunController, allow_unpaired=()) -> events.RunFinished:
    finished = controller.run()
    assert_model_pairing(controller.log.events, allow_unpaired)
    assert_tool_pairing(controller.log.events)
    return finished


def _of(controller, cls):
    return [e for e in controller.log.events if isinstance(e, cls)]


def _last_read_id(context) -> str:
    body = last_result(context, "read_file")
    assert body is not None and body["ok"]
    return body["tool_call_id"]


def _cite_last_read(plan: PlanProposal = PLAN):
    """A scripted step proposing `plan`, citing the newest successful read."""
    return lambda ctx: turn(propose_plan_call(dataclasses.replace(plan, evidence_refs=(_last_read_id(ctx),))))


def _read_then_plan(plan: PlanProposal = PLAN) -> list:
    return [turn(read_file_call(plan.change.path)), _cite_last_read(plan)]


def _scan(controller, *sentinels: str, allow: dict[str, set[str]] | None = None) -> None:
    """No sentinel appears in any event field, except where `allow` names
    the exact (event type, field) pairs that may hold it."""
    allow = allow or {}
    for event in controller.log.events:
        for name, value in vars(event).items():
            text = repr(value)
            for sentinel in sentinels:
                if sentinel in text and f"{type(event).__name__}.{name}" not in allow.get(sentinel, set()):
                    raise AssertionError(f"{sentinel!r} leaked into {type(event).__name__}.{name}")


def _transcript_kinds(controller) -> list[FeedbackKind]:
    return [item.kind for item in controller._transcript]


def _sites(controller) -> list[str]:
    return [site for site, _ in controller._cancellation_sites]


# --------------------------------------------------------------------------
# Happy path, model identity and latency (A6), redaction (A5)
# --------------------------------------------------------------------------


def test_a6_read_then_plan_passes_with_the_real_model_name_and_latency():
    controller = _build(ScriptedModel(_read_then_plan()))
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
    starts, received = _of(controller, events.ModelRequestStarted), _of(controller, events.ModelResponseReceived)
    assert len(starts) == len(received) == 2
    assert {e.model_name for e in starts + received} == {"fake-model-v1"}
    assert all(e.latency_seconds == pytest.approx(0.001) for e in received)
    assert [e.request_id for e in starts] == ["r-d4b-req-0-1", "r-d4b-req-0-2"]
    assert [e.tool_call_count for e in received] == [1, 1]


def test_a5_redacted_arguments_are_canonical_for_every_model_tool():
    plan_step = _cite_last_read()
    model = ScriptedModel(
        [
            turn(list_directory_call("jobs"), search_text_call("retry", "jobs", limit=5), read_file_call("jobs/worker.py", 1, 3)),
            plan_step,
        ]
    )
    controller = _build(model)
    _run(controller)
    requests = [e for e in _of(controller, events.ToolRequested) if e.tool is not domain.ToolName.APPLY_PATCH]
    assert [e.tool for e in requests] == [
        domain.ToolName.LIST_DIRECTORY,
        domain.ToolName.SEARCH_TEXT,
        domain.ToolName.READ_FILE,
        domain.ToolName.PROPOSE_PLAN,
    ]
    calls = [call for returned in model.returned for call in returned.calls]
    assert len(calls) == len(requests)
    for event, call in zip(requests, calls):
        assert event.redacted_arguments_json == redact_arguments(parse_tool_arguments(event.tool, call.arguments_json))
        assert event.provider_call_id == call.provider_call_id.decode()


# --------------------------------------------------------------------------
# §7.1a: the three outcome events (R38) and pairing
# --------------------------------------------------------------------------


class SentinelLookalike:
    """Looks like a turn but is not one; its type name must never leak."""

    status = S.COMPLETED
    model_name = b"m"
    response_id = b"r"
    calls = ()


@pytest.mark.parametrize("returned", [None, 7, {"status": "completed"}, SentinelLookalike()])
def test_r38_a_returned_non_turn_is_rejected_and_shares_its_error(returned):
    controller = _build(ScriptedModel([returned]))
    finished = _run(controller)
    (rejected,) = _of(controller, events.ModelResponseRejected)
    assert rejected.rejection_reason is events.ResponseRejectionReason.NOT_A_MODEL_TURN
    assert rejected.error.code is ErrorCode.MODEL_RESPONSE_MALFORMED
    assert rejected.error.message == "model client returned an object that is not a model turn"
    assert not _of(controller, events.ModelResponseReceived) and not _of(controller, events.ModelRequestFailed)
    assert not _of(controller, events.ToolRequested)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is rejected.error  # P1: no teardown override applies
    _scan(controller, "SentinelLookalike", "status")
    build_report(controller.log.events)


@pytest.mark.parametrize("status", ["completed", object()])
def test_r38_an_unrecognized_status_is_rejected(status):
    controller = _build(ScriptedModel([dataclasses.replace(turn(read_file_call("a.py")), status=status)]))
    finished = _run(controller)
    (rejected,) = _of(controller, events.ModelResponseRejected)
    assert rejected.rejection_reason is events.ResponseRejectionReason.STATUS_UNRECOGNIZED
    assert rejected.error.message == "model client returned a turn with an unrecognized status"
    assert finished.error is rejected.error
    assert not _of(controller, events.ModelResponseReceived)


def test_p1_a_cleanup_override_replaces_the_rejection_error():
    workspace = FakeWorkspace()
    workspace.dispose_error = RuntimeError("dispose failed")
    controller = _build(ScriptedModel([None]), workspace=workspace)
    finished = _run(controller)
    (rejected,) = _of(controller, events.ModelResponseRejected)
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is not rejected.error
    assert finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED


def test_r38_respond_raising_an_exception_is_failed_then_unclassified():
    controller = _build(ScriptedModel([RuntimeError("SENTINEL_PROVIDER_TEXT")]))
    finished = _run(controller)
    (failed,) = _of(controller, events.ModelRequestFailed)
    assert failed.error.code is ErrorCode.MODEL_PROVIDER_REQUEST_FAILED
    assert failed.error.message == "model request raised before returning a response"
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE and finished.error.message == _UNCLASSIFIED
    _scan(controller, "SENTINEL_PROVIDER_TEXT", "RuntimeError")
    # J2: a call that raised gets no C25 decision.
    assert [site for site, _, _ in controller._post_return_decisions] == []


def test_r38_respond_raising_an_interrupt_propagates_the_exact_instance():
    interrupt = KeyboardInterrupt()
    controller = _build(ScriptedModel([interrupt]))
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt
    assert len(_of(controller, events.ModelRequestFailed)) == 1
    assert_model_pairing(controller.log.events)
    assert controller._post_return_decisions == []
    assert not _of(controller, events.RunFinished)


def test_r38_an_emission_failure_during_an_interrupt_is_attached():
    interrupt = KeyboardInterrupt()
    controller = _build(ScriptedModel([interrupt]))
    boom = RuntimeError("log broken")
    real = controller._emit

    def emit(event):
        if isinstance(event, events.ModelRequestFailed):
            raise boom
        real(event)

    controller._emit = emit
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt
    assert getattr(interrupt, CLEANUP_FAILURES_ATTRIBUTE) == (boom,)


# --------------------------------------------------------------------------
# J1: strict pairing
# --------------------------------------------------------------------------


def _started(request_id: str, sequence: int = 0) -> events.ModelRequestStarted:
    return events.ModelRequestStarted(
        run_id="r",
        sequence=sequence,
        timestamp=SteppingClock().now(),
        state=domain.RunState.EXPLORE,
        iteration=0,
        request_id=request_id,
        model_name="m",
    )


def test_j1_strict_pairing_rejects_an_unexplained_missing_pair_even_when_unclassified():
    controller = _build(ScriptedModel([RuntimeError("x")]))
    finished = controller.run()
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    trace = [e for e in controller.log.events if not isinstance(e, events.ModelRequestFailed)]
    with pytest.raises(AssertionError):
        assert_model_pairing(trace)


def _fault_once(controller, cls):
    """Makes the first emission of `cls` fail once; records the request id."""
    real = controller._emit
    seen: list[str] = []

    def emit(event):
        if isinstance(event, cls) and not seen:
            seen.append(event.request_id)
            raise RuntimeError("injected emission fault")
        real(event)

    controller._emit = emit
    return seen


def test_j1_one_shot_emission_fault_is_unclassified_and_names_its_request():
    controller = _build(ScriptedModel(_read_then_plan()))
    seen = _fault_once(controller, events.ModelResponseReceived)
    finished = controller.run()
    assert seen == ["r-d4b-req-0-1"]  # the fault hit this request's outcome event
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert_model_pairing(controller.log.events, allow_unpaired={"r-d4b-req-0-1"})
    with pytest.raises(AssertionError):
        assert_model_pairing(controller.log.events)
    with pytest.raises(AssertionError):
        assert_model_pairing(controller.log.events, allow_unpaired={"r-d4b-req-0-2"})


def test_j1_a_failure_inside_terminate_propagates_without_run_finished():
    workspace = FakeWorkspace()
    controller = _build(ScriptedModel([None]), workspace=workspace)
    boom = RuntimeError("terminal emission failed")
    real = controller._emit

    def emit(event):
        if isinstance(event, events.RunFinished):
            raise boom
        real(event)

    controller._emit = emit
    with pytest.raises(RuntimeError) as info:
        controller.run()
    assert info.value is boom
    assert not _of(controller, events.RunFinished)


def test_r15_tool_pairing_helper_self_test():
    good = _build(ScriptedModel(_read_then_plan()))
    good.run()
    assert_tool_pairing(good.log.events)
    missing = [e for e in good.log.events if not isinstance(e, events.ToolCompleted)]
    with pytest.raises(AssertionError):
        assert_tool_pairing(missing)
    with pytest.raises(AssertionError):
        assert_model_pairing([_started("a"), _started("b", 1)])


# --------------------------------------------------------------------------
# §7.1b precedence (R33, R39) and J2 decision records
# --------------------------------------------------------------------------

_BAD_NAME = b"bad name!"
_ROWS = {
    # name: (first turn, terminal without cancellation, error code, kind)
    "failed": (turn(status=S.FAILED), domain.TerminalReason.UNRECOVERABLE_ERROR, ErrorCode.MODEL_RESPONSE_FAILED_STATUS, "operational_failure_precedence"),
    "in_progress": (turn(status=S.IN_PROGRESS), domain.TerminalReason.UNRECOVERABLE_ERROR, ErrorCode.MODEL_RESPONSE_MALFORMED, "operational_failure_precedence"),
    "queued": (turn(status=S.QUEUED), domain.TerminalReason.UNRECOVERABLE_ERROR, ErrorCode.MODEL_RESPONSE_MALFORMED, "operational_failure_precedence"),
    "failed_bad_metadata": (turn(status=S.FAILED, model_name=_BAD_NAME), domain.TerminalReason.UNRECOVERABLE_ERROR, ErrorCode.MODEL_RESPONSE_MALFORMED, "operational_failure_precedence"),
    "incomplete_no_reason": (turn(status=S.INCOMPLETE), domain.TerminalReason.UNRECOVERABLE_ERROR, ErrorCode.MODEL_RESPONSE_MALFORMED, "operational_failure_precedence"),
    "incomplete_valid": (turn(status=S.INCOMPLETE, incomplete_reason=b"max_output_tokens"), domain.TerminalReason.MODEL_RESPONSE_INCOMPLETE, None, "checked"),
    "provider_cancelled": (turn(status=S.CANCELLED), domain.TerminalReason.CANCELLED, None, "checked"),
}


@pytest.mark.parametrize("row", sorted(_ROWS))
@pytest.mark.parametrize("pending", [False, True])
def test_r39_run_ending_rows_with_and_without_a_pending_cancellation(row, pending):
    first, reason, code, kind = _ROWS[row]
    source = CancellationSource()

    def step(ctx):
        if pending:
            source.request("t")
        return first

    controller = _build(ScriptedModel([step]), cancellation=source.token)
    finished = _run(controller)
    (received,) = _of(controller, events.ModelResponseReceived)
    assert received.status is first.status  # recorded exactly as received
    assert not _of(controller, events.ToolRequested)
    if row == "incomplete_valid" and pending:
        assert finished.terminal_reason is domain.TerminalReason.CANCELLED
    else:
        assert finished.terminal_reason is reason
    if code is None:
        assert finished.error is None
    else:
        assert finished.error.code is code
    assert [(s, k) for s, _, k in controller._post_return_decisions] == [("C25", kind)]
    consulted_at_c25 = "C25" in _sites(controller)
    assert consulted_at_c25 is (row == "incomplete_valid")
    if row == "failed_bad_metadata":
        assert received.model_name == "codeagent.invalid-model-name"
        _scan(controller, "bad name!")


@pytest.mark.parametrize("pending", [False, True])
def test_r39_rejected_envelope_outranks_a_pending_cancellation(pending):
    source = CancellationSource()

    def step(ctx):
        if pending:
            source.request("t")
        return object()

    controller = _build(ScriptedModel([step]), cancellation=source.token)
    finished = _run(controller)
    assert finished.error.code is ErrorCode.MODEL_RESPONSE_MALFORMED
    assert [(s, k) for s, _, k in controller._post_return_decisions] == [("C25", "operational_failure_precedence")]
    assert "C25" not in _sites(controller)


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_r39_completed_rows_yield_to_a_pending_cancellation(malformed, pending):
    source = CancellationSource()
    first = turn() if malformed else turn(read_file_call("jobs/worker.py"))

    def step(ctx):
        if pending:
            source.request("t")
        return first

    rest = _read_then_plan() if malformed else [_cite_last_read()]
    controller = _build(ScriptedModel([step, *rest]), cancellation=source.token)
    finished = _run(controller)
    if pending:
        assert finished.terminal_reason is domain.TerminalReason.CANCELLED and finished.error is None
        assert not _of(controller, events.ToolRequested)
        assert controller._transcript == []  # uncharged, no feedback
        assert _sites(controller)[-1] == "C25"
    else:
        assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
        assert (FeedbackKind.MALFORMED_RESPONSE in _transcript_kinds(controller)) is malformed
    assert controller._post_return_decisions[0][::2] == ("C25", "checked")


def test_j2_c5_once_per_pass_c24_for_later_requests_and_decision_records():
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    model = ScriptedModel(_read_then_plan() + _read_then_plan())
    controller = _build(model, verifier=verifier)
    finished = _run(controller)
    assert finished.iterations_used == 2
    sites = _sites(controller)
    assert sites.count("C5") == controller._passes_begun == 2
    starts = _of(controller, events.ModelRequestStarted)
    request_sites = [s for s, seq in controller._cancellation_sites if s in ("C5", "C24")]
    assert request_sites == ["C5", "C24", "C5", "C24"]
    assert [seq for s, seq in controller._cancellation_sites if s in ("C5", "C24")] == [e.sequence for e in starts]
    # Every returned model outcome reaches C25 exactly once, right after it.
    outcomes = _of(controller, events.ModelResponseReceived)
    c25 = [(seq, k) for s, seq, k in controller._post_return_decisions if s == "C25"]
    assert [seq for seq, _ in c25] == [e.sequence + 1 for e in outcomes]
    # Every returned tool outcome reaches C27 exactly once, after its ToolCompleted.
    completed = [e for e in _of(controller, events.ToolCompleted) if e.tool is not domain.ToolName.APPLY_PATCH]
    c27 = [seq for s, seq, k in controller._post_return_decisions if s == "C27"]
    assert c27 == [e.sequence + 1 for e in completed]
    # C26 precedes every model-requested triple.
    requested = [e for e in _of(controller, events.ToolRequested) if e.tool is not domain.ToolName.APPLY_PATCH]
    assert [seq for s, seq in controller._cancellation_sites if s == "C26"] == [e.sequence for e in requested]


def test_j2_c26_precedes_denied_triples_too():
    model = ScriptedModel([turn(read_file_call(".git/config"), read_file_call("jobs/worker.py")), _cite_last_read()])
    controller = _build(model)
    _run(controller)
    requested = [e for e in _of(controller, events.ToolRequested) if e.tool is not domain.ToolName.APPLY_PATCH]
    policy = {e.tool_call_id: e for e in _of(controller, events.PolicyDecisionRecorded)}
    assert not policy[requested[0].tool_call_id].allowed
    assert [seq for s, seq in controller._cancellation_sites if s == "C26"] == [e.sequence for e in requested]


def test_j2_consulted_site_inventory_is_c1_to_c5_and_c12_to_c27():
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    approval = FakeApprovalProvider(
        (domain.ApprovalDecision.REVISION_REQUESTED, domain.ApprovalDecision.APPROVED, domain.ApprovalDecision.APPROVED)
    )
    model = ScriptedModel(_read_then_plan() * 3)
    controller = _build(model, verifier=verifier, approval=approval)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    expected = {f"C{i}" for i in [*range(1, 6), *range(12, 28)]}
    assert set(_sites(controller)) == expected
    source = (SRC / "controller.py").read_text()
    for retired in ("C6", "C7", "C8", "C9", "C10", "C11"):
        assert f'"{retired}"' not in source


# --------------------------------------------------------------------------
# Raising tools (R25) and J2's no-C27 rule
# --------------------------------------------------------------------------


class _RaisingTools(FakeRepositoryTools):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    def read_file(self, args, should_stop):
        raise self.exc


def test_r25_a_raising_tool_completes_then_is_unclassified_without_a_c27_record():
    controller = _build(ScriptedModel([turn(read_file_call("a.py"))]), tools=_RaisingTools(RuntimeError("SENTINEL_TOOL")))
    finished = _run(controller)
    (completed,) = _of(controller, events.ToolCompleted)
    assert completed.error.message == "tool failed unexpectedly"
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert [s for s, _, _ in controller._post_return_decisions] == ["C25"]
    _scan(controller, "SENTINEL_TOOL")


def test_r25_an_interrupting_tool_completes_then_propagates_the_exact_instance():
    interrupt = KeyboardInterrupt()
    controller = _build(ScriptedModel([turn(read_file_call("a.py"))]), tools=_RaisingTools(interrupt))
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt
    (completed,) = _of(controller, events.ToolCompleted)
    assert completed.error.message == "tool interrupted"
    assert_tool_pairing(controller.log.events)
    assert [s for s, _, _ in controller._post_return_decisions] == ["C25"]


def test_r25_tool_completed_emission_failure_during_an_interrupt_is_attached():
    interrupt = KeyboardInterrupt()
    controller = _build(ScriptedModel([turn(read_file_call("a.py"))]), tools=_RaisingTools(interrupt))
    boom = RuntimeError("log broken")
    real = controller._emit

    def emit(event):
        if isinstance(event, events.ToolCompleted):
            raise boom
        real(event)

    controller._emit = emit
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt and getattr(interrupt, CLEANUP_FAILURES_ATTRIBUTE) == (boom,)


# --------------------------------------------------------------------------
# Malformed turns (A1, R29) and turn ordering (A2, A3, R5)
# --------------------------------------------------------------------------

_MALFORMED = {
    "zero_calls": (turn(), 1, "no_tool_calls"),
    "nine_calls": (turn(*[read_file_call("a.py") for _ in range(9)]), 9, "too_many_tool_calls"),
    "twelve_calls": (turn(*[read_file_call("a.py") for _ in range(12)]), 9, "too_many_tool_calls"),
    "unknown_name": (turn(tool_call("rm_rf", {"path": "a"})), 1, "tool_name_unknown"),
    "bad_json": (turn(tool_call("read_file", b"{not json")), 1, "arguments_invalid_json"),
    "duplicate_id": (
        turn(read_file_call("a.py", call_id=b"same"), read_file_call("b.py", call_id=b"same")),
        2,
        "provider_call_id_duplicate",
    ),
    "bad_provider_id": (turn(read_file_call("a.py", call_id=b"SENTINEL ID!")), 1, "provider_call_id_invalid"),
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED))
def test_a1_malformed_turns_are_charged_fed_back_and_the_run_continues(shape):
    bad, charge, reason = _MALFORMED[shape]
    model = ScriptedModel([bad, *_read_then_plan()])
    controller = _build(model)
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    first = _of(controller, events.ModelResponseReceived)[0]
    assert first.error.code is ErrorCode.MODEL_RESPONSE_MALFORMED
    assert f"reason={reason}" in first.error.message
    assert model.contexts[1].tool_calls_remaining == 25 - charge
    (item,) = [i for i in model.contexts[1].transcript if i.kind is FeedbackKind.MALFORMED_RESPONSE]
    assert set(item.body) == {"call_index", "reason"} and item.body["reason"] == reason
    # No ToolRequested from the malformed turn: the first is the later read.
    assert _of(controller, events.ToolRequested)[0].sequence > first.sequence + 1
    _scan(controller, "SENTINEL ID!", "rm_rf", "{not json")


def test_r29_a_valid_provider_id_is_audited_but_never_citable():
    model = ScriptedModel(
        [
            turn(read_file_call("jobs/worker.py", call_id=b"tc-0-1")),
            turn(_raw_plan_call("call_provider")),
            *_read_then_plan(),
        ]
    )
    # The first read's controller ID is also tc-0-1, so cite a provider-only id.
    controller = _build(model)
    _run(controller)
    first = _of(controller, events.ToolRequested)[0]
    assert first.provider_call_id == "tc-0-1" and first.tool_call_id == "tc-0-1"
    body = last_result(model.contexts[1], "read_file")
    assert body["provider_call_id"] == "tc-0-1"
    denied = [e for e in _of(controller, events.PolicyDecisionRecorded) if not e.allowed]
    assert denied[0].reason == "arguments_invalid:evidence_ref_invalid"


def test_a2_apply_patch_requested_in_explore_is_denied_and_non_terminal():
    model = ScriptedModel([turn(tool_call("apply_patch", {"path": "a.py"})), *_read_then_plan()])
    controller = _build(model)
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    first = controller.log.events[[type(e) for e in controller.log.events].index(events.ToolRequested):][:3]
    assert [type(e) for e in first] == [events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted]
    assert first[1].reason == "tool_not_legal_in_state" and not first[1].allowed
    assert first[2].error.code is ErrorCode.TOOL_INPUT_INVALID


def _second_turn_sequence(*second_calls):
    model = ScriptedModel([turn(read_file_call("jobs/worker.py")), lambda ctx: turn(*[c(ctx) for c in second_calls]), *_read_then_plan()])
    controller = _build(model)
    finished = _run(controller)
    return controller, finished


def _plan_call(ctx, plan=PLAN):
    return propose_plan_call(dataclasses.replace(plan, evidence_refs=(_last_read_id(ctx),)))


def _tools_in_pass(controller):
    return [
        (e.tool.value, p.allowed, p.reason)
        for e, p in zip(_of(controller, events.ToolRequested), _of(controller, events.PolicyDecisionRecorded))
    ]


def test_r5_plan_then_read_denies_the_read_and_proposes():
    controller, finished = _second_turn_sequence(_plan_call, lambda ctx: read_file_call("jobs/worker.py"))
    tools = _tools_in_pass(controller)
    assert tools[1:3] == [("propose_plan", True, "propose_plan is legal in state EXPLORE"), ("read_file", False, "after_propose_plan")]
    plan_proposed = _of(controller, events.PlanProposed)
    assert len(plan_proposed) == 1
    # PlanProposed follows the later denial (MR12).
    denial_done = [e for e in _of(controller, events.ToolCompleted) if e.tool is domain.ToolName.READ_FILE][1]
    assert plan_proposed[0].sequence > denial_done.sequence
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED


def test_r5_a_second_plan_never_replaces_the_first():
    other = make_plan(problem_hypothesis="a different hypothesis")
    controller, _ = _second_turn_sequence(_plan_call, lambda ctx: _plan_call(ctx, other))
    assert _tools_in_pass(controller)[2] == ("propose_plan", False, "after_propose_plan")
    assert _of(controller, events.PlanProposed)[0].problem_hypothesis == PLAN.problem_hypothesis


def test_r5_calls_after_an_invalid_plan_are_denied_and_the_pass_continues():
    invalid = lambda ctx: tool_call("propose_plan", {"problem_hypothesis": "h"})  # noqa: E731 - missing keys
    controller, finished = _second_turn_sequence(
        lambda ctx: read_file_call("jobs/worker.py"), invalid, lambda ctx: read_file_call("jobs/worker.py")
    )
    tools = _tools_in_pass(controller)
    assert tools[1][1] is True and tools[2][:2] == ("propose_plan", False)
    assert tools[3] == ("read_file", False, "after_propose_plan")
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert len(_of(controller, events.PlanProposed)) == 1


def test_r5_plan_then_apply_patch_denies_the_patch():
    controller, _ = _second_turn_sequence(_plan_call, lambda ctx: tool_call("apply_patch", {"path": "a"}))
    assert _tools_in_pass(controller)[2] == ("apply_patch", False, "after_propose_plan")


# --------------------------------------------------------------------------
# Evidence (R6, R18) and plan validation (A4)
# --------------------------------------------------------------------------


def _plan_citing(*refs: str) -> ProposePlanArgs:
    return dataclasses.replace(PLAN, evidence_refs=refs)


def _raw_plan_call(*refs: str):
    """A `propose_plan` call sent as raw JSON, for references the schema
    itself rejects (anything not shaped `tc-<pass>-<seq>`)."""
    return tool_call(
        "propose_plan",
        {
            "change": {"expected_text": PLAN.change.expected_text, "new_text": PLAN.change.new_text, "path": PLAN.change.path},
            "evidence_refs": list(refs),
            "problem_hypothesis": PLAN.problem_hypothesis,
            "risk_notes": PLAN.risk_notes,
            "verification_intent": PLAN.verification_intent,
        },
    )


def _denial_reason_for(model_steps, **build):
    model = ScriptedModel(model_steps + _read_then_plan())
    controller = _build(model, **build)
    _run(controller)
    plans = [
        p for e, p in zip(_of(controller, events.ToolRequested), _of(controller, events.PolicyDecisionRecorded))
        if e.tool is domain.ToolName.PROPOSE_PLAN
    ]
    return plans[0], controller


def test_r18_a_same_turn_read_cannot_be_cited_but_is_fed_back():
    model_steps = [turn(read_file_call("jobs/worker.py"), propose_plan_call(_plan_citing("tc-0-1")))]
    policy, controller = _denial_reason_for(model_steps)
    assert policy.reason == "arguments_invalid:evidence_ref_invalid"
    assert _of(controller, events.ToolCompleted)[0].success


def test_r18_an_earlier_turn_read_is_cited_with_a_same_turn_read():
    model = ScriptedModel(
        [turn(read_file_call("jobs/worker.py")), turn(read_file_call("jobs/worker.py"), propose_plan_call(_plan_citing("tc-0-1")))]
    )
    controller = _build(model)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert _of(controller, events.PlanProposed)[0].evidence_refs == ("tc-0-1",)


@pytest.mark.parametrize("ref", ["tc-0-9", "tc-0-2", "call_1", "nonsense"])
def test_r6_unknown_failed_and_plan_ids_are_rejected(ref):
    # tc-0-1 is a failed read (missing file in a failing toolkit) is covered
    # below; here tc-0-2 is the denied plan call's own id.
    plan_call = propose_plan_call(_plan_citing(ref)) if ref.startswith("tc-") else _raw_plan_call(ref)
    steps = [turn(read_file_call("jobs/worker.py")), turn(plan_call)]
    policy, _ = _denial_reason_for(steps)
    assert policy.reason == "arguments_invalid:evidence_ref_invalid"


def test_r6_a_failed_call_cannot_be_cited():
    class FailsOnce(FakeRepositoryTools):
        def read_file(self, args, should_stop):
            self.calls.append(("read_file", args))
            if len(self.calls) == 1:
                return self._failure()
            return super().read_file(args, should_stop)

    steps = [turn(read_file_call("missing.py")), turn(propose_plan_call(_plan_citing("tc-0-1")))]
    policy, _ = _denial_reason_for(steps, tools=FailsOnce())
    assert policy.reason == "arguments_invalid:evidence_ref_invalid"


def test_r6_a_prior_pass_id_is_rejected():
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    model = ScriptedModel([*_read_then_plan(), turn(propose_plan_call(_plan_citing("tc-0-1"))), *_read_then_plan()])
    controller = _build(model, verifier=verifier)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    pass2 = [p for p in _of(controller, events.PolicyDecisionRecorded) if p.iteration == 1]
    assert pass2[0].reason == "arguments_invalid:evidence_ref_invalid"
    # K9: the prior pass's read is still in the transcript, just not citable.
    assert any(i.body.get("tool_call_id") == "tc-0-1" for i in model.contexts[2].transcript)


def test_r6_an_id_dropped_from_the_transcript_is_rejected():
    big = "x" * (60 * 1024) + "\n"
    big_lines = big * 1
    tools = FakeRepositoryTools(big_lines)
    reads = [turn(read_file_call("jobs/worker.py")) for _ in range(5)]
    model = ScriptedModel([*reads, turn(propose_plan_call(_plan_citing("tc-0-1"))), _cite_last_read()])
    controller = _build(model, tools=tools)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert model.contexts[5].dropped_count >= 1
    assert all(i.body.get("tool_call_id") != "tc-0-1" for i in model.contexts[5].transcript)
    denied = [p for p in _of(controller, events.PolicyDecisionRecorded) if not p.allowed]
    assert denied[0].reason == "arguments_invalid:evidence_ref_invalid"
    assert all(ctx.size <= CONTEXT_CAP_BYTES for ctx in model.contexts)


@pytest.mark.parametrize(
    "arguments",
    [
        {"change": {"expected_text": "a", "new_text": "a", "path": "jobs/worker.py"}, "evidence_refs": ["tc-0-1"], "problem_hypothesis": "h", "risk_notes": "", "verification_intent": "v"},
        {"change": {"expected_text": "a", "new_text": "b", "path": "jobs/worker.py"}, "evidence_refs": [], "problem_hypothesis": "h", "risk_notes": "", "verification_intent": "v"},
        {"change": {"expected_text": "a", "new_text": "b" * 70_000, "path": "jobs/worker.py"}, "evidence_refs": ["tc-0-1"], "problem_hypothesis": "h", "risk_notes": "", "verification_intent": "v"},
    ],
    ids=["unchanged", "no_evidence", "oversize"],
)
def test_a4_invalid_plans_are_denied_and_stay_in_explore(arguments):
    steps = [turn(read_file_call("jobs/worker.py")), turn(tool_call("propose_plan", arguments))]
    policy, controller = _denial_reason_for(steps)
    assert not policy.allowed and policy.reason.startswith("arguments_invalid:")
    assert policy.state is domain.RunState.EXPLORE


# --------------------------------------------------------------------------
# Budgets (A14, A15, R9) and the wall clock (A16, A17, J3)
# --------------------------------------------------------------------------


def _single_reads(n: int) -> list:
    return [turn(read_file_call("jobs/worker.py")) for _ in range(n)]


def test_a14_the_26th_call_exceeds_the_budget_with_no_tool_requested():
    model = ScriptedModel(_single_reads(26))
    controller = _build(model)
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    (budget,) = _of(controller, events.BudgetExceeded)
    assert (budget.kind, budget.limit_value, budget.observed_value, budget.projected_value) == (
        domain.BudgetKind.TOOL_CALLS,
        25,
        25,
        26,
    )
    assert len(_of(controller, events.ToolRequested)) == 25
    assert budget.sequence > _of(controller, events.ModelResponseReceived)[-1].sequence


def test_r9_a_turn_reaching_exactly_the_limit_is_allowed_and_charged_atomically():
    eight = turn(*[read_file_call("jobs/worker.py") for _ in range(8)])
    model = ScriptedModel([eight, eight, eight, turn(read_file_call("jobs/worker.py")), _cite_last_read()])
    controller = _build(model)
    # 8 + 8 + 8 + 1 == 25 is allowed; the plan turn would be the 26th call.
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert [c.tool_calls_remaining for c in model.contexts] == [25, 17, 9, 1, 0]
    (budget,) = _of(controller, events.BudgetExceeded)
    assert (budget.observed_value, budget.projected_value) == (25, 26)


def test_r9_an_overrunning_turn_is_refused_whole():
    eight = turn(*[read_file_call("jobs/worker.py") for _ in range(8)])
    model = ScriptedModel([eight, eight, eight, eight])
    controller = _build(model)
    _run(controller)
    assert len(_of(controller, events.ToolRequested)) == 24
    (budget,) = _of(controller, events.BudgetExceeded)
    assert (budget.observed_value, budget.projected_value) == (24, 32)


@pytest.mark.parametrize("value", [True, 0, -1, 1001, 2.0, "25"])
def test_r9_run_config_validates_the_tool_call_budget(value):
    with pytest.raises(ValueError):
        RunConfig(run_id="r", task_statement="t", approval_mode=domain.ApprovalMode.NONE, lifecycle_id="a" * 32, max_tool_calls_per_pass=value)


@pytest.mark.parametrize("value", [True, 0, -1.0, float("nan"), float("inf"), 86_401])
def test_r9_run_config_validates_the_wall_clock_budget(value):
    with pytest.raises(ValueError):
        RunConfig(run_id="r", task_statement="t", approval_mode=domain.ApprovalMode.NONE, lifecycle_id="a" * 32, wall_clock_budget_seconds=value)


def test_a15_the_tool_call_counter_resets_each_pass():
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    model = ScriptedModel([*_single_reads(20), _cite_last_read(), *_read_then_plan()])
    controller = _build(model, verifier=verifier)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    pass2 = [c for c in model.contexts if c.pass_index == 1]
    assert pass2[0].tool_calls_remaining == 25


def _deadline_run(advance_where: str, *, cancel=False, budget=10.0):
    clock = ManualClock()
    source = CancellationSource()

    def bump():
        clock.advance(budget * 10**5)  # arbitrarily long: nothing bounds the overrun
        if cancel:
            source.request("t")

    class Approval(FakeApprovalProvider):
        def decide(self, request):
            if advance_where == "approval":
                bump()
            return super().decide(request)

    class Tools(FakeRepositoryTools):
        def read_file(self, args, should_stop):
            if advance_where == "tool":
                bump()
            return super().read_file(args, should_stop)

    def model_step(ctx):
        if advance_where == "model":
            bump()
        return turn(read_file_call("jobs/worker.py"))

    model = ScriptedModel([model_step, _cite_last_read()])
    controller = _build(
        model,
        tools=Tools("    job.retry_count += 1\n"),
        approval=Approval((domain.ApprovalDecision.APPROVED,)),
        clock=clock,
        cancellation=source.token,
        wall_clock_budget_seconds=budget,
    )
    return controller, _run(controller)


@pytest.mark.parametrize("where,site", [("model", "C25"), ("tool", "C27"), ("approval", "C15")])
def test_a16_the_wall_clock_trips_at_the_next_safe_point(where, site):
    controller, finished = _deadline_run(where)
    assert finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    (budget,) = _of(controller, events.BudgetExceeded)
    assert budget.kind is domain.BudgetKind.WALL_CLOCK and budget.limit_value == 10.0
    assert _sites(controller)[-1] == site


def test_j3_a_deadline_crossed_during_approval_records_the_decision_first():
    controller, finished = _deadline_run("approval")
    approval = _of(controller, events.ApprovalRecorded)
    (budget,) = _of(controller, events.BudgetExceeded)
    assert len(approval) == 1 and approval[0].sequence < budget.sequence
    assert not any(e.tool is domain.ToolName.APPLY_PATCH for e in _of(controller, events.ToolRequested))


@pytest.mark.parametrize("where", ["model", "tool", "approval"])
def test_a17_j3_cancellation_outranks_an_expired_deadline(where):
    controller, finished = _deadline_run(where, cancel=True)
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED
    assert not _of(controller, events.BudgetExceeded)


def test_a17_the_wall_clock_outranks_the_tool_call_budget():
    clock = ManualClock()

    def over(ctx):
        clock.advance(10**6)
        return turn(*[read_file_call("jobs/worker.py") for _ in range(8)])

    model = ScriptedModel([*_single_reads(24), over])
    controller = _build(model, clock=clock, wall_clock_budget_seconds=5.0)
    _run(controller)
    (budget,) = _of(controller, events.BudgetExceeded)
    assert budget.kind is domain.BudgetKind.WALL_CLOCK


# --------------------------------------------------------------------------
# Approval binding and digests (A19, R2, R19, R30), the patch seam
# --------------------------------------------------------------------------


def test_a19_digests_bind_proposal_approval_and_patch():
    approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
    applier = FakePatchApplier()
    controller = _build(ScriptedModel(_read_then_plan()), approval=approval, applier=applier)
    _run(controller)
    (proposed,), (recorded,), (applied,) = (
        _of(controller, events.PlanProposed),
        _of(controller, events.ApprovalRecorded),
        _of(controller, events.PatchApplied),
    )
    (request,) = approval.requests
    digests = compute_digests(request.args)
    assert proposed.plan_digest == recorded.plan_digest == request.plan_digest == digests.plan_digest
    assert proposed.change_digest == applied.change_digest == request.change_digest == digests.change_digest
    assert proposed.plan_revision == recorded.plan_revision == applied.plan_revision == request.plan_revision == 1
    # The patch seam: the operations are built only from the approved change.
    assert applier.operations == [(PatchOperation.from_change(request.args.change),)]


def test_a19_material_change_is_measured_against_the_last_approved_revision():
    reworded = make_plan(problem_hypothesis="the same change, explained differently")
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    approval = FakeApprovalProvider(
        (domain.ApprovalDecision.REVISION_REQUESTED, domain.ApprovalDecision.APPROVED, domain.ApprovalDecision.APPROVED),
        revision_note="SENTINEL_NOTE please reconsider",
    )
    model = ScriptedModel(_read_then_plan() + _read_then_plan() + _read_then_plan(reworded))
    controller = _build(model, approval=approval, verifier=verifier)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    proposed = _of(controller, events.PlanProposed)
    # r1 proposed; r2 the same change after a revision (nothing approved yet);
    # r3 reworded with the same change as approved r2.
    assert [e.plan_revision for e in proposed] == [1, 2, 3]
    assert [e.material_change for e in proposed] == [True, True, False]
    revision = [i for i in model.contexts[2].transcript if i.kind is FeedbackKind.REVISION_REQUESTED]
    assert revision[0].body["operator_note"] == "SENTINEL_NOTE please reconsider"
    _scan(controller, "SENTINEL_NOTE", allow={"SENTINEL_NOTE": {"ApprovalRecorded.operator_note"}})


@pytest.mark.parametrize("field", ["problem_hypothesis", "evidence_refs", "verification_intent", "risk_notes", "change"])
def test_r19_a_mutated_approved_plan_fails_closed_before_dispatch(field):
    replacement = {
        "problem_hypothesis": "tampered",
        "evidence_refs": ("tc-0-9",),
        "verification_intent": "tampered",
        "risk_notes": "tampered",
        "change": ProposedChange("jobs/worker.py", "    job.retry_count += 1\n", "    tampered\n"),
    }[field]

    class Tamper(FakeApprovalProvider):
        def decide(self, request):
            response = super().decide(request)
            object.__setattr__(request.args, field, replacement)
            return response

    applier = FakePatchApplier()
    controller = _build(ScriptedModel(_read_then_plan()), approval=Tamper((domain.ApprovalDecision.APPROVED,)), applier=applier)
    finished = _run(controller)
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert applier.operations == []
    assert not any(e.tool is domain.ToolName.APPLY_PATCH for e in _of(controller, events.ToolRequested))


def test_r30_plan_proposal_is_the_parsed_arguments_and_dispatch_uses_compute_digests(monkeypatch):
    assert PlanProposal is ProposePlanArgs
    for module in SRC.glob("*.py"):
        text = module.read_text()
        assert ("codeagent.plan.v1" in text) == (module.name == "tool_schema.py"), module.name
        assert ("codeagent.change.v1" in text) == (module.name == "tool_schema.py"), module.name
    calls = []
    real = controller_module.compute_digests
    monkeypatch.setattr(controller_module, "compute_digests", lambda args: calls.append(args) or real(args))
    _run(_build(ScriptedModel(_read_then_plan())))
    assert len(calls) == 2  # once at proposal, once at the dispatch recheck


def test_patch_seam_ast_pins_no_caller_supplied_operations():
    assert "patch_operations" not in inspect.signature(_lifecycle_run.run_lifecycle_aware).parameters
    assert list(inspect.signature(GitPatchApplier.__init__).parameters) == ["self", "worktree_path"]
    tree = ast.parse((SRC / "_lifecycle_run.py").read_text())
    assert not any(isinstance(n, ast.Name) and n.id == "PatchOperation" for n in ast.walk(tree))


# --------------------------------------------------------------------------
# Notes (R34) and redaction (R4)
# --------------------------------------------------------------------------


class _NoteApproval:
    def __init__(self, decision, note):
        self.decision, self.note = decision, note

    def decide(self, request):
        return ApprovalResponse(self.decision, self.note)


@pytest.mark.parametrize("note", [None, "", "   ", "\x01\x02", "\t\n\x7f", "​‮", " \x00 ​", "x" * 2049, "\ud800ok"])
def test_r34_invalid_revision_notes_are_unclassified_with_no_approval_recorded(note):
    controller = _build(ScriptedModel(_read_then_plan()), approval=_NoteApproval(domain.ApprovalDecision.REVISION_REQUESTED, note))
    finished = _run(controller)
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert not _of(controller, events.ApprovalRecorded)


@pytest.mark.parametrize("note,recorded", [("ok\x01", "ok?"), ("é", "é"), ("?", "?")])
def test_r34_meaningful_revision_notes_are_recorded_sanitized(note, recorded):
    controller = _build(
        ScriptedModel(_read_then_plan() * 3),
        approval=_NoteApproval(domain.ApprovalDecision.REVISION_REQUESTED, note),
        max_plan_revisions=1,
    )
    _run(controller)
    assert _of(controller, events.ApprovalRecorded)[0].operator_note == recorded


@pytest.mark.parametrize("decision", [domain.ApprovalDecision.APPROVED, domain.ApprovalDecision.REJECTED])
@pytest.mark.parametrize("note,recorded", [(None, None), ("\x01", None), ("", None), ("fine\x07", "fine?")])
def test_r34_approve_and_reject_normalise_meaningless_notes(decision, note, recorded):
    controller = _build(ScriptedModel(_read_then_plan()), approval=_NoteApproval(decision, note))
    _run(controller)
    assert _of(controller, events.ApprovalRecorded)[0].operator_note == recorded


def test_r34_a_non_response_return_is_unclassified():
    class Bad:
        def decide(self, request):
            return domain.ApprovalDecision.APPROVED

    controller = _build(ScriptedModel(_read_then_plan()), approval=Bad())
    finished = _run(controller)
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE and not _of(controller, events.ApprovalRecorded)


class _SnippetTools(FakeRepositoryTools):
    def search_text(self, args, should_stop):
        self.calls.append(("search_text", args))
        payload = {
            "excluded": {},
            "next_cursor": None,
            "page_coverage_complete": True,
            "page_truncated": False,
            "results": [{"column": 1, "line": 1, "occurrences": 1, "path": "jobs/worker.py", "snippet": "SENTINEL_SNIPPET"}],
            "search_complete": True,
            "unsearched": {},
        }
        return ToolOutcome(ok=True, payload=payload, summary="1 result(s)")


class _NoisyVerifier(FakeVerifier):
    def run(self, attempt_index):
        result = super().run(attempt_index)
        return dataclasses.replace(result, stdout="SENTINEL_STDOUT", stderr="SENTINEL_STDERR")


def test_r4_sentinels_never_reach_events_except_their_documented_fields():
    plan = make_plan(
        expected_text="    job.retry_count += 1\n",
        new_text="    SENTINEL_NEW\n",
        risk_notes="copied SENTINEL_FILE",
    )
    plan = dataclasses.replace(plan, change=ProposedChange("jobs/worker.py", "SENTINEL_EXPECTED\n", "    SENTINEL_NEW\n"))
    verifier = _NoisyVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    approval = FakeApprovalProvider(
        (domain.ApprovalDecision.APPROVED,), revision_note="unused"
    )
    model = ScriptedModel([turn(search_text_call("retry")), *_read_then_plan(plan), *_read_then_plan(plan)])
    controller = _build(
        model,
        tools=_SnippetTools("SENTINEL_FILE content\n"),
        verifier=verifier,
        approval=approval,
    )
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    _scan(
        controller,
        "SENTINEL_EXPECTED",
        "SENTINEL_NEW",
        "SENTINEL_SNIPPET",
        "SENTINEL_STDOUT",
        "SENTINEL_STDERR",
        "SENTINEL_FILE",
        allow={"SENTINEL_FILE": {"PlanProposed.risk_notes"}},
    )
    # The verification tails did reach the model, in memory only.
    failed = [i for i in model.contexts[3].transcript if i.kind is FeedbackKind.VERIFICATION_FAILED]
    assert failed[0].body["stdout_tail"] == "SENTINEL_STDOUT"


def test_r11_risk_notes_reach_plan_proposed_unchanged():
    plan = make_plan(risk_notes="may affect the retry metrics")
    controller = _build(ScriptedModel(_read_then_plan(plan)))
    _run(controller)
    assert _of(controller, events.PlanProposed)[0].risk_notes == "may affect the retry metrics"


# --------------------------------------------------------------------------
# Context (R12, R26)
# --------------------------------------------------------------------------


def test_r12_task_statement_limit():
    base = dict(run_id="r", approval_mode=domain.ApprovalMode.NONE, lifecycle_id="a" * 32)
    RunConfig(task_statement="t" * 8192, **base)
    with pytest.raises(ValueError):
        RunConfig(task_statement="t" * 8193, **base)
    with pytest.raises(ValueError):
        RunConfig(task_statement="t", max_repair_iterations=20, **base)


def test_r12_r26_context_stays_under_the_cap_and_never_drops_verification_items():
    line = "y" * 200 + "\n"
    tools = FakeRepositoryTools(line * 380)  # about 80 KiB per read result
    verifier = FakeVerifier((events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED))
    malformed = [turn() for _ in range(6)]
    model = ScriptedModel([*_read_then_plan(), *malformed, *_single_reads(10), _cite_last_read()])
    controller = _build(model, tools=tools, verifier=verifier)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert all(ctx.size <= CONTEXT_CAP_BYTES for ctx in model.contexts)
    assert model.contexts[-1].dropped_count > 0
    for ctx in model.contexts[2:]:
        assert any(i.kind is FeedbackKind.VERIFICATION_FAILED for i in ctx.transcript)


# --------------------------------------------------------------------------
# MODEL_RESPONSE_INCOMPLETE (R36) and structural pins (R14)
# --------------------------------------------------------------------------


def test_r36_incomplete_is_explore_only_non_abort_and_rendered():
    assert domain.Trigger.MODEL_RESPONSE_INCOMPLETE not in domain.ABORT_TRIGGERS
    for state in domain.RunState:
        if state is domain.RunState.EXPLORE:
            assert domain.transition(state, domain.Trigger.MODEL_RESPONSE_INCOMPLETE).next_state is domain.RunState.DONE
        else:
            with pytest.raises(domain.IllegalTransitionError):
                domain.transition(state, domain.Trigger.MODEL_RESPONSE_INCOMPLETE)
    controller = _build(ScriptedModel([turn(status=S.INCOMPLETE, incomplete_reason=b"max_output_tokens")]))
    _run(controller)
    report = build_report(controller.log.events)
    assert report.terminal_reason == "model_response_incomplete"
    assert "model_response_incomplete" in render_text(report)


def test_r14_no_production_module_imports_test_support_or_the_scripted_model():
    for module in SRC.rglob("*.py"):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not any(n.startswith("tests") for n in names), module.name



# --------------------------------------------------------------------------
# Further targeted checks (R1/R27 response limit, J4 end to end, should_stop,
# R24 static .git denial, K5 placeholders, R26 drop order, R4 hypothesis)
# --------------------------------------------------------------------------


def _big_plan_call(fill: int):
    arguments = {
        "change": {"expected_text": "a", "new_text": "b" * fill, "path": "jobs/worker.py"},
        "evidence_refs": ["tc-0-1"],
        "problem_hypothesis": "h",
        "risk_notes": "",
        "verification_intent": "v",
    }
    return tool_call("propose_plan", arguments)


def test_r1_a_response_over_one_mebibyte_is_malformed():
    calls = [_big_plan_call(140_000) for _ in range(8)]
    assert sum(len(c.provider_call_id) + len(c.name) + len(c.arguments_json) for c in calls) > 1_048_576
    controller = _build(ScriptedModel([turn(*calls), *_read_then_plan()]))
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    first = _of(controller, events.ModelResponseReceived)[0]
    assert first.error is not None and "reason=response_too_large" in first.error.message


def test_j4_a_control_character_query_is_denied_and_never_recorded():
    sentinel = "SENTINEL_QUERY_7f3a"
    model = ScriptedModel([turn(search_text_call(sentinel + "\x1b")), *_read_then_plan()])
    controller = _build(model)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    policy = _of(controller, events.PolicyDecisionRecorded)[0]
    assert not policy.allowed and policy.reason == "arguments_invalid:query_control_character"
    request = _of(controller, events.ToolRequested)[0]
    assert set(json.loads(request.redacted_arguments_json)) == {"invalid", "raw_bytes", "raw_sha256"}
    _scan(controller, sentinel)


def test_should_stop_is_a_pure_poll_the_tool_can_answer():
    source = CancellationSource()

    class Polls(FakeRepositoryTools):
        def read_file(self, args, should_stop):
            source.request("t")
            assert should_stop() is True
            return self._failure()

    controller = _build(ScriptedModel([turn(read_file_call("a.py"))]), tools=Polls(), cancellation=source.token)
    finished = _run(controller)
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED
    (completed,) = _of(controller, events.ToolCompleted)
    assert completed.error.message == "fixture-forced read failure"
    assert _sites(controller)[-1] == "C27"


def test_r24_git_paths_are_denied_at_policy_before_any_tool_runs():
    class Untouchable(FakeRepositoryTools):
        def read_file(self, args, should_stop):
            raise AssertionError("the toolkit must not be reached")

        list_directory = search_text = read_file

    for path in (".git", ".GIT", ".Git/x"):
        model = ScriptedModel([turn(read_file_call(path)), turn(list_directory_call(path))] + [turn(status=S.CANCELLED)])
        controller = _build(model, tools=Untouchable())
        assert _run(controller).terminal_reason is domain.TerminalReason.CANCELLED
        reasons = [p.reason for p in _of(controller, events.PolicyDecisionRecorded)]
        assert reasons == ["administrative_path", "administrative_path"]


def test_k5_an_invalid_response_id_becomes_a_placeholder():
    bad = dataclasses.replace(turn(read_file_call("jobs/worker.py")), response_id=b"SENTINEL RESP!")
    controller = _build(ScriptedModel([bad, *_read_then_plan()]))
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    first = _of(controller, events.ModelResponseReceived)[0]
    assert first.response_id == "codeagent.invalid-response-id"
    assert first.error.code is ErrorCode.MODEL_RESPONSE_MALFORMED
    _scan(controller, "SENTINEL RESP!")


def test_r26_old_malformed_items_are_dropped_before_newer_feedback():
    line = "y" * 200 + "\n"
    tools = FakeRepositoryTools(line * 380)
    model = ScriptedModel([*[turn() for _ in range(6)], *_single_reads(5), _cite_last_read()])
    controller = _build(model, tools=tools)
    assert _run(controller).terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    last = model.contexts[-1]
    assert last.dropped_count >= 6
    assert FeedbackKind.MALFORMED_RESPONSE not in [i.kind for i in last.transcript]
    assert all(ctx.size <= CONTEXT_CAP_BYTES for ctx in model.contexts)


def test_r4_the_hypothesis_reaches_only_plan_proposed():
    plan = make_plan(problem_hypothesis="SENTINEL_HYPOTHESIS about retries")
    controller = _build(ScriptedModel(_read_then_plan(plan)))
    _run(controller)
    _scan(controller, "SENTINEL_HYPOTHESIS", allow={"SENTINEL_HYPOTHESIS": {"PlanProposed.problem_hypothesis"}})


# --------------------------------------------------------------------------
# Correction pass (2026-10-20): return-type validation inside the guard,
# and failed-tool duration accounting
# --------------------------------------------------------------------------


class SentinelNotAnOutcome:
    def __repr__(self) -> str:
        return "SENTINEL_RETURNED_REPR"


class _ReturnsWrongType(FakeRepositoryTools):
    def read_file(self, args, should_stop):
        return SentinelNotAnOutcome()


def test_a_wrong_return_type_completes_the_triple_and_is_unclassified():
    controller = _build(ScriptedModel([turn(read_file_call("a.py"))]), tools=_ReturnsWrongType())
    finished = _run(controller)  # strict tool and model pairing
    kinds = [type(e) for e in controller.log.events if isinstance(e, (events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted))]
    assert kinds == [events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted]
    (completed,) = _of(controller, events.ToolCompleted)
    assert not completed.success
    assert completed.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert completed.error.message == "tool failed unexpectedly"
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE and finished.error.message == _UNCLASSIFIED
    assert [s for s, _, _ in controller._post_return_decisions] == ["C25"]  # no C27
    _scan(controller, "SENTINEL_RETURNED_REPR", "SentinelNotAnOutcome")


def _timed_total(behaviour: str, seconds: float):
    clock = ManualClock()

    class Timed(FakeRepositoryTools):
        def read_file(self, args, should_stop):
            clock.advance(seconds)
            if behaviour == "raise":
                raise RuntimeError("SENTINEL_TOOL_FAILURE")
            if behaviour == "cleanup":
                raise ToolCleanupUnconfirmedError((OSError("close"),), None)
            return super().read_file(args, should_stop)

    model = ScriptedModel([turn(read_file_call("jobs/worker.py")), _cite_last_read()])
    controller = _build(model, tools=Timed("    job.retry_count += 1\n"), clock=clock)
    finished = _run(controller)
    completed = [e for e in _of(controller, events.ToolCompleted) if e.tool is domain.ToolName.READ_FILE]
    return finished, completed[0]


@pytest.mark.parametrize("behaviour,code", [("raise", ErrorCode.UNCLASSIFIED_FAILURE), ("cleanup", ErrorCode.TOOL_EXECUTION_FAILED), ("ok", None)])
def test_tool_duration_is_counted_exactly_once_on_every_returned_or_raised_path(behaviour, code):
    base, _ = _timed_total(behaviour, 0.0)
    finished, completed = _timed_total(behaviour, 7.0)
    assert completed.duration_seconds == pytest.approx(7.0)
    assert finished.total_duration_seconds - base.total_duration_seconds == pytest.approx(7.0)
    if code is None:
        assert finished.error is None
    else:
        assert finished.error.code is code


# --------------------------------------------------------------------------
# Correction pass (2026-10-20): the controller-dispatched apply_patch triple
# is completed by one guard around the post-policy patch transaction
# --------------------------------------------------------------------------

_PATCH_SENTINEL_PLAN = make_plan(new_text="    SENTINEL_NEW_TEXT\n")


class _Invalid:
    def __repr__(self) -> str:
        return "SENTINEL_INVALID_RESULT"


def _patch_failure_run(case: str, seconds: float, exc: BaseException | None = None):
    clock = ManualClock()
    exc = exc if exc is not None else RuntimeError("SENTINEL_PATCH_FAILURE")

    class Applier(FakePatchApplier):
        def apply(self, run_id, iteration, approved_paths, operations):
            self.operations.append(operations)
            if case in ("raises", "interrupt", "invalid"):
                clock.advance(seconds)
            if case in ("raises", "interrupt"):
                raise exc
            if case == "invalid":
                return _Invalid()
            return super().apply(run_id, iteration, approved_paths, operations)

    class Session(FakeCheckpointSession):
        def establish(self, initial_sha):
            if case == "establish":
                clock.advance(seconds)
                raise exc
            return super().establish(initial_sha)

        def advance(self, new_sha):
            if case == "advance":
                clock.advance(seconds)
                raise exc
            return super().advance(new_sha)

    controller = _build(
        ScriptedModel(_read_then_plan(_PATCH_SENTINEL_PLAN)), applier=Applier(), session=Session(), clock=clock
    )
    raised: list[BaseException] = []
    real = controller._run_after_start

    def spy():
        try:
            return real()
        except BaseException as caught:  # noqa: BLE001 - recorded, then re-raised unchanged
            raised.append(caught)
            raise

    controller._run_after_start = spy
    return controller, raised


def _patch_events(controller):
    return [e for e in controller.log.events if getattr(e, "tool", None) is domain.ToolName.APPLY_PATCH]


@pytest.mark.parametrize("case", ["raises", "invalid", "establish", "advance"])
def test_an_unexpected_patch_transaction_failure_completes_the_triple(case):
    controller, raised = _patch_failure_run(case, 5.0)
    finished = _run(controller)  # strict tool and model pairing
    triple = _patch_events(controller)
    assert [type(e) for e in triple] == [events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted]
    assert [triple[1].sequence, triple[2].sequence] == [triple[0].sequence + 1, triple[0].sequence + 2]
    completed = triple[2]
    assert not completed.success and completed.result_summary == ""
    assert completed.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert completed.error.message == "tool failed unexpectedly"
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    for cls in (events.CheckpointCreated, events.PatchApplied, events.VerificationCompleted):
        assert not _of(controller, cls)
    # The exact exception reached the Slice 3C-3 boundary.
    (exc,) = raised
    if case == "invalid":
        assert type(exc) is TypeError
    else:
        assert str(exc) == "SENTINEL_PATCH_FAILURE"
    # No C27 for the controller-dispatched patch.
    assert all(seq < triple[0].sequence for _, seq, _ in controller._post_return_decisions)
    _scan(controller, "SENTINEL_PATCH_FAILURE", "SENTINEL_INVALID_RESULT", "_Invalid", "SENTINEL_NEW_TEXT", "RuntimeError")


@pytest.mark.parametrize("case", ["raises", "invalid", "establish", "advance"])
def test_an_unexpected_patch_failure_counts_its_duration_exactly_once(case):
    base, _ = _patch_failure_run(case, 0.0)
    timed, _ = _patch_failure_run(case, 5.0)
    base_finished, timed_finished = base.run(), timed.run()
    assert _patch_events(timed)[2].duration_seconds == pytest.approx(5.0)
    assert timed_finished.total_duration_seconds - base_finished.total_duration_seconds == pytest.approx(5.0)


def test_an_interrupt_in_the_patch_transaction_completes_the_triple_and_propagates():
    interrupt = KeyboardInterrupt()
    controller, _ = _patch_failure_run("interrupt", 1.0, exc=interrupt)
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt
    assert_tool_pairing(controller.log.events)
    triple = _patch_events(controller)
    assert [type(e) for e in triple] == [events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted]
    assert triple[2].error.message == "tool interrupted"
    assert not _of(controller, events.RunFinished) and not _of(controller, events.PatchApplied)


@pytest.mark.parametrize("case", ["raises", "interrupt"])
def test_a_failing_patch_failure_emission_is_attempted_once(case):
    interrupt = KeyboardInterrupt()
    controller, _ = _patch_failure_run(case, 0.0, exc=interrupt if case == "interrupt" else None)
    boom = RuntimeError("log broken")
    attempts = []
    real = controller._emit

    def emit(event):
        if isinstance(event, events.ToolCompleted) and event.tool is domain.ToolName.APPLY_PATCH:
            attempts.append(event)
            raise boom
        real(event)

    controller._emit = emit
    if case == "interrupt":
        with pytest.raises(KeyboardInterrupt) as info:
            controller.run()
        assert info.value is interrupt and getattr(interrupt, CLEANUP_FAILURES_ATTRIBUTE) == (boom,)
    else:
        finished = controller.run()
        assert finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert len(attempts) == 1


def test_mapped_patch_failures_and_success_keep_their_exact_shape():
    # A returned patch failure keeps its own error object; success keeps its events.
    failing = _build(ScriptedModel(_read_then_plan()), applier=FakePatchApplier(True))
    finished = _run(failing)
    (completed,) = [e for e in _of(failing, events.ToolCompleted) if e.tool is domain.ToolName.APPLY_PATCH]
    assert finished.error is completed.error and completed.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    session = FakeCheckpointSession()
    session.advance_error = CheckpointRefError(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "forced", outcome=MutationOutcome.UNCHANGED
    )
    mapped = _build(ScriptedModel(_read_then_plan()), session=session)
    finished = _run(mapped)
    (patch_done,) = [e for e in _of(mapped, events.ToolCompleted) if e.tool is domain.ToolName.APPLY_PATCH]
    assert finished.error.code is patch_done.error.code is ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED
    ok = _build(ScriptedModel(_read_then_plan()))
    _run(ok)
    assert [type(e) for e in ok.log.events if isinstance(e, (events.CheckpointCreated, events.PatchApplied))] == [events.CheckpointCreated, events.PatchApplied]
