"""Error taxonomy for CodeAgent.

Scope: Milestone 0, error-taxonomy step. Defines a stable, persisted
vocabulary for genuine *operational failures* — things going wrong in a
way the system did not intend — as distinct from ordinary domain
outcomes already represented elsewhere and deliberately NOT treated as
errors here:

- verification test failures (events.VerificationOutcome.TEST_FAILURE)
- budget exhaustion (domain.TerminalReason.BUDGET_EXCEEDED)
- plan rejection (domain.TerminalReason.PLAN_REJECTED)
- user/controller cancellation (domain.TerminalReason.CANCELLED,
  events.ModelResponseStatus.CANCELLED)
- an ordinary denied tool request (events.PolicyDecisionRecorded with
  allowed=False) — a single denial that does not end the run
- a model response that finished INCOMPLETE (e.g. hit a token limit) —
  a model-behavior outcome, not a system failure

ErrorDomain classifies *what kind of thing failed*. It carries no
disposition semantics: whether a given occurrence should be retried,
escalated, or made terminal depends on controller state, remaining
budgets, and attempt count — none of which exist yet, since no
controller has been built. A future controller makes that decision
using an ErrorCode plus that runtime context; this module deliberately
does not, and no field here should be read as an unconditional retry
rule.

domain.IllegalTransitionError and domain.IllegalToolCallError remain
local Python exceptions in domain.py, unrelated to this taxonomy — they
represent domain.py's own synchronous, fail-closed defenses against a
controller bug, not an operational failure of the kind represented
here. domain.py does not import this module. If a future controller
ever catches one of those exceptions anyway (meaning a bug slipped past
domain.py's own guards), it may translate that into
OperationalError(code=ErrorCode.INTERNAL_INVARIANT_VIOLATION, ...) so
the run still ends with an honest, recorded reason — but that
translation is the controller's job, not something this module or
domain.py does.

Deferred to later work, not in this file:
- The controller-owned retry/abort/escalation policy that actually
  consults ErrorDomain/ErrorCode plus runtime context.
- A fallback observability path for the case PERSISTENCE_WRITE_FAILED
  describes but may not itself be able to record, if the very sink that
  failed is what would have to record it (see PERSISTENCE_WRITE_FAILED
  below).

`OperationalError.error_id` (below) was deliberately deferred in the
original design pass — no design at that point referenced one failure
from two events. The Milestone 1 controller then produced a concrete
case (one apply_patch failure represented by both a ToolCompleted and
the RunFinished that cites it), so the deferral's own stated
reconsideration condition was met and error_id was added. See
ENGINEERING_LOG.md for the evidence-driven reversal entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique


@unique
class ErrorDomain(str, Enum):
    """What kind of thing failed. Purely descriptive — see module
    docstring: this is not a retry/terminal/escalate decision."""

    MODEL_PROVIDER = "model_provider"
    MODEL_RESPONSE = "model_response"
    TOOL_INPUT = "tool_input"
    TOOL_EXECUTION = "tool_execution"
    PATCH = "patch"
    EXECUTOR = "executor"
    PERSISTENCE = "persistence"
    POLICY = "policy"
    INTERNAL = "internal"


@unique
class ErrorCode(str, Enum):
    """Stable, persisted failure identifiers. Value strings are a
    serialization contract like domain.py's and events.py's enums — do
    not rename or renumber an existing member; add a new one instead.
    """

    # ErrorDomain.MODEL_PROVIDER — the provider never returned a
    # response object at all (see events.ModelRequestFailed).
    MODEL_PROVIDER_REQUEST_FAILED = "model_provider_request_failed"
    MODEL_PROVIDER_AUTH_FAILED = "model_provider_auth_failed"

    # ErrorDomain.MODEL_RESPONSE — the provider did return a response,
    # but either its own status was FAILED, or the controller's
    # downstream parsing of an otherwise-successful response failed.
    MODEL_RESPONSE_FAILED_STATUS = "model_response_failed_status"
    MODEL_RESPONSE_MALFORMED = "model_response_malformed"

    # ErrorDomain.TOOL_INPUT — a dispatched tool call's arguments failed
    # validation before the tool ran.
    TOOL_INPUT_INVALID = "tool_input_invalid"

    # ErrorDomain.TOOL_EXECUTION — a dispatched tool raised an
    # unexpected failure while actually running.
    TOOL_EXECUTION_FAILED = "tool_execution_failed"

    # ErrorDomain.PATCH — apply_patch (a tool call) failed either during
    # proposal validation or during application to the worktree.
    PATCH_VALIDATION_FAILED = "patch_validation_failed"
    PATCH_APPLICATION_FAILED = "patch_application_failed"

    # ErrorDomain.EXECUTOR — the baseline/verification command executor
    # failed independently of whether the command under test passed.
    EXECUTOR_COMMAND_START_FAILED = "executor_command_start_failed"
    EXECUTOR_TIMEOUT = "executor_timeout"
    EXECUTOR_ENVIRONMENT_FAILURE = "executor_environment_failure"

    # ErrorDomain.PERSISTENCE — the event log or checkpoint store could
    # not be durably written.
    PERSISTENCE_WRITE_FAILED = "persistence_write_failed"

    # ErrorDomain.POLICY — a severe policy breach, as opposed to an
    # ordinary single denied tool request (events.PolicyDecisionRecorded
    # with allowed=False), which is not an error at all.
    POLICY_VIOLATION_SEVERE = "policy_violation_severe"

    # ErrorDomain.INTERNAL — a bug in CodeAgent itself, not the target
    # repository or the model; or a terminal operational failure this
    # taxonomy did not anticipate (see UNCLASSIFIED_FAILURE's own note
    # on where it may legitimately appear).
    INTERNAL_INVARIANT_VIOLATION = "internal_invariant_violation"
    # Permitted only as RunFinished.error when terminal_reason is
    # UNRECOVERABLE_ERROR (see events.py's validation). Every other
    # event in this codebase requires a precise, domain-specific code,
    # because what happened is already known at the point that event is
    # recorded — there is no legitimate case for using this code
    # anywhere but the run's final, single terminal report. A real
    # occurrence is a signal the taxonomy is missing a code, not a
    # normal outcome.
    UNCLASSIFIED_FAILURE = "unclassified_failure"


ERROR_DOMAIN_BY_CODE: dict[ErrorCode, ErrorDomain] = {
    ErrorCode.MODEL_PROVIDER_REQUEST_FAILED: ErrorDomain.MODEL_PROVIDER,
    ErrorCode.MODEL_PROVIDER_AUTH_FAILED: ErrorDomain.MODEL_PROVIDER,
    ErrorCode.MODEL_RESPONSE_FAILED_STATUS: ErrorDomain.MODEL_RESPONSE,
    ErrorCode.MODEL_RESPONSE_MALFORMED: ErrorDomain.MODEL_RESPONSE,
    ErrorCode.TOOL_INPUT_INVALID: ErrorDomain.TOOL_INPUT,
    ErrorCode.TOOL_EXECUTION_FAILED: ErrorDomain.TOOL_EXECUTION,
    ErrorCode.PATCH_VALIDATION_FAILED: ErrorDomain.PATCH,
    ErrorCode.PATCH_APPLICATION_FAILED: ErrorDomain.PATCH,
    ErrorCode.EXECUTOR_COMMAND_START_FAILED: ErrorDomain.EXECUTOR,
    ErrorCode.EXECUTOR_TIMEOUT: ErrorDomain.EXECUTOR,
    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE: ErrorDomain.EXECUTOR,
    ErrorCode.PERSISTENCE_WRITE_FAILED: ErrorDomain.PERSISTENCE,
    ErrorCode.POLICY_VIOLATION_SEVERE: ErrorDomain.POLICY,
    ErrorCode.INTERNAL_INVARIANT_VIOLATION: ErrorDomain.INTERNAL,
    ErrorCode.UNCLASSIFIED_FAILURE: ErrorDomain.INTERNAL,
}


def domain_of(code: ErrorCode) -> ErrorDomain:
    return ERROR_DOMAIN_BY_CODE[code]


def _require_nonempty(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True, kw_only=True)
class OperationalError:
    """A stable, persisted failure code plus a sanitized, human-readable
    summary.

    `code` is the serialization identifier: stable, matched
    programmatically, never renamed once persisted (see ErrorCode).
    `error_id` identifies one specific *occurrence* of a failure — the
    caller generates it once and reuses the same value verbatim on
    every event representing that same occurrence (e.g. a failed
    ToolCompleted and the RunFinished that cites it), so a reader can
    join them. A new failure occurrence gets a new error_id even if it
    has the same `code`. `message` is free text for a human reading the
    trace — it must already be sanitized by the caller before
    construction (no raw exception tracebacks, environment variables,
    model arguments, subprocess output, secrets, or unrestricted
    provider payloads); this module cannot verify that mechanically,
    the same way events.ToolRequested cannot verify its arguments were
    actually redacted. `message` must never be parsed or matched
    against by other code — only `code` and `error_id` are
    serialization identifiers.
    """

    code: ErrorCode
    error_id: str
    message: str

    def __post_init__(self) -> None:
        _require_nonempty("error_id", self.error_id)
        _require_nonempty("message", self.message)
