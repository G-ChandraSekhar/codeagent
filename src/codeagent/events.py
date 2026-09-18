"""Event schemas for the CodeAgent append-only event log.

Scope: Milestone 0, event-schemas step only (per guide §10). This module
defines the typed shape of each event — it does not implement an event
sink, JSONL (de)serialization, or secret redaction. Those are separate,
later work (see "Deferred" note below).

Every event shares a common envelope (run_id, sequence, timestamp,
state, iteration, schema_version) plus a payload specific to what
happened. Each event type is its own frozen dataclass rather than one
generic envelope-plus-blob, so a caller building e.g. a PatchApplied
event gets real field names and types instead of an untyped mapping.

Model-response status/usage shape (ModelResponseStatus, optional token
counts, incomplete_reason, request_id correlation) is a *documentation*-
informed design, based on the OpenAI Responses API reference describing
usage as optional and distinguishing completed/failed/in_progress/
cancelled/queued/incomplete states. It has not been confirmed against
the API's live behavior — that confirmation is S2a (the Responses API
spike), which has not run yet. Treat this shape as provisional until
S2a executes and either confirms or revises it.

Deferred to later steps, not in this file:
- EventSink / append-only persistence and JSONL encoding.
- Secret redaction (ToolRequested.redacted_arguments_json must already
  be redacted by the writer before persistence — this module only
  validates that the string is both well-formed JSON and already in
  this module's canonical serialization; it does not redact anything).
- A schema-version migration strategy beyond the plain int field below.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, unique
from typing import ClassVar

from codeagent import domain
from codeagent.domain import (
    ApprovalDecision,
    ApprovalMode,
    BudgetKind,
    RunState,
    TerminalReason,
    ToolName,
    Trigger,
)
from codeagent.errors import ErrorCode, OperationalError

# Value strings are a stable, persisted serialization contract, same as
# domain.py's enums: these are written into JSONL event logs. Do not
# rename or renumber an existing member's value — add a new one instead.


@unique
class EventType(str, Enum):
    RUN_STARTED = "run_started"
    BASELINE_RECORDED = "baseline_recorded"
    STATE_TRANSITIONED = "state_transitioned"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_FAILED = "model_request_failed"
    MODEL_RESPONSE_RECEIVED = "model_response_received"
    TOOL_REQUESTED = "tool_requested"
    POLICY_DECISION_RECORDED = "policy_decision_recorded"
    TOOL_COMPLETED = "tool_completed"
    PLAN_PROPOSED = "plan_proposed"
    APPROVAL_RECORDED = "approval_recorded"
    PATCH_APPLIED = "patch_applied"
    CHECKPOINT_CREATED = "checkpoint_created"
    VERIFICATION_COMPLETED = "verification_completed"
    BUDGET_EXCEEDED = "budget_exceeded"
    RUN_FINISHED = "run_finished"
    EVIDENCE_CAPTURED = "evidence_captured"


@unique
class VerificationOutcome(str, Enum):
    """Distinguishes why a baseline or verification run did or didn't
    pass — a bare boolean can't tell a real test failure apart from the
    command never starting."""

    PASSED = "passed"
    TEST_FAILURE = "test_failure"
    TIMEOUT = "timeout"
    ENVIRONMENT_FAILURE = "environment_failure"
    COMMAND_START_FAILURE = "command_start_failure"


@unique
class ContainerCleanupStatus(str, Enum):
    """Whether a verification container's cleanup was actually
    confirmed — Milestone 2 slice 2B-2 (ADR 0003 Amendment 2). Replaces
    a bare boolean/defaulted cleanup flag: there is no permissive
    default anywhere this is used, so every construction site must
    supply one explicitly.

    - `NOT_APPLICABLE`: `docker create` was never invoked for this
      attempt — nothing could have been left behind. Legal only for a
      failure that occurs *before* the create invocation begins; once
      that invocation is attempted (including a bare launch failure),
      cleanup must be confirmed one way or the other, never reported as
      not-applicable.
    - `CONFIRMED_ABSENT`: cleanup ran and a genuine listing afterward
      confirmed the container's exact name is gone.
    - `UNCONFIRMED`: cleanup was attempted but could not be confirmed —
      a nonzero exit, an unparseable listing, or the name still present.
      A cleanup *attempt* is not a cleanup *guarantee*.
    """

    NOT_APPLICABLE = "not_applicable"
    CONFIRMED_ABSENT = "confirmed_absent"
    UNCONFIRMED = "unconfirmed"


@unique
class ModelResponseStatus(str, Enum):
    """Provisional — see module docstring: documentation-informed, not
    yet confirmed against the live API (S2a spike, unrun)."""

    COMPLETED = "completed"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    CANCELLED = "cancelled"
    QUEUED = "queued"
    INCOMPLETE = "incomplete"


def _require_nonempty(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a nonempty string")


def _require_nonempty_if_present(name: str, value: str | None) -> None:
    if value is not None and not value:
        raise ValueError(f"{name} must be None or a nonempty string, got {value!r}")


def _require_nonempty_tuple(name: str, value: tuple[str, ...]) -> None:
    if not value:
        raise ValueError(f"{name} must be a nonempty tuple")
    for i, item in enumerate(value):
        if not item:
            raise ValueError(f"{name}[{i}] must be a nonempty string")


def _require_nonnegative(name: str, value: int | float | None) -> None:
    """Reject a negative, NaN, or infinite value. None is always allowed
    (fields using this helper are optional unless required elsewhere)."""
    if value is None:
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


def _validate_exit_code_for_outcome(outcome: VerificationOutcome, exit_code: int | None) -> None:
    """Exit-code rules per outcome:
    - PASSED requires exit_code == 0.
    - TEST_FAILURE requires a present, nonzero exit_code.
    - COMMAND_START_FAILURE requires exit_code is None (the command
      never ran, so there is nothing to report an exit code for).
    - TIMEOUT and ENVIRONMENT_FAILURE permit either None or any integer
      exit code — e.g. a killed process's negative POSIX signal-based
      return code (Linux/macOS) if one is available, or None if it
      isn't (e.g. the sandbox reports a timeout without a process exit
      code at all).
    """
    if outcome is VerificationOutcome.PASSED:
        if exit_code != 0:
            raise ValueError(f"exit_code must be 0 when outcome is PASSED, got {exit_code!r}")
    elif outcome is VerificationOutcome.TEST_FAILURE:
        if exit_code is None or exit_code == 0:
            raise ValueError(
                "exit_code must be a present, nonzero value when outcome is "
                f"TEST_FAILURE, got {exit_code!r}"
            )
    elif outcome is VerificationOutcome.COMMAND_START_FAILURE:
        if exit_code is not None:
            raise ValueError(
                "exit_code must be None when outcome is COMMAND_START_FAILURE "
                f"(the command never ran), got {exit_code!r}"
            )
    # TIMEOUT and ENVIRONMENT_FAILURE: any int or None is permitted.


# The ErrorCode(s) each non-passing, non-test-failure outcome accepts
# (point 7 of the error-taxonomy correction pass): `outcome` stays the
# single source of truth, and `error.code` is validated against it
# rather than being an independent, possibly-drifting fact. Most
# outcomes accept exactly one code; ENVIRONMENT_FAILURE accepts two —
# the general EXECUTOR_ENVIRONMENT_FAILURE and the narrower, Milestone-3
# EXECUTOR_OOM_KILLED (a Docker-confirmed OOM kill) — both are still
# ENVIRONMENT_FAILURE outcomes, just with a more specific cause in the
# OOM case. Every other outcome/error combination stays fail-closed.
_EXECUTOR_ERROR_CODES_BY_OUTCOME: dict[VerificationOutcome, frozenset[ErrorCode]] = {
    VerificationOutcome.TIMEOUT: frozenset({ErrorCode.EXECUTOR_TIMEOUT}),
    VerificationOutcome.ENVIRONMENT_FAILURE: frozenset(
        {ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE, ErrorCode.EXECUTOR_OOM_KILLED}
    ),
    VerificationOutcome.COMMAND_START_FAILURE: frozenset({ErrorCode.EXECUTOR_COMMAND_START_FAILED}),
}


def _validate_error_for_verification_outcome(
    outcome: VerificationOutcome, error: OperationalError | None
) -> None:
    expected_codes = _EXECUTOR_ERROR_CODES_BY_OUTCOME.get(outcome)
    if expected_codes is None:
        # PASSED or TEST_FAILURE: both are expected domain outcomes, not
        # errors (error-taxonomy requirement 2) — no error permitted.
        if error is not None:
            raise ValueError(f"error must be None when outcome is {outcome!r}, got {error!r}")
    elif error is None or error.code not in expected_codes:
        raise ValueError(
            f"error must be set with a code in "
            f"{sorted(c.value for c in expected_codes)!r} when outcome is {outcome!r}, "
            f"got {error!r}"
        )


def _validate_cleanup_status_for_outcome(
    outcome: VerificationOutcome, cleanup_status: ContainerCleanupStatus
) -> None:
    """Cleanup-status/outcome consistency (Milestone 2 slice 2B-2):

    - `UNCONFIRMED` implies `outcome is ENVIRONMENT_FAILURE` — the
      executor already overrides any provisional outcome to
      ENVIRONMENT_FAILURE whenever cleanup cannot be confirmed, so
      PASSED/TEST_FAILURE/TIMEOUT/COMMAND_START_FAILURE can never pair
      with UNCONFIRMED.
    - `NOT_APPLICABLE` implies `outcome` is `COMMAND_START_FAILURE` or
      `ENVIRONMENT_FAILURE` — only a failure before any container ever
      ran can have nothing to clean up; PASSED/TEST_FAILURE/TIMEOUT all
      require a container to have actually run.
    - `CONFIRMED_ABSENT` is legal for every outcome.
    """
    if cleanup_status is ContainerCleanupStatus.UNCONFIRMED:
        if outcome is not VerificationOutcome.ENVIRONMENT_FAILURE:
            raise ValueError(
                "cleanup_status UNCONFIRMED requires outcome ENVIRONMENT_FAILURE, "
                f"got {outcome!r}"
            )
    elif cleanup_status is ContainerCleanupStatus.NOT_APPLICABLE:
        if outcome not in (
            VerificationOutcome.COMMAND_START_FAILURE,
            VerificationOutcome.ENVIRONMENT_FAILURE,
        ):
            raise ValueError(
                "cleanup_status NOT_APPLICABLE requires outcome COMMAND_START_FAILURE or "
                f"ENVIRONMENT_FAILURE, got {outcome!r}"
            )


def validate_verification_outcome_shape(
    outcome: VerificationOutcome,
    exit_code: int | None,
    error: OperationalError | None,
    cleanup_status: ContainerCleanupStatus,
) -> None:
    """The single, public rule set for whether an (outcome, exit_code,
    error, cleanup_status) quadruple is internally consistent — shared
    by BaselineRecorded, VerificationCompleted, and
    controller.VerificationResult.

    This is the one function outside this module that production code
    (controller.py, executor.py) may call to validate a verification
    outcome's shape; the underscore-prefixed helpers above are this
    module's own implementation detail. Keeping validation behind one
    public entry point, rather than private functions called directly
    from another module, is what makes it impossible for
    VerificationResult's rules to quietly drift from these events'
    rules — see tests/unit/test_verification_result_parity.py.
    """
    _validate_exit_code_for_outcome(outcome, exit_code)
    _validate_error_for_verification_outcome(outcome, error)
    _validate_cleanup_status_for_outcome(outcome, cleanup_status)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in redacted_arguments_json")
        seen.add(key)
        result[key] = value
    return result


def _reject_nonfinite_constant(token: str) -> float:
    raise ValueError(f"nonfinite numeric token {token!r} is not allowed in redacted_arguments_json")


def _validate_canonical_redacted_arguments_json(value: str) -> None:
    """Validate that `value` is both well-formed JSON encoding an object
    and already written in this module's project-specific canonical
    form: keys sorted (recursively, at every nesting level), compact
    separators (no incidental whitespace), Unicode characters preserved
    rather than \\uXXXX-escaped, no duplicate object keys, and no NaN /
    Infinity / -Infinity tokens.

    This is NOT a JSON standard — it exists only so two independently
    produced JSON strings for the same logical arguments compare equal
    byte-for-byte in the event log. It says nothing about whether the
    arguments have actually been redacted; that redaction step is
    separate, later work (see module docstring).
    """
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"redacted_arguments_json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"redacted_arguments_json must encode a JSON object, got {type(parsed).__name__}"
        )
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if canonical != value:
        raise ValueError(
            "redacted_arguments_json is not in canonical form "
            f"(expected {canonical!r}, got {value!r})"
        )


@dataclass(frozen=True, kw_only=True)
class Event:
    """Common envelope fields shared by every event type.

    Event ordering convention: a causative event — ToolRequested,
    PlanProposed, PatchApplied, VerificationCompleted, BudgetExceeded,
    and the like — is emitted while the run is still in the
    controller's current/source state: the state the event's cause
    happened in, before any resulting transition. StateTransitioned is
    then emitted separately, with `state` equal to `to_state` (the
    state the run has just entered) and the prior state carried in
    `from_state`. RunFinished is always emitted in DONE, after the
    terminal StateTransitioned event.
    """

    run_id: str
    sequence: int
    timestamp: datetime
    state: RunState
    iteration: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_nonempty("run_id", self.run_id)
        _require_nonnegative("sequence", self.sequence)
        _require_nonnegative("iteration", self.iteration)
        if self.schema_version < 1:
            raise ValueError(f"schema_version must be >= 1, got {self.schema_version!r}")
        if self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    event_type: ClassVar[EventType] = EventType.RUN_STARTED
    repository_path: str
    task_statement: str
    verify_command: tuple[str, ...]
    approval_mode: ApprovalMode

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("repository_path", self.repository_path)
        _require_nonempty("task_statement", self.task_statement)
        _require_nonempty_tuple("verify_command", self.verify_command)


@dataclass(frozen=True, kw_only=True)
class BaselineRecorded(Event):
    event_type: ClassVar[EventType] = EventType.BASELINE_RECORDED
    outcome: VerificationOutcome
    command: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    cleanup_status: ContainerCleanupStatus
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty_tuple("command", self.command)
        _require_nonnegative("duration_seconds", self.duration_seconds)
        validate_verification_outcome_shape(
            self.outcome, self.exit_code, self.error, self.cleanup_status
        )


@dataclass(frozen=True, kw_only=True)
class StateTransitioned(Event):
    event_type: ClassVar[EventType] = EventType.STATE_TRANSITIONED
    trigger: Trigger
    from_state: RunState
    to_state: RunState
    terminal_reason: TerminalReason | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.to_state is not self.state:
            raise ValueError(
                "StateTransitioned.state must equal to_state "
                f"(state={self.state!r}, to_state={self.to_state!r})"
            )
        try:
            result = domain.transition(self.from_state, self.trigger)
        except domain.IllegalTransitionError as exc:
            raise ValueError(
                f"StateTransitioned claims an impossible transition: {exc}"
            ) from exc
        if result.next_state is not self.to_state:
            raise ValueError(
                "StateTransitioned.to_state does not match domain.transition's result "
                f"(claimed to_state={self.to_state!r}, actual={result.next_state!r})"
            )
        if result.terminal_reason is not self.terminal_reason:
            raise ValueError(
                "StateTransitioned.terminal_reason does not match domain.transition's result "
                f"(claimed={self.terminal_reason!r}, actual={result.terminal_reason!r})"
            )


@dataclass(frozen=True, kw_only=True)
class ModelRequestStarted(Event):
    event_type: ClassVar[EventType] = EventType.MODEL_REQUEST_STARTED
    request_id: str
    model_name: str
    input_token_estimate: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("request_id", self.request_id)
        _require_nonempty("model_name", self.model_name)
        _require_nonnegative("input_token_estimate", self.input_token_estimate)


@dataclass(frozen=True, kw_only=True)
class ModelRequestFailed(Event):
    """Represents an outbound model request attempt for which no
    provider response object was obtained — connection failure,
    auth/config failure, or the request could not even be submitted.
    ModelResponseReceived's response_id stays required and honest (a
    response really did come back) rather than being made optional to
    let one event mean two different things; see the error-taxonomy
    design discussion for the comparison with a generic OperationFailed
    event (rejected) and an optional response_id (rejected).

    This event's cardinality relative to ModelRequestStarted /
    ModelResponseReceived is NOT guaranteed by this module. A started
    request may end with no terminal event at all if the controller or
    host process crashes before either event is recorded. Whether an
    in-flight QUEUED/IN_PROGRESS observation should itself be treated
    as a terminal-ish state for this purpose is also unresolved —
    provisional until S2a (the Responses API spike) and later
    controller/recovery design settle real observed behavior; no
    recovery or reconciliation behavior is implemented or assumed here.
    """

    event_type: ClassVar[EventType] = EventType.MODEL_REQUEST_FAILED
    request_id: str
    error: OperationalError

    _ALLOWED_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        {ErrorCode.MODEL_PROVIDER_REQUEST_FAILED, ErrorCode.MODEL_PROVIDER_AUTH_FAILED}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("request_id", self.request_id)
        if self.error.code not in self._ALLOWED_ERROR_CODES:
            raise ValueError(
                "error.code must be one of "
                f"{sorted(c.value for c in self._ALLOWED_ERROR_CODES)}, got "
                f"{self.error.code!r}"
            )


@dataclass(frozen=True, kw_only=True)
class ModelResponseReceived(Event):
    event_type: ClassVar[EventType] = EventType.MODEL_RESPONSE_RECEIVED
    request_id: str
    model_name: str
    response_id: str
    status: ModelResponseStatus
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_seconds: float
    tool_call_count: int
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("request_id", self.request_id)
        _require_nonempty("model_name", self.model_name)
        _require_nonempty("response_id", self.response_id)
        _require_nonnegative("input_tokens", self.input_tokens)
        _require_nonnegative("output_tokens", self.output_tokens)
        _require_nonnegative("latency_seconds", self.latency_seconds)
        _require_nonnegative("tool_call_count", self.tool_call_count)
        is_incomplete = self.status is ModelResponseStatus.INCOMPLETE
        has_reason = self.incomplete_reason is not None
        if is_incomplete != has_reason:
            raise ValueError(
                "incomplete_reason must be set if and only if status is INCOMPLETE "
                f"(status={self.status!r}, incomplete_reason={self.incomplete_reason!r})"
            )
        if self.incomplete_reason is not None:
            _require_nonempty("incomplete_reason", self.incomplete_reason)

        if self.status is ModelResponseStatus.FAILED:
            if self.error is None or self.error.code is not ErrorCode.MODEL_RESPONSE_FAILED_STATUS:
                raise ValueError(
                    "error must be set with code MODEL_RESPONSE_FAILED_STATUS when status "
                    f"is FAILED, got {self.error!r}"
                )
        elif self.status is ModelResponseStatus.COMPLETED:
            if self.error is not None and self.error.code is not ErrorCode.MODEL_RESPONSE_MALFORMED:
                raise ValueError(
                    "error, if set while status is COMPLETED, must have code "
                    f"MODEL_RESPONSE_MALFORMED, got {self.error!r}"
                )
        elif self.error is not None:
            raise ValueError(f"error must be None when status is {self.status!r}, got {self.error!r}")


@dataclass(frozen=True, kw_only=True)
class ToolRequested(Event):
    event_type: ClassVar[EventType] = EventType.TOOL_REQUESTED
    tool: ToolName
    tool_call_id: str
    # Must already be redacted by the caller before this event is
    # constructed — see module docstring. Validated here only for being
    # well-formed JSON encoding an object *and* already written in this
    # module's canonical form (see _validate_canonical_redacted_arguments_json);
    # no redaction is performed or verified by this validation.
    redacted_arguments_json: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("tool_call_id", self.tool_call_id)
        _validate_canonical_redacted_arguments_json(self.redacted_arguments_json)


@dataclass(frozen=True, kw_only=True)
class PolicyDecisionRecorded(Event):
    event_type: ClassVar[EventType] = EventType.POLICY_DECISION_RECORDED
    tool: ToolName
    tool_call_id: str
    allowed: bool
    reason: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("tool_call_id", self.tool_call_id)
        _require_nonempty("reason", self.reason)


@dataclass(frozen=True, kw_only=True)
class ToolCompleted(Event):
    event_type: ClassVar[EventType] = EventType.TOOL_COMPLETED
    tool: ToolName
    tool_call_id: str
    success: bool
    duration_seconds: float
    result_summary: str
    error: OperationalError | None = None

    # Generic codes are legal for any tool's failure. Patch-specific
    # codes are legal only when tool is APPLY_PATCH — a read tool or
    # propose_plan can never report a patch validation/application
    # failure, since they never touch the worktree.
    _GENERIC_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        {ErrorCode.TOOL_INPUT_INVALID, ErrorCode.TOOL_EXECUTION_FAILED}
    )
    _PATCH_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        {
            ErrorCode.PATCH_VALIDATION_FAILED,
            ErrorCode.PATCH_APPLICATION_FAILED,
            ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE,
            ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE,
            # Milestone 2 slice 2B-2 (ADR 0003 Amendment 2): a
            # single apply_patch transaction now spans the workspace
            # entry gate, lazy checkpoint establishment/advance, and the
            # approved-path postcondition — each can fail this same tool
            # call, and each needs its own precise, legal code here
            # rather than an occurrence with nowhere legal to be
            # reported.
            ErrorCode.WORKSPACE_ENTRY_GATE_FAILED,
            ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED,
            ErrorCode.CHECKPOINT_REF_OPERATION_FAILED,
            ErrorCode.CHECKPOINT_REF_UNEXPECTED_STATE,
            ErrorCode.CHECKPOINT_REF_OUTCOME_UNKNOWN,
            # The approved-path postcondition failure (a PatchApplier
            # that changed files outside the approved plan) — ADR 0003
            # Amendment 1 point 4's ordering.
            ErrorCode.INTERNAL_INVARIANT_VIOLATION,
        }
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("tool_call_id", self.tool_call_id)
        _require_nonnegative("duration_seconds", self.duration_seconds)
        if self.success:
            if self.error is not None:
                raise ValueError(f"error must be None when success is True, got {self.error!r}")
            return
        allowed = self._GENERIC_ERROR_CODES
        if self.tool is ToolName.APPLY_PATCH:
            allowed = allowed | self._PATCH_ERROR_CODES
        if self.error is None or self.error.code not in allowed:
            raise ValueError(
                f"error must be set with a code from {sorted(c.value for c in allowed)} "
                f"for tool {self.tool!r} when success is False, got {self.error!r}"
            )


@dataclass(frozen=True, kw_only=True)
class PlanProposed(Event):
    event_type: ClassVar[EventType] = EventType.PLAN_PROPOSED
    problem_hypothesis: str
    evidence_refs: tuple[str, ...]
    proposed_file_paths: tuple[str, ...]
    verification_intent: str
    risk_notes: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("problem_hypothesis", self.problem_hypothesis)
        _require_nonempty("verification_intent", self.verification_intent)
        for i, item in enumerate(self.evidence_refs):
            if not item:
                raise ValueError(f"evidence_refs[{i}] must be a nonempty string")
        for i, item in enumerate(self.proposed_file_paths):
            if not item:
                raise ValueError(f"proposed_file_paths[{i}] must be a nonempty string")


@dataclass(frozen=True, kw_only=True)
class ApprovalRecorded(Event):
    event_type: ClassVar[EventType] = EventType.APPROVAL_RECORDED
    decision: ApprovalDecision
    approval_mode: ApprovalMode
    operator_note: str | None = None


@dataclass(frozen=True, kw_only=True)
class PatchApplied(Event):
    event_type: ClassVar[EventType] = EventType.PATCH_APPLIED
    operation_count: int
    files_changed: tuple[str, ...]
    checkpoint_id: str
    diff_bytes: int

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("checkpoint_id", self.checkpoint_id)
        if self.operation_count < 1:
            raise ValueError(f"operation_count must be >= 1, got {self.operation_count!r}")
        _require_nonnegative("diff_bytes", self.diff_bytes)
        for i, item in enumerate(self.files_changed):
            if not item:
                raise ValueError(f"files_changed[{i}] must be a nonempty string")


@dataclass(frozen=True, kw_only=True)
class CheckpointCreated(Event):
    event_type: ClassVar[EventType] = EventType.CHECKPOINT_CREATED
    checkpoint_id: str
    parent_checkpoint_id: str | None
    commit_hash: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty("checkpoint_id", self.checkpoint_id)
        _require_nonempty_if_present("parent_checkpoint_id", self.parent_checkpoint_id)
        _require_nonempty("commit_hash", self.commit_hash)


@dataclass(frozen=True, kw_only=True)
class VerificationCompleted(Event):
    event_type: ClassVar[EventType] = EventType.VERIFICATION_COMPLETED
    outcome: VerificationOutcome
    command: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    fail_to_pass: tuple[str, ...]
    pass_to_pass_broken: tuple[str, ...]
    cleanup_status: ContainerCleanupStatus
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonempty_tuple("command", self.command)
        _require_nonnegative("duration_seconds", self.duration_seconds)
        validate_verification_outcome_shape(
            self.outcome, self.exit_code, self.error, self.cleanup_status
        )


@dataclass(frozen=True, kw_only=True)
class BudgetExceeded(Event):
    """Emitted in the controller's current/source state — the state the
    budget was hit in — not in DONE. The subsequent StateTransitioned
    event (trigger=BUDGET_EXCEEDED) is what actually moves the run to
    DONE; see Event's ordering-convention docstring.
    """

    event_type: ClassVar[EventType] = EventType.BUDGET_EXCEEDED
    kind: BudgetKind
    limit_value: int | float
    # Actual consumption at the time the budget check ran.
    observed_value: int | float
    # Consumption if the denied operation had been allowed to proceed —
    # lets the controller block *before* actually overspending, not only
    # report after the fact. None if no such projection was computed.
    projected_value: int | float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_nonnegative("limit_value", self.limit_value)
        _require_nonnegative("observed_value", self.observed_value)
        _require_nonnegative("projected_value", self.projected_value)
        if self.projected_value is not None and self.projected_value < self.observed_value:
            raise ValueError(
                "projected_value must be >= observed_value "
                f"(observed_value={self.observed_value!r}, "
                f"projected_value={self.projected_value!r})"
            )
        reaches_limit = self.observed_value >= self.limit_value or (
            self.projected_value is not None and self.projected_value >= self.limit_value
        )
        if not reaches_limit:
            raise ValueError(
                "either observed_value or projected_value must reach limit_value "
                f"(limit_value={self.limit_value!r}, observed_value={self.observed_value!r}, "
                f"projected_value={self.projected_value!r})"
            )


@dataclass(frozen=True, kw_only=True)
class EvidenceCaptured(Event):
    """Milestone 2 slice 2B-2 (ADR 0003 Amendment 2 / ADR 0006 Amendment
    4): the outcome of attempting to capture and durably publish the
    run's evidence artifact at terminal teardown. Always attempted,
    regardless of whether the run's underlying domain outcome
    succeeded, and never blocks any subsequent cleanup step.

    `artifact_id`, `sha256_payload`, and `bytes_written` are the
    artifact's receipt: present together if and only if an artifact was
    genuinely published under that identity, absent together otherwise
    — never partially present. No raw diff payload or absolute host
    path ever appears in this event; the receipt is metadata about a
    file that exists elsewhere, not the file's content or location.

    Exactly four shapes are legal:
    - complete: `success=True`, `complete=True`, `error=None`, receipt
      present.
    - incomplete preview (truncated, or an untracked path was found):
      `success=False`, `complete=False`,
      `error.code=EVIDENCE_INCOMPLETE`, receipt present (a preview
      artifact really was published).
    - durability unconfirmed (the artifact published successfully, but
      a step after the no-replace publish — a temp-file cleanup or the
      publishing directory's fsync — could not be confirmed):
      `success=False`, `complete=True`,
      `error.code=EVIDENCE_DURABILITY_UNCONFIRMED`, receipt present.
      The published artifact itself is never deleted or overwritten in
      response to this.
    - capture failed outright, or a collision with an existing artifact
      under the same identity: `success=False`, `complete=False`,
      `error.code` one of `EVIDENCE_CAPTURE_FAILED`/
      `EVIDENCE_ARTIFACT_COLLISION`, no receipt (nothing this run
      produced was ever published).
    """

    event_type: ClassVar[EventType] = EventType.EVIDENCE_CAPTURED
    success: bool
    complete: bool
    artifact_id: str | None = None
    sha256_payload: str | None = None
    bytes_written: int | None = None
    error: OperationalError | None = None

    _RECEIPT_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        {ErrorCode.EVIDENCE_INCOMPLETE, ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED}
    )
    _NO_RECEIPT_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        {ErrorCode.EVIDENCE_CAPTURE_FAILED, ErrorCode.EVIDENCE_ARTIFACT_COLLISION}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        receipt_fields_present = (
            self.artifact_id is not None,
            self.sha256_payload is not None,
            self.bytes_written is not None,
        )
        if len(set(receipt_fields_present)) != 1:
            raise ValueError(
                "artifact_id, sha256_payload, and bytes_written must be present or "
                f"absent together, got {receipt_fields_present!r}"
            )
        has_receipt = receipt_fields_present[0]
        if has_receipt:
            _require_nonempty("artifact_id", self.artifact_id)
            _require_nonempty("sha256_payload", self.sha256_payload)
            _require_nonnegative("bytes_written", self.bytes_written)

        if self.success:
            if self.error is not None:
                raise ValueError(f"error must be None when success is True, got {self.error!r}")
            if not self.complete:
                raise ValueError("success=True requires complete=True")
            if not has_receipt:
                raise ValueError("a successful capture must carry receipt metadata")
            return

        if self.error is None:
            raise ValueError("error must be set when success is False")
        code = self.error.code
        if code in self._RECEIPT_ERROR_CODES:
            if not has_receipt:
                raise ValueError(
                    f"error code {code!r} requires receipt metadata "
                    "(an artifact was genuinely published)"
                )
        elif code in self._NO_RECEIPT_ERROR_CODES:
            if has_receipt:
                raise ValueError(
                    f"error code {code!r} must not carry receipt metadata "
                    "(nothing was published)"
                )
        else:
            raise ValueError(
                f"error.code must be one of "
                f"{sorted(c.value for c in self._RECEIPT_ERROR_CODES | self._NO_RECEIPT_ERROR_CODES)}, "
                f"got {code!r}"
            )
        if code is ErrorCode.EVIDENCE_INCOMPLETE and self.complete:
            raise ValueError("EVIDENCE_INCOMPLETE requires complete=False")
        if code is ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED and not self.complete:
            # ADR 0003 Amendment 2's terminal precedence: an incomplete
            # capture that also hits ambiguous post-link housekeeping
            # must be reported as EVIDENCE_INCOMPLETE, never as
            # EVIDENCE_DURABILITY_UNCONFIRMED — this pairing would mean
            # the precedence rule was not applied upstream.
            raise ValueError("EVIDENCE_DURABILITY_UNCONFIRMED requires complete=True")
        if code in self._NO_RECEIPT_ERROR_CODES and self.complete:
            raise ValueError(f"error code {code!r} requires complete=False")


@dataclass(frozen=True, kw_only=True)
class RunFinished(Event):
    event_type: ClassVar[EventType] = EventType.RUN_FINISHED
    terminal_reason: TerminalReason
    total_cost_microusd: int | None
    total_tokens: int
    total_duration_seconds: float
    iterations_used: int
    error: OperationalError | None = None

    # Every ErrorCode except POLICY_VIOLATION_SEVERE, which is reserved
    # for terminal_reason=POLICY_VIOLATION specifically (see below).
    _UNRECOVERABLE_ERROR_CODES: ClassVar[frozenset[ErrorCode]] = frozenset(
        set(ErrorCode) - {ErrorCode.POLICY_VIOLATION_SEVERE}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.state is not RunState.DONE:
            raise ValueError(f"RunFinished.state must be DONE, got {self.state!r}")
        _require_nonnegative("total_cost_microusd", self.total_cost_microusd)
        _require_nonnegative("total_tokens", self.total_tokens)
        _require_nonnegative("total_duration_seconds", self.total_duration_seconds)
        _require_nonnegative("iterations_used", self.iterations_used)

        if self.terminal_reason is TerminalReason.POLICY_VIOLATION:
            if self.error is None or self.error.code is not ErrorCode.POLICY_VIOLATION_SEVERE:
                raise ValueError(
                    "error must be set with code POLICY_VIOLATION_SEVERE when terminal_reason "
                    f"is POLICY_VIOLATION, got {self.error!r}"
                )
        elif self.terminal_reason is TerminalReason.UNRECOVERABLE_ERROR:
            if self.error is None or self.error.code not in self._UNRECOVERABLE_ERROR_CODES:
                raise ValueError(
                    "error must be set with a code other than POLICY_VIOLATION_SEVERE when "
                    f"terminal_reason is UNRECOVERABLE_ERROR, got {self.error!r}"
                )
        elif self.error is not None:
            raise ValueError(
                f"error must be None when terminal_reason is {self.terminal_reason!r}, "
                f"got {self.error!r}"
            )


AnyEvent = (
    RunStarted
    | BaselineRecorded
    | StateTransitioned
    | ModelRequestStarted
    | ModelRequestFailed
    | ModelResponseReceived
    | ToolRequested
    | PolicyDecisionRecorded
    | ToolCompleted
    | PlanProposed
    | ApprovalRecorded
    | PatchApplied
    | CheckpointCreated
    | VerificationCompleted
    | BudgetExceeded
    | RunFinished
    | EvidenceCaptured
)

EVENT_CLASSES_BY_TYPE: dict[EventType, type[Event]] = {
    RunStarted.event_type: RunStarted,
    BaselineRecorded.event_type: BaselineRecorded,
    StateTransitioned.event_type: StateTransitioned,
    ModelRequestStarted.event_type: ModelRequestStarted,
    ModelRequestFailed.event_type: ModelRequestFailed,
    ModelResponseReceived.event_type: ModelResponseReceived,
    ToolRequested.event_type: ToolRequested,
    PolicyDecisionRecorded.event_type: PolicyDecisionRecorded,
    ToolCompleted.event_type: ToolCompleted,
    PlanProposed.event_type: PlanProposed,
    ApprovalRecorded.event_type: ApprovalRecorded,
    PatchApplied.event_type: PatchApplied,
    CheckpointCreated.event_type: CheckpointCreated,
    VerificationCompleted.event_type: VerificationCompleted,
    BudgetExceeded.event_type: BudgetExceeded,
    RunFinished.event_type: RunFinished,
    EvidenceCaptured.event_type: EvidenceCaptured,
}
