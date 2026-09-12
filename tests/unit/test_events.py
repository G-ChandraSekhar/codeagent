"""Exhaustive construction and validation tests for events.py.

Every event type gets at least one successful construction and one
test per validation invariant it declares. Cross-cutting coverage
(EventType <-> class registry, AnyEvent membership, pinned enum values)
lives at the bottom.
"""

from __future__ import annotations

import json
import typing
from datetime import datetime, timezone

import pytest

from codeagent import domain, events
from codeagent.errors import ErrorCode, OperationalError


_EXECUTOR_ERROR_CODE_BY_OUTCOME = {
    events.VerificationOutcome.TIMEOUT: ErrorCode.EXECUTOR_TIMEOUT,
    events.VerificationOutcome.ENVIRONMENT_FAILURE: ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
    events.VerificationOutcome.COMMAND_START_FAILURE: ErrorCode.EXECUTOR_COMMAND_START_FAILED,
}


def _executor_error_for(outcome: events.VerificationOutcome) -> OperationalError:
    return OperationalError(code=_EXECUTOR_ERROR_CODE_BY_OUTCOME[outcome], message="executor failure")


def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        run_id="run-1",
        sequence=0,
        timestamp=now(),
        state=domain.RunState.EXPLORE,
        iteration=0,
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------
# Base envelope validation (exercised directly on Event)
# --------------------------------------------------------------------


def test_event_accepts_a_valid_envelope() -> None:
    e = events.Event(**make_envelope())
    assert e.run_id == "run-1"
    assert e.schema_version == 1


def test_event_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError):
        events.Event(**make_envelope(run_id=""))


def test_event_rejects_negative_sequence() -> None:
    with pytest.raises(ValueError):
        events.Event(**make_envelope(sequence=-1))


def test_event_rejects_negative_iteration() -> None:
    with pytest.raises(ValueError):
        events.Event(**make_envelope(iteration=-1))


@pytest.mark.parametrize("schema_version", [0, -1])
def test_event_rejects_nonpositive_schema_version(schema_version: int) -> None:
    with pytest.raises(ValueError):
        events.Event(**make_envelope(schema_version=schema_version))


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError):
        events.Event(**make_envelope(timestamp=datetime(2026, 1, 1)))


def test_event_accepts_sequence_and_iteration_at_zero() -> None:
    e = events.Event(**make_envelope(sequence=0, iteration=0))
    assert e.sequence == 0
    assert e.iteration == 0


# --------------------------------------------------------------------
# RunStarted
# --------------------------------------------------------------------


def test_run_started_construction() -> None:
    e = events.RunStarted(
        **make_envelope(state=domain.RunState.INIT),
        repository_path="/tmp/repo",
        task_statement="fix the retry bug",
        verify_command=("pytest", "-q"),
        approval_mode=domain.ApprovalMode.INTERACTIVE,
    )
    assert e.verify_command == ("pytest", "-q")


@pytest.mark.parametrize(
    "overrides",
    [
        dict(repository_path=""),
        dict(task_statement=""),
        dict(verify_command=()),
        dict(verify_command=("pytest", "")),
    ],
)
def test_run_started_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        repository_path="/tmp/repo",
        task_statement="fix the retry bug",
        verify_command=("pytest", "-q"),
        approval_mode=domain.ApprovalMode.INTERACTIVE,
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.RunStarted(**make_envelope(state=domain.RunState.INIT), **kwargs)


# --------------------------------------------------------------------
# BaselineRecorded
# --------------------------------------------------------------------


def _make_baseline_recorded(**overrides: object) -> events.BaselineRecorded:
    kwargs: dict[str, object] = dict(
        outcome=events.VerificationOutcome.PASSED,
        command=("pytest",),
        exit_code=0,
        duration_seconds=1.0,
    )
    kwargs.update(overrides)
    return events.BaselineRecorded(**make_envelope(state=domain.RunState.BASELINE), **kwargs)


def test_baseline_recorded_construction() -> None:
    e = _make_baseline_recorded(duration_seconds=1.5)
    assert e.exit_code == 0


def test_baseline_recorded_rejects_empty_command() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(command=())


def test_baseline_recorded_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(duration_seconds=-1.0)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_baseline_recorded_rejects_nonfinite_duration(duration: float) -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(duration_seconds=duration)


# Exit-code rules per outcome (point 1): PASSED requires exactly 0;
# TEST_FAILURE requires a present, nonzero code; COMMAND_START_FAILURE
# requires no code (the command never ran); TIMEOUT and
# ENVIRONMENT_FAILURE permit either None or any int, including a
# negative POSIX signal-based return code (e.g. -9 for SIGKILL on
# Linux/macOS) when the sandbox is able to report one.


@pytest.mark.parametrize("exit_code", [1, -1, None])
def test_baseline_recorded_rejects_passed_with_wrong_exit_code(exit_code: int | None) -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(outcome=events.VerificationOutcome.PASSED, exit_code=exit_code)


def test_baseline_recorded_accepts_passed_with_zero_exit_code() -> None:
    e = _make_baseline_recorded(outcome=events.VerificationOutcome.PASSED, exit_code=0)
    assert e.exit_code == 0


@pytest.mark.parametrize("exit_code", [0, None])
def test_baseline_recorded_rejects_test_failure_with_wrong_exit_code(
    exit_code: int | None,
) -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(
            outcome=events.VerificationOutcome.TEST_FAILURE, exit_code=exit_code
        )


@pytest.mark.parametrize("exit_code", [1, 2, -1])
def test_baseline_recorded_accepts_test_failure_with_nonzero_exit_code(exit_code: int) -> None:
    e = _make_baseline_recorded(
        outcome=events.VerificationOutcome.TEST_FAILURE, exit_code=exit_code
    )
    assert e.exit_code == exit_code


def test_baseline_recorded_rejects_exit_code_on_command_start_failure() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(
            outcome=events.VerificationOutcome.COMMAND_START_FAILURE, exit_code=1
        )


def test_baseline_recorded_accepts_command_start_failure_without_exit_code() -> None:
    e = _make_baseline_recorded(
        outcome=events.VerificationOutcome.COMMAND_START_FAILURE,
        exit_code=None,
        error=_executor_error_for(events.VerificationOutcome.COMMAND_START_FAILURE),
    )
    assert e.exit_code is None


@pytest.mark.parametrize(
    "outcome", [events.VerificationOutcome.TIMEOUT, events.VerificationOutcome.ENVIRONMENT_FAILURE]
)
@pytest.mark.parametrize("exit_code", [None, 0, 1, -9])
def test_baseline_recorded_accepts_timeout_or_environment_failure_with_any_exit_code(
    outcome: events.VerificationOutcome, exit_code: int | None
) -> None:
    e = _make_baseline_recorded(
        outcome=outcome, exit_code=exit_code, error=_executor_error_for(outcome)
    )
    assert e.outcome is outcome
    assert e.exit_code == exit_code


def test_baseline_recorded_requires_error_for_command_start_failure() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(
            outcome=events.VerificationOutcome.COMMAND_START_FAILURE, exit_code=None, error=None
        )


def test_baseline_recorded_rejects_mismatched_error_code_for_outcome() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(
            outcome=events.VerificationOutcome.TIMEOUT,
            exit_code=None,
            error=_executor_error_for(events.VerificationOutcome.ENVIRONMENT_FAILURE),
        )


def test_baseline_recorded_rejects_error_when_passed() -> None:
    with pytest.raises(ValueError):
        _make_baseline_recorded(
            outcome=events.VerificationOutcome.PASSED,
            exit_code=0,
            error=_executor_error_for(events.VerificationOutcome.TIMEOUT),
        )


# --------------------------------------------------------------------
# StateTransitioned
# --------------------------------------------------------------------


def test_state_transitioned_construction_matches_domain_transition() -> None:
    e = events.StateTransitioned(
        **make_envelope(state=domain.RunState.DONE),
        trigger=domain.Trigger.VERIFICATION_PASSED,
        from_state=domain.RunState.VERIFY,
        to_state=domain.RunState.DONE,
        terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
    )
    assert e.to_state is domain.RunState.DONE


def test_state_transitioned_rejects_state_not_matching_to_state() -> None:
    with pytest.raises(ValueError):
        events.StateTransitioned(
            **make_envelope(state=domain.RunState.EXPLORE),
            trigger=domain.Trigger.VERIFICATION_PASSED,
            from_state=domain.RunState.VERIFY,
            to_state=domain.RunState.DONE,
            terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
        )


def test_state_transitioned_rejects_a_transition_domain_would_reject() -> None:
    with pytest.raises(ValueError):
        events.StateTransitioned(
            **make_envelope(state=domain.RunState.EXPLORE),
            trigger=domain.Trigger.PATCH_APPLIED,
            from_state=domain.RunState.INIT,
            to_state=domain.RunState.EXPLORE,
        )


def test_state_transitioned_rejects_wrong_claimed_next_state() -> None:
    with pytest.raises(ValueError):
        events.StateTransitioned(
            **make_envelope(state=domain.RunState.PLAN),
            trigger=domain.Trigger.RUN_STARTED,
            from_state=domain.RunState.INIT,
            to_state=domain.RunState.PLAN,
        )


def test_state_transitioned_rejects_wrong_claimed_terminal_reason() -> None:
    with pytest.raises(ValueError):
        events.StateTransitioned(
            **make_envelope(state=domain.RunState.DONE),
            trigger=domain.Trigger.PLAN_REJECTED,
            from_state=domain.RunState.APPROVAL,
            to_state=domain.RunState.DONE,
            terminal_reason=domain.TerminalReason.CANCELLED,
        )


@pytest.mark.parametrize(
    ("from_state", "trigger", "to_state", "terminal_reason"),
    [
        (domain.RunState.INIT, domain.Trigger.RUN_STARTED, domain.RunState.BASELINE, None),
        (
            domain.RunState.BASELINE,
            domain.Trigger.BASELINE_RECORDED,
            domain.RunState.EXPLORE,
            None,
        ),
        (domain.RunState.EXPLORE, domain.Trigger.PLAN_PROPOSED, domain.RunState.PLAN, None),
        (domain.RunState.PLAN, domain.Trigger.PLAN_RECORDED, domain.RunState.APPROVAL, None),
        (
            domain.RunState.APPROVAL,
            domain.Trigger.PLAN_APPROVED,
            domain.RunState.EXECUTE,
            None,
        ),
        (
            domain.RunState.APPROVAL,
            domain.Trigger.PLAN_REJECTED,
            domain.RunState.DONE,
            domain.TerminalReason.PLAN_REJECTED,
        ),
        (
            domain.RunState.APPROVAL,
            domain.Trigger.PLAN_REVISION_REQUESTED,
            domain.RunState.EXPLORE,
            None,
        ),
        (domain.RunState.EXECUTE, domain.Trigger.PATCH_APPLIED, domain.RunState.VERIFY, None),
        (
            domain.RunState.VERIFY,
            domain.Trigger.VERIFICATION_PASSED,
            domain.RunState.DONE,
            domain.TerminalReason.VERIFICATION_PASSED,
        ),
        (
            domain.RunState.VERIFY,
            domain.Trigger.VERIFICATION_FAILED,
            domain.RunState.EXPLORE,
            None,
        ),
    ],
    ids=lambda v: getattr(v, "value", v),
)
def test_state_transitioned_accepts_every_legal_domain_transition(
    from_state: domain.RunState,
    trigger: domain.Trigger,
    to_state: domain.RunState,
    terminal_reason: domain.TerminalReason | None,
) -> None:
    e = events.StateTransitioned(
        **make_envelope(state=to_state),
        trigger=trigger,
        from_state=from_state,
        to_state=to_state,
        terminal_reason=terminal_reason,
    )
    assert e.from_state is from_state


@pytest.mark.parametrize("trigger", sorted(domain.ABORT_TRIGGERS, key=lambda t: t.value))
def test_state_transitioned_accepts_every_abort_trigger(trigger: domain.Trigger) -> None:
    reason = domain.transition(domain.RunState.EXPLORE, trigger).terminal_reason
    e = events.StateTransitioned(
        **make_envelope(state=domain.RunState.DONE),
        trigger=trigger,
        from_state=domain.RunState.EXPLORE,
        to_state=domain.RunState.DONE,
        terminal_reason=reason,
    )
    assert e.terminal_reason is reason


# --------------------------------------------------------------------
# ModelRequestStarted / ModelResponseReceived
# --------------------------------------------------------------------


def test_model_request_started_construction() -> None:
    e = events.ModelRequestStarted(
        **make_envelope(), request_id="req-1", model_name="test-model"
    )
    assert e.request_id == "req-1"


@pytest.mark.parametrize(
    "overrides",
    [dict(request_id=""), dict(model_name=""), dict(input_token_estimate=-1)],
)
def test_model_request_started_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(request_id="req-1", model_name="test-model")
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.ModelRequestStarted(**make_envelope(), **kwargs)


def test_model_response_received_construction() -> None:
    e = events.ModelResponseReceived(
        **make_envelope(),
        request_id="req-1",
        model_name="test-model",
        response_id="resp-1",
        status=events.ModelResponseStatus.COMPLETED,
        input_tokens=10,
        output_tokens=20,
        latency_seconds=0.4,
        tool_call_count=1,
    )
    assert e.status is events.ModelResponseStatus.COMPLETED


def test_model_response_received_allows_missing_token_counts() -> None:
    e = events.ModelResponseReceived(
        **make_envelope(),
        request_id="req-1",
        model_name="test-model",
        response_id="resp-1",
        status=events.ModelResponseStatus.FAILED,
        input_tokens=None,
        output_tokens=None,
        latency_seconds=0.1,
        tool_call_count=0,
        error=OperationalError(code=ErrorCode.MODEL_RESPONSE_FAILED_STATUS, message="boom"),
    )
    assert e.input_tokens is None


# error/status matrix (error-taxonomy correction pass, point 3): FAILED
# requires MODEL_RESPONSE_FAILED_STATUS; COMPLETED optionally carries
# MODEL_RESPONSE_MALFORMED (the provider succeeded, but the controller's
# own downstream parse of the payload can still fail); every other
# status forbids error entirely.


def _make_model_response_received(**overrides: object) -> events.ModelResponseReceived:
    kwargs: dict[str, object] = dict(
        request_id="req-1",
        model_name="test-model",
        response_id="resp-1",
        status=events.ModelResponseStatus.COMPLETED,
        latency_seconds=0.1,
        tool_call_count=0,
    )
    kwargs.update(overrides)
    return events.ModelResponseReceived(**make_envelope(), **kwargs)


def test_model_response_received_completed_without_error() -> None:
    e = _make_model_response_received(status=events.ModelResponseStatus.COMPLETED)
    assert e.error is None


def test_model_response_received_completed_with_malformed_error() -> None:
    e = _make_model_response_received(
        status=events.ModelResponseStatus.COMPLETED,
        error=OperationalError(code=ErrorCode.MODEL_RESPONSE_MALFORMED, message="bad json"),
    )
    assert e.error.code is ErrorCode.MODEL_RESPONSE_MALFORMED


def test_model_response_received_rejects_wrong_error_code_when_completed() -> None:
    with pytest.raises(ValueError):
        _make_model_response_received(
            status=events.ModelResponseStatus.COMPLETED,
            error=OperationalError(code=ErrorCode.MODEL_RESPONSE_FAILED_STATUS, message="x"),
        )


def test_model_response_received_requires_error_when_failed() -> None:
    with pytest.raises(ValueError):
        _make_model_response_received(status=events.ModelResponseStatus.FAILED, error=None)


def test_model_response_received_rejects_wrong_error_code_when_failed() -> None:
    with pytest.raises(ValueError):
        _make_model_response_received(
            status=events.ModelResponseStatus.FAILED,
            error=OperationalError(code=ErrorCode.MODEL_RESPONSE_MALFORMED, message="x"),
        )


@pytest.mark.parametrize(
    "status",
    [
        events.ModelResponseStatus.INCOMPLETE,
        events.ModelResponseStatus.CANCELLED,
        events.ModelResponseStatus.IN_PROGRESS,
        events.ModelResponseStatus.QUEUED,
    ],
)
def test_model_response_received_forbids_error_for_other_statuses(
    status: events.ModelResponseStatus,
) -> None:
    kwargs: dict[str, object] = dict(
        status=status,
        error=OperationalError(code=ErrorCode.MODEL_RESPONSE_MALFORMED, message="x"),
    )
    if status is events.ModelResponseStatus.INCOMPLETE:
        kwargs["incomplete_reason"] = "max_output_tokens"
    with pytest.raises(ValueError):
        _make_model_response_received(**kwargs)


@pytest.mark.parametrize(
    "overrides",
    [
        dict(request_id=""),
        dict(model_name=""),
        dict(response_id=""),
        dict(input_tokens=-1),
        dict(output_tokens=-1),
        dict(latency_seconds=-0.1),
        dict(latency_seconds=float("nan")),
        dict(latency_seconds=float("inf")),
        dict(tool_call_count=-1),
    ],
)
def test_model_response_received_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        request_id="req-1",
        model_name="test-model",
        response_id="resp-1",
        status=events.ModelResponseStatus.COMPLETED,
        input_tokens=1,
        output_tokens=1,
        latency_seconds=0.1,
        tool_call_count=0,
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.ModelResponseReceived(**make_envelope(), **kwargs)


def test_model_response_received_requires_reason_when_incomplete() -> None:
    with pytest.raises(ValueError):
        events.ModelResponseReceived(
            **make_envelope(),
            request_id="req-1",
            model_name="test-model",
            response_id="resp-1",
            status=events.ModelResponseStatus.INCOMPLETE,
            incomplete_reason=None,
            latency_seconds=0.1,
            tool_call_count=0,
        )


def test_model_response_received_rejects_empty_reason_when_incomplete() -> None:
    with pytest.raises(ValueError):
        events.ModelResponseReceived(
            **make_envelope(),
            request_id="req-1",
            model_name="test-model",
            response_id="resp-1",
            status=events.ModelResponseStatus.INCOMPLETE,
            incomplete_reason="",
            latency_seconds=0.1,
            tool_call_count=0,
        )


def test_model_response_received_rejects_reason_when_not_incomplete() -> None:
    with pytest.raises(ValueError):
        events.ModelResponseReceived(
            **make_envelope(),
            request_id="req-1",
            model_name="test-model",
            response_id="resp-1",
            status=events.ModelResponseStatus.COMPLETED,
            incomplete_reason="ran out of tokens",
            latency_seconds=0.1,
            tool_call_count=0,
        )


def test_model_response_received_accepts_incomplete_with_reason() -> None:
    e = events.ModelResponseReceived(
        **make_envelope(),
        request_id="req-1",
        model_name="test-model",
        response_id="resp-1",
        status=events.ModelResponseStatus.INCOMPLETE,
        incomplete_reason="max_output_tokens",
        latency_seconds=0.1,
        tool_call_count=0,
    )
    assert e.incomplete_reason == "max_output_tokens"


# --------------------------------------------------------------------
# ModelRequestFailed
# --------------------------------------------------------------------


def test_model_request_failed_construction() -> None:
    e = events.ModelRequestFailed(
        **make_envelope(),
        request_id="req-1",
        error=OperationalError(code=ErrorCode.MODEL_PROVIDER_REQUEST_FAILED, message="timed out"),
    )
    assert e.request_id == "req-1"
    assert e.error.code is ErrorCode.MODEL_PROVIDER_REQUEST_FAILED


def test_model_request_failed_accepts_auth_failed() -> None:
    e = events.ModelRequestFailed(
        **make_envelope(),
        request_id="req-1",
        error=OperationalError(code=ErrorCode.MODEL_PROVIDER_AUTH_FAILED, message="401"),
    )
    assert e.error.code is ErrorCode.MODEL_PROVIDER_AUTH_FAILED


def test_model_request_failed_rejects_empty_request_id() -> None:
    with pytest.raises(ValueError):
        events.ModelRequestFailed(
            **make_envelope(),
            request_id="",
            error=OperationalError(code=ErrorCode.MODEL_PROVIDER_REQUEST_FAILED, message="x"),
        )


@pytest.mark.parametrize(
    "code",
    [c for c in ErrorCode if c not in (ErrorCode.MODEL_PROVIDER_REQUEST_FAILED, ErrorCode.MODEL_PROVIDER_AUTH_FAILED)],
)
def test_model_request_failed_rejects_codes_outside_model_provider_domain(
    code: ErrorCode,
) -> None:
    with pytest.raises(ValueError):
        events.ModelRequestFailed(
            **make_envelope(),
            request_id="req-1",
            error=OperationalError(code=code, message="x"),
        )


# --------------------------------------------------------------------
# ToolRequested
# --------------------------------------------------------------------


def _make_tool_requested(**overrides: object) -> events.ToolRequested:
    kwargs: dict[str, object] = dict(
        tool=domain.ToolName.READ_FILE,
        tool_call_id="tc-1",
        redacted_arguments_json="{}",
    )
    kwargs.update(overrides)
    return events.ToolRequested(**make_envelope(), **kwargs)


def test_tool_requested_construction_with_canonical_json() -> None:
    e = _make_tool_requested(redacted_arguments_json='{"path":"a.py"}')
    assert e.tool is domain.ToolName.READ_FILE


def test_tool_requested_rejects_empty_tool_call_id() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(tool_call_id="")


def test_tool_requested_rejects_malformed_json() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json="{not json")


def test_tool_requested_rejects_non_object_json() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json="[1,2,3]")


def test_tool_requested_rejects_non_object_scalar_json() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json="42")


def test_tool_requested_accepts_empty_object() -> None:
    e = _make_tool_requested(redacted_arguments_json="{}")
    assert e.redacted_arguments_json == "{}"


# Canonical-form rejection tests (point 3): sorted keys, compact
# separators, no duplicate keys, no NaN/Infinity. This validates that
# the supplied string is *already* canonical — it does not reformat or
# fix up noncanonical input, and it does not implement redaction.


@pytest.mark.parametrize(
    "noncanonical_json",
    [
        '{"a": 1}',  # space after colon
        '{ "a":1}',  # space after opening brace
        '{"a":1 }',  # space before closing brace
        '{"a":1,"b":2}\n',  # trailing whitespace/newline
    ],
    ids=["space_after_colon", "space_after_brace", "space_before_brace", "trailing_newline"],
)
def test_tool_requested_rejects_noncanonical_whitespace(noncanonical_json: str) -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json=noncanonical_json)


def test_tool_requested_rejects_noncanonical_key_order() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json='{"b":1,"a":2}')


def test_tool_requested_accepts_sorted_key_order() -> None:
    e = _make_tool_requested(redacted_arguments_json='{"a":2,"b":1}')
    assert e.redacted_arguments_json == '{"a":2,"b":1}'


def test_tool_requested_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json='{"a":1,"a":2}')


def test_tool_requested_rejects_duplicate_keys_in_a_nested_object() -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json='{"a":{"x":1,"x":2}}')


@pytest.mark.parametrize(
    "nonfinite_json",
    ['{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}'],
    ids=["nan", "infinity", "negative_infinity"],
)
def test_tool_requested_rejects_nonfinite_numbers(nonfinite_json: str) -> None:
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json=nonfinite_json)


def test_tool_requested_preserves_unicode_rather_than_escaping_it() -> None:
    canonical = json.dumps(
        {"name": "café"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert "\\u" not in canonical  # sanity check on the fixture itself
    e = _make_tool_requested(redacted_arguments_json=canonical)
    assert e.redacted_arguments_json == canonical


def test_tool_requested_rejects_escaped_unicode_as_noncanonical() -> None:
    escaped = json.dumps({"name": "café"}, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        _make_tool_requested(redacted_arguments_json=escaped)


# --------------------------------------------------------------------
# PolicyDecisionRecorded / ToolCompleted
# --------------------------------------------------------------------


def test_policy_decision_recorded_construction() -> None:
    e = events.PolicyDecisionRecorded(
        **make_envelope(),
        tool=domain.ToolName.APPLY_PATCH,
        tool_call_id="tc-1",
        allowed=True,
        reason="within policy",
    )
    assert e.allowed is True


@pytest.mark.parametrize("overrides", [dict(tool_call_id=""), dict(reason="")])
def test_policy_decision_recorded_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        tool=domain.ToolName.APPLY_PATCH, tool_call_id="tc-1", allowed=True, reason="ok"
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.PolicyDecisionRecorded(**make_envelope(), **kwargs)


def test_tool_completed_construction() -> None:
    e = events.ToolCompleted(
        **make_envelope(),
        tool=domain.ToolName.READ_FILE,
        tool_call_id="tc-1",
        success=True,
        duration_seconds=0.01,
        result_summary="12 lines",
    )
    assert e.success is True


def test_tool_completed_accepts_empty_result_summary() -> None:
    e = events.ToolCompleted(
        **make_envelope(),
        tool=domain.ToolName.READ_FILE,
        tool_call_id="tc-1",
        success=True,
        duration_seconds=0.0,
        result_summary="",
    )
    assert e.result_summary == ""


@pytest.mark.parametrize("overrides", [dict(tool_call_id=""), dict(duration_seconds=-0.1)])
def test_tool_completed_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        tool=domain.ToolName.READ_FILE,
        tool_call_id="tc-1",
        success=True,
        duration_seconds=0.0,
        result_summary="",
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.ToolCompleted(**make_envelope(), **kwargs)


_TOOL_COMPLETED_GENERIC_ERROR_CODES = [
    ErrorCode.TOOL_INPUT_INVALID,
    ErrorCode.TOOL_EXECUTION_FAILED,
]
_TOOL_COMPLETED_PATCH_ERROR_CODES = [
    ErrorCode.PATCH_VALIDATION_FAILED,
    ErrorCode.PATCH_APPLICATION_FAILED,
]
_ALL_TOOL_NAMES = list(domain.ToolName)
_NON_PATCH_TOOL_NAMES = [t for t in _ALL_TOOL_NAMES if t is not domain.ToolName.APPLY_PATCH]


def _make_tool_completed_failure(
    tool: domain.ToolName, code: ErrorCode
) -> events.ToolCompleted:
    return events.ToolCompleted(
        **make_envelope(),
        tool=tool,
        tool_call_id="tc-1",
        success=False,
        duration_seconds=0.0,
        result_summary="",
        error=OperationalError(code=code, message="failed"),
    )


@pytest.mark.parametrize("tool", _ALL_TOOL_NAMES)
@pytest.mark.parametrize("code", _TOOL_COMPLETED_GENERIC_ERROR_CODES)
def test_tool_completed_accepts_generic_codes_for_every_tool(
    tool: domain.ToolName, code: ErrorCode
) -> None:
    e = _make_tool_completed_failure(tool, code)
    assert e.error.code is code


@pytest.mark.parametrize("code", _TOOL_COMPLETED_PATCH_ERROR_CODES)
def test_tool_completed_accepts_patch_codes_for_apply_patch(code: ErrorCode) -> None:
    e = _make_tool_completed_failure(domain.ToolName.APPLY_PATCH, code)
    assert e.error.code is code


@pytest.mark.parametrize("tool", _NON_PATCH_TOOL_NAMES)
@pytest.mark.parametrize("code", _TOOL_COMPLETED_PATCH_ERROR_CODES)
def test_tool_completed_rejects_patch_codes_for_non_patch_tools(
    tool: domain.ToolName, code: ErrorCode
) -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(tool, code)


# Explicit, named proof (not just the parametrized sweep above) that
# each non-patch tool specifically cannot report a patch-specific code.


def test_read_file_cannot_report_patch_validation_failed() -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(domain.ToolName.READ_FILE, ErrorCode.PATCH_VALIDATION_FAILED)


def test_list_directory_cannot_report_patch_application_failed() -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(
            domain.ToolName.LIST_DIRECTORY, ErrorCode.PATCH_APPLICATION_FAILED
        )


def test_search_text_cannot_report_patch_validation_failed() -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(domain.ToolName.SEARCH_TEXT, ErrorCode.PATCH_VALIDATION_FAILED)


def test_propose_plan_cannot_report_patch_application_failed() -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(
            domain.ToolName.PROPOSE_PLAN, ErrorCode.PATCH_APPLICATION_FAILED
        )


def test_tool_completed_requires_error_when_failed() -> None:
    with pytest.raises(ValueError):
        events.ToolCompleted(
            **make_envelope(),
            tool=domain.ToolName.APPLY_PATCH,
            tool_call_id="tc-1",
            success=False,
            duration_seconds=0.0,
            result_summary="",
            error=None,
        )


@pytest.mark.parametrize("tool", _ALL_TOOL_NAMES)
@pytest.mark.parametrize(
    "code",
    [
        c
        for c in ErrorCode
        if c not in _TOOL_COMPLETED_GENERIC_ERROR_CODES and c not in _TOOL_COMPLETED_PATCH_ERROR_CODES
    ],
)
def test_tool_completed_rejects_codes_outside_tool_and_patch_domains(
    tool: domain.ToolName, code: ErrorCode
) -> None:
    with pytest.raises(ValueError):
        _make_tool_completed_failure(tool, code)


# Full partition-style sweep: every (tool, code) pair for success=False,
# classified exactly as generic (always allowed), patch-specific
# (allowed only for APPLY_PATCH), or forbidden — mirrors domain.py's
# exhaustive (state, trigger) partition tests.


@pytest.mark.parametrize(
    ("tool", "code"),
    [(t, c) for t in _ALL_TOOL_NAMES for c in ErrorCode],
    ids=[f"{t.value}+{c.value}" for t in _ALL_TOOL_NAMES for c in ErrorCode],
)
def test_tool_completed_error_permission_matrix_is_exhaustive(
    tool: domain.ToolName, code: ErrorCode
) -> None:
    expected_allowed = code in _TOOL_COMPLETED_GENERIC_ERROR_CODES or (
        tool is domain.ToolName.APPLY_PATCH and code in _TOOL_COMPLETED_PATCH_ERROR_CODES
    )
    if expected_allowed:
        e = _make_tool_completed_failure(tool, code)
        assert e.error.code is code
    else:
        with pytest.raises(ValueError):
            _make_tool_completed_failure(tool, code)


def test_tool_completed_rejects_error_when_success() -> None:
    with pytest.raises(ValueError):
        events.ToolCompleted(
            **make_envelope(),
            tool=domain.ToolName.READ_FILE,
            tool_call_id="tc-1",
            success=True,
            duration_seconds=0.0,
            result_summary="ok",
            error=OperationalError(code=ErrorCode.TOOL_EXECUTION_FAILED, message="x"),
        )


# --------------------------------------------------------------------
# PlanProposed
# --------------------------------------------------------------------


def test_plan_proposed_construction() -> None:
    e = events.PlanProposed(
        **make_envelope(),
        problem_hypothesis="idempotency key dropped on retry",
        evidence_refs=("jobs/worker.py:42",),
        proposed_file_paths=("jobs/worker.py",),
        verification_intent="pytest tests/test_worker.py",
        risk_notes="none identified",
    )
    assert e.proposed_file_paths == ("jobs/worker.py",)


@pytest.mark.parametrize(
    "overrides",
    [
        dict(problem_hypothesis=""),
        dict(verification_intent=""),
        dict(evidence_refs=("",)),
        dict(proposed_file_paths=("",)),
    ],
)
def test_plan_proposed_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        problem_hypothesis="hypothesis",
        evidence_refs=(),
        proposed_file_paths=(),
        verification_intent="pytest",
        risk_notes="",
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.PlanProposed(**make_envelope(), **kwargs)


def test_plan_proposed_allows_empty_evidence_and_paths() -> None:
    e = events.PlanProposed(
        **make_envelope(),
        problem_hypothesis="hypothesis",
        evidence_refs=(),
        proposed_file_paths=(),
        verification_intent="pytest",
        risk_notes="",
    )
    assert e.evidence_refs == ()


# --------------------------------------------------------------------
# ApprovalRecorded
# --------------------------------------------------------------------


@pytest.mark.parametrize("decision", list(domain.ApprovalDecision))
def test_approval_recorded_accepts_every_approval_decision(
    decision: domain.ApprovalDecision,
) -> None:
    e = events.ApprovalRecorded(
        **make_envelope(state=domain.RunState.APPROVAL),
        decision=decision,
        approval_mode=domain.ApprovalMode.INTERACTIVE,
    )
    assert e.decision is decision


@pytest.mark.parametrize("mode", list(domain.ApprovalMode))
def test_approval_recorded_accepts_every_approval_mode(mode: domain.ApprovalMode) -> None:
    e = events.ApprovalRecorded(
        **make_envelope(state=domain.RunState.APPROVAL),
        decision=domain.ApprovalDecision.APPROVED,
        approval_mode=mode,
    )
    assert e.approval_mode is mode


def test_approval_recorded_operator_note_optional() -> None:
    e = events.ApprovalRecorded(
        **make_envelope(state=domain.RunState.APPROVAL),
        decision=domain.ApprovalDecision.REJECTED,
        approval_mode=domain.ApprovalMode.NONE,
    )
    assert e.operator_note is None


# --------------------------------------------------------------------
# PatchApplied
# --------------------------------------------------------------------


def test_patch_applied_construction() -> None:
    e = events.PatchApplied(
        **make_envelope(state=domain.RunState.EXECUTE),
        operation_count=2,
        files_changed=("a.py", "b.py"),
        checkpoint_id="ckpt-1",
        diff_bytes=128,
    )
    assert e.operation_count == 2


@pytest.mark.parametrize(
    "overrides",
    [
        dict(checkpoint_id=""),
        dict(operation_count=0),
        dict(diff_bytes=-1),
        dict(files_changed=("",)),
    ],
)
def test_patch_applied_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        operation_count=1, files_changed=("a.py",), checkpoint_id="ckpt-1", diff_bytes=0
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.PatchApplied(**make_envelope(state=domain.RunState.EXECUTE), **kwargs)


# --------------------------------------------------------------------
# CheckpointCreated
# --------------------------------------------------------------------


def test_checkpoint_created_construction() -> None:
    e = events.CheckpointCreated(
        **make_envelope(state=domain.RunState.EXECUTE),
        checkpoint_id="ckpt-1",
        parent_checkpoint_id=None,
        commit_hash="abc123",
    )
    assert e.parent_checkpoint_id is None


def test_checkpoint_created_accepts_a_parent() -> None:
    e = events.CheckpointCreated(
        **make_envelope(state=domain.RunState.EXECUTE),
        checkpoint_id="ckpt-2",
        parent_checkpoint_id="ckpt-1",
        commit_hash="def456",
    )
    assert e.parent_checkpoint_id == "ckpt-1"


@pytest.mark.parametrize(
    "overrides",
    [dict(checkpoint_id=""), dict(commit_hash=""), dict(parent_checkpoint_id="")],
)
def test_checkpoint_created_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(checkpoint_id="ckpt-1", parent_checkpoint_id=None, commit_hash="abc123")
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.CheckpointCreated(**make_envelope(state=domain.RunState.EXECUTE), **kwargs)


# --------------------------------------------------------------------
# VerificationCompleted
# --------------------------------------------------------------------


def _make_verification_completed(**overrides: object) -> events.VerificationCompleted:
    kwargs: dict[str, object] = dict(
        outcome=events.VerificationOutcome.PASSED,
        command=("pytest", "-q"),
        exit_code=0,
        duration_seconds=2.0,
        fail_to_pass=("tests/test_retry.py::test_idempotent",),
        pass_to_pass_broken=(),
    )
    kwargs.update(overrides)
    return events.VerificationCompleted(**make_envelope(state=domain.RunState.VERIFY), **kwargs)


def test_verification_completed_construction() -> None:
    e = _make_verification_completed()
    assert e.outcome is events.VerificationOutcome.PASSED


def test_verification_completed_rejects_empty_command() -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(command=())


def test_verification_completed_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(duration_seconds=-1.0)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_verification_completed_rejects_nonfinite_duration(duration: float) -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(duration_seconds=duration)


# Same exit-code-per-outcome rules as BaselineRecorded (point 1); see
# _validate_exit_code_for_outcome, shared by both event types.


@pytest.mark.parametrize("exit_code", [1, -1, None])
def test_verification_completed_rejects_passed_with_wrong_exit_code(
    exit_code: int | None,
) -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(
            outcome=events.VerificationOutcome.PASSED, exit_code=exit_code
        )


@pytest.mark.parametrize("exit_code", [0, None])
def test_verification_completed_rejects_test_failure_with_wrong_exit_code(
    exit_code: int | None,
) -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(
            outcome=events.VerificationOutcome.TEST_FAILURE, exit_code=exit_code
        )


@pytest.mark.parametrize("exit_code", [1, 2, -1])
def test_verification_completed_accepts_test_failure_with_nonzero_exit_code(
    exit_code: int,
) -> None:
    e = _make_verification_completed(
        outcome=events.VerificationOutcome.TEST_FAILURE, exit_code=exit_code
    )
    assert e.exit_code == exit_code


def test_verification_completed_rejects_exit_code_on_command_start_failure() -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(
            outcome=events.VerificationOutcome.COMMAND_START_FAILURE, exit_code=1
        )


def test_verification_completed_accepts_command_start_failure_without_exit_code() -> None:
    e = _make_verification_completed(
        outcome=events.VerificationOutcome.COMMAND_START_FAILURE,
        exit_code=None,
        error=_executor_error_for(events.VerificationOutcome.COMMAND_START_FAILURE),
    )
    assert e.exit_code is None


@pytest.mark.parametrize(
    "outcome", [events.VerificationOutcome.TIMEOUT, events.VerificationOutcome.ENVIRONMENT_FAILURE]
)
@pytest.mark.parametrize("exit_code", [None, 0, 1, -9])
def test_verification_completed_accepts_timeout_or_environment_failure_with_any_exit_code(
    outcome: events.VerificationOutcome, exit_code: int | None
) -> None:
    e = _make_verification_completed(
        outcome=outcome, exit_code=exit_code, error=_executor_error_for(outcome)
    )
    assert e.outcome is outcome
    assert e.exit_code == exit_code


def test_verification_completed_requires_error_for_timeout() -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(
            outcome=events.VerificationOutcome.TIMEOUT, exit_code=None, error=None
        )


def test_verification_completed_rejects_error_when_passed() -> None:
    with pytest.raises(ValueError):
        _make_verification_completed(
            error=_executor_error_for(events.VerificationOutcome.TIMEOUT)
        )


# --------------------------------------------------------------------
# BudgetExceeded
# --------------------------------------------------------------------


# BudgetExceeded is a causative event fired in the controller's current
# state — the state the run was in when the budget check happened — not
# in DONE. The StateTransitioned event that follows is what actually
# carries the run to DONE (point 2; see Event's ordering docstring), so
# these tests use an active state (EXPLORE/EXECUTE/VERIFY) as the
# envelope's `state`, never DONE.


def _make_budget_exceeded(**overrides: object) -> events.BudgetExceeded:
    kwargs: dict[str, object] = dict(
        kind=domain.BudgetKind.WALL_CLOCK, limit_value=10, observed_value=10
    )
    kwargs.update(overrides)
    return events.BudgetExceeded(**make_envelope(state=domain.RunState.EXPLORE), **kwargs)


@pytest.mark.parametrize("kind", list(domain.BudgetKind))
def test_budget_exceeded_accepts_every_kind(kind: domain.BudgetKind) -> None:
    e = _make_budget_exceeded(kind=kind)
    assert e.kind is kind


@pytest.mark.parametrize(
    "state",
    [domain.RunState.EXPLORE, domain.RunState.EXECUTE, domain.RunState.VERIFY],
)
def test_budget_exceeded_is_emitted_in_the_source_state_not_done(
    state: domain.RunState,
) -> None:
    e = events.BudgetExceeded(
        **make_envelope(state=state),
        kind=domain.BudgetKind.TOOL_CALLS,
        limit_value=5,
        observed_value=5,
    )
    assert e.state is state


@pytest.mark.parametrize(
    "overrides",
    [
        dict(limit_value=-1),
        dict(observed_value=-1),
        dict(limit_value=10, observed_value=9, projected_value=None),
        dict(projected_value=-1),
        dict(limit_value=float("nan")),
        dict(observed_value=float("inf")),
        dict(projected_value=float("nan")),
    ],
)
def test_budget_exceeded_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _make_budget_exceeded(**overrides)


def test_budget_exceeded_rejects_projected_value_below_observed_value() -> None:
    with pytest.raises(ValueError):
        _make_budget_exceeded(limit_value=100, observed_value=50, projected_value=40)


# Point 4: the budget can be "reached" via observed_value alone, via
# projected_value alone (proactive enforcement — block before actually
# overspending), or both; equality with the limit counts as reaching it.


def test_budget_exceeded_accepts_observed_value_exactly_at_limit() -> None:
    e = _make_budget_exceeded(limit_value=100, observed_value=100, projected_value=None)
    assert e.observed_value == 100


def test_budget_exceeded_accepts_observed_value_above_limit() -> None:
    e = _make_budget_exceeded(limit_value=100, observed_value=150, projected_value=None)
    assert e.observed_value == 150


def test_budget_exceeded_accepts_projected_value_exactly_at_limit() -> None:
    e = _make_budget_exceeded(limit_value=100, observed_value=80, projected_value=100)
    assert e.projected_value == 100


def test_budget_exceeded_accepts_projected_overrun_with_observed_under_limit() -> None:
    """The proactive case: the operation hasn't happened yet, so
    observed_value is still under the limit, but the projected
    consumption if it were allowed to proceed would exceed it."""
    e = _make_budget_exceeded(limit_value=100, observed_value=60, projected_value=120)
    assert e.observed_value == 60
    assert e.projected_value == 120


def test_budget_exceeded_rejects_when_neither_observed_nor_projected_reaches_limit() -> None:
    with pytest.raises(ValueError):
        _make_budget_exceeded(limit_value=100, observed_value=50, projected_value=60)


def test_budget_exceeded_default_projected_value_is_none() -> None:
    e = _make_budget_exceeded(limit_value=10, observed_value=10)
    assert e.projected_value is None


# --------------------------------------------------------------------
# RunFinished
# --------------------------------------------------------------------


def test_run_finished_construction() -> None:
    e = events.RunFinished(
        **make_envelope(state=domain.RunState.DONE),
        terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
        total_cost_microusd=42,
        total_tokens=1000,
        total_duration_seconds=30.0,
        iterations_used=1,
    )
    assert e.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED


def test_run_finished_allows_missing_cost() -> None:
    e = events.RunFinished(
        **make_envelope(state=domain.RunState.DONE),
        terminal_reason=domain.TerminalReason.CANCELLED,
        total_cost_microusd=None,
        total_tokens=0,
        total_duration_seconds=0.0,
        iterations_used=0,
    )
    assert e.total_cost_microusd is None


def test_run_finished_rejects_nonterminal_state() -> None:
    with pytest.raises(ValueError):
        events.RunFinished(
            **make_envelope(state=domain.RunState.EXPLORE),
            terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
            total_cost_microusd=0,
            total_tokens=0,
            total_duration_seconds=0.0,
            iterations_used=0,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        dict(total_cost_microusd=-1),
        dict(total_tokens=-1),
        dict(total_duration_seconds=-1.0),
        dict(iterations_used=-1),
    ],
)
def test_run_finished_rejects_negative_numeric_fields(overrides: dict[str, object]) -> None:
    kwargs = dict(
        terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
        total_cost_microusd=0,
        total_tokens=0,
        total_duration_seconds=0.0,
        iterations_used=0,
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        events.RunFinished(**make_envelope(state=domain.RunState.DONE), **kwargs)


def _make_run_finished(**overrides: object) -> events.RunFinished:
    kwargs: dict[str, object] = dict(
        terminal_reason=domain.TerminalReason.VERIFICATION_PASSED,
        total_cost_microusd=0,
        total_tokens=0,
        total_duration_seconds=0.0,
        iterations_used=0,
    )
    kwargs.update(overrides)
    return events.RunFinished(**make_envelope(state=domain.RunState.DONE), **kwargs)


@pytest.mark.parametrize(
    "terminal_reason",
    [
        domain.TerminalReason.VERIFICATION_PASSED,
        domain.TerminalReason.PLAN_REJECTED,
        domain.TerminalReason.BUDGET_EXCEEDED,
        domain.TerminalReason.CANCELLED,
    ],
)
def test_run_finished_forbids_error_for_expected_terminal_reasons(
    terminal_reason: domain.TerminalReason,
) -> None:
    with pytest.raises(ValueError):
        _make_run_finished(
            terminal_reason=terminal_reason,
            error=OperationalError(code=ErrorCode.UNCLASSIFIED_FAILURE, message="x"),
        )


def test_run_finished_requires_error_for_policy_violation() -> None:
    with pytest.raises(ValueError):
        _make_run_finished(terminal_reason=domain.TerminalReason.POLICY_VIOLATION, error=None)


def test_run_finished_accepts_policy_violation_with_severe_code() -> None:
    e = _make_run_finished(
        terminal_reason=domain.TerminalReason.POLICY_VIOLATION,
        error=OperationalError(code=ErrorCode.POLICY_VIOLATION_SEVERE, message="sandbox escape"),
    )
    assert e.error.code is ErrorCode.POLICY_VIOLATION_SEVERE


@pytest.mark.parametrize("code", [c for c in ErrorCode if c is not ErrorCode.POLICY_VIOLATION_SEVERE])
def test_run_finished_rejects_non_severe_codes_for_policy_violation(code: ErrorCode) -> None:
    with pytest.raises(ValueError):
        _make_run_finished(
            terminal_reason=domain.TerminalReason.POLICY_VIOLATION,
            error=OperationalError(code=code, message="x"),
        )


def test_run_finished_requires_error_for_unrecoverable_error() -> None:
    with pytest.raises(ValueError):
        _make_run_finished(terminal_reason=domain.TerminalReason.UNRECOVERABLE_ERROR, error=None)


@pytest.mark.parametrize("code", [c for c in ErrorCode if c is not ErrorCode.POLICY_VIOLATION_SEVERE])
def test_run_finished_accepts_any_non_policy_code_for_unrecoverable_error(
    code: ErrorCode,
) -> None:
    e = _make_run_finished(
        terminal_reason=domain.TerminalReason.UNRECOVERABLE_ERROR,
        error=OperationalError(code=code, message="x"),
    )
    assert e.error.code is code


def test_run_finished_rejects_policy_violation_severe_code_for_unrecoverable_error() -> None:
    with pytest.raises(ValueError):
        _make_run_finished(
            terminal_reason=domain.TerminalReason.UNRECOVERABLE_ERROR,
            error=OperationalError(code=ErrorCode.POLICY_VIOLATION_SEVERE, message="x"),
        )


# --------------------------------------------------------------------
# Cross-cutting: EventType <-> class registry, AnyEvent, pinned values
# --------------------------------------------------------------------


def test_every_event_type_has_exactly_one_class() -> None:
    assert set(events.EVENT_CLASSES_BY_TYPE) == set(events.EventType)


def test_every_registered_class_reports_its_own_key() -> None:
    for event_type, cls in events.EVENT_CLASSES_BY_TYPE.items():
        assert cls.event_type is event_type


def test_any_event_union_matches_the_registry_exactly() -> None:
    members = set(typing.get_args(events.AnyEvent))
    assert members == set(events.EVENT_CLASSES_BY_TYPE.values())


def test_event_type_values_are_pinned() -> None:
    assert {t.value for t in events.EventType} == {
        "run_started",
        "baseline_recorded",
        "state_transitioned",
        "model_request_started",
        "model_request_failed",
        "model_response_received",
        "tool_requested",
        "policy_decision_recorded",
        "tool_completed",
        "plan_proposed",
        "approval_recorded",
        "patch_applied",
        "checkpoint_created",
        "verification_completed",
        "budget_exceeded",
        "run_finished",
    }


def test_verification_outcome_values_are_pinned() -> None:
    assert {o.value for o in events.VerificationOutcome} == {
        "passed",
        "test_failure",
        "timeout",
        "environment_failure",
        "command_start_failure",
    }


def test_model_response_status_values_are_pinned() -> None:
    assert {s.value for s in events.ModelResponseStatus} == {
        "completed",
        "failed",
        "in_progress",
        "cancelled",
        "queued",
        "incomplete",
    }
