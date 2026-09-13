"""Exhaustive tests for the Milestone 0 error taxonomy in errors.py.

Covers: OperationalError construction/validation, ErrorCode <->
ErrorDomain mapping completeness, pinned enum values, and — per the
error-taxonomy requirement that expected outcomes must never be
accidentally classified as errors — explicit checks that ErrorCode's
vocabulary is disjoint from domain.py's and events.py's "not an error"
outcomes.
"""

from __future__ import annotations

import pytest

from codeagent import domain
from codeagent.errors import ERROR_DOMAIN_BY_CODE, ErrorCode, ErrorDomain, OperationalError, domain_of

ALL_CODES = list(ErrorCode)
ALL_DOMAINS = list(ErrorDomain)


# --------------------------------------------------------------------
# OperationalError construction / validation
# --------------------------------------------------------------------


@pytest.mark.parametrize("code", ALL_CODES)
def test_operational_error_construction_with_every_code(code: ErrorCode) -> None:
    e = OperationalError(code=code, error_id="err-1", message="a sanitized summary")
    assert e.code is code
    assert e.error_id == "err-1"
    assert e.message == "a sanitized summary"


def test_operational_error_rejects_empty_message() -> None:
    with pytest.raises(ValueError):
        OperationalError(code=ErrorCode.TOOL_EXECUTION_FAILED, error_id="err-1", message="")


def test_operational_error_rejects_empty_error_id() -> None:
    with pytest.raises(ValueError):
        OperationalError(code=ErrorCode.TOOL_EXECUTION_FAILED, error_id="", message="x")


def test_operational_error_reuses_the_same_error_id_across_instances() -> None:
    """The whole point of error_id: two OperationalError instances
    representing the same failure occurrence carry identical error_id
    values even though they're separate objects (e.g. one attached to
    a ToolCompleted, one to a RunFinished)."""
    shared_id = "err-shared-1"
    first = OperationalError(
        code=ErrorCode.PATCH_VALIDATION_FAILED, error_id=shared_id, message="a"
    )
    second = OperationalError(
        code=ErrorCode.PATCH_VALIDATION_FAILED, error_id=shared_id, message="a, restated"
    )
    assert first.error_id == second.error_id == shared_id


# --------------------------------------------------------------------
# ErrorDomain mapping completeness
# --------------------------------------------------------------------


def test_every_error_code_has_exactly_one_domain() -> None:
    assert set(ERROR_DOMAIN_BY_CODE) == set(ErrorCode)


def test_every_domain_is_used_by_at_least_one_code() -> None:
    assert set(ERROR_DOMAIN_BY_CODE.values()) == set(ErrorDomain)


@pytest.mark.parametrize("code", ALL_CODES)
def test_domain_of_matches_the_fixed_mapping(code: ErrorCode) -> None:
    assert domain_of(code) is ERROR_DOMAIN_BY_CODE[code]


@pytest.mark.parametrize(
    ("code", "expected_domain"),
    [
        (ErrorCode.MODEL_PROVIDER_REQUEST_FAILED, ErrorDomain.MODEL_PROVIDER),
        (ErrorCode.MODEL_PROVIDER_AUTH_FAILED, ErrorDomain.MODEL_PROVIDER),
        (ErrorCode.MODEL_RESPONSE_FAILED_STATUS, ErrorDomain.MODEL_RESPONSE),
        (ErrorCode.MODEL_RESPONSE_MALFORMED, ErrorDomain.MODEL_RESPONSE),
        (ErrorCode.TOOL_INPUT_INVALID, ErrorDomain.TOOL_INPUT),
        (ErrorCode.TOOL_EXECUTION_FAILED, ErrorDomain.TOOL_EXECUTION),
        (ErrorCode.PATCH_VALIDATION_FAILED, ErrorDomain.PATCH),
        (ErrorCode.PATCH_APPLICATION_FAILED, ErrorDomain.PATCH),
        (ErrorCode.EXECUTOR_COMMAND_START_FAILED, ErrorDomain.EXECUTOR),
        (ErrorCode.EXECUTOR_TIMEOUT, ErrorDomain.EXECUTOR),
        (ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE, ErrorDomain.EXECUTOR),
        (ErrorCode.EXECUTOR_OOM_KILLED, ErrorDomain.EXECUTOR),
        (ErrorCode.PERSISTENCE_WRITE_FAILED, ErrorDomain.PERSISTENCE),
        (ErrorCode.POLICY_VIOLATION_SEVERE, ErrorDomain.POLICY),
        (ErrorCode.INTERNAL_INVARIANT_VIOLATION, ErrorDomain.INTERNAL),
        (ErrorCode.UNCLASSIFIED_FAILURE, ErrorDomain.INTERNAL),
    ],
)
def test_specific_code_to_domain_assignments(
    code: ErrorCode, expected_domain: ErrorDomain
) -> None:
    assert domain_of(code) is expected_domain


# --------------------------------------------------------------------
# Pinned values
# --------------------------------------------------------------------


def test_error_domain_values_are_pinned() -> None:
    assert {d.value for d in ErrorDomain} == {
        "model_provider",
        "model_response",
        "tool_input",
        "tool_execution",
        "patch",
        "executor",
        "persistence",
        "policy",
        "internal",
    }


def test_error_code_values_are_pinned() -> None:
    assert {c.value for c in ErrorCode} == {
        "model_provider_request_failed",
        "model_provider_auth_failed",
        "model_response_failed_status",
        "model_response_malformed",
        "tool_input_invalid",
        "tool_execution_failed",
        "patch_validation_failed",
        "patch_application_failed",
        "executor_command_start_failed",
        "executor_timeout",
        "executor_environment_failure",
        "executor_oom_killed",
        "persistence_write_failed",
        "policy_violation_severe",
        "internal_invariant_violation",
        "unclassified_failure",
    }


# --------------------------------------------------------------------
# Expected outcomes are never accidentally classified as errors
# --------------------------------------------------------------------

# The values these outcomes use in domain.py / events.py -- ErrorCode
# must not contain any of them, and vice versa. If it did, a stable
# persisted error code could be confused with a stable persisted
# non-error outcome code sharing the same string.
NOT_ERROR_VALUES = {
    "verification_passed",
    "plan_rejected",
    "budget_exceeded",
    "cancelled",
    "test_failure",
    "policy_denied",  # never existed, but must never be added as a code:
    # ordinary policy denial is not an error at all.
    "incomplete",
}


def test_error_code_values_never_collide_with_expected_outcome_values() -> None:
    error_values = {c.value for c in ErrorCode}
    assert error_values.isdisjoint(NOT_ERROR_VALUES)


def test_no_error_code_exists_for_ordinary_policy_denial() -> None:
    """An ordinary denied tool request (events.PolicyDecisionRecorded
    with allowed=False) must never gain its own ErrorCode -- only a
    SEVERE policy violation does."""
    assert not any("denied" in c.value for c in ErrorCode)
    assert not any("denial" in c.value for c in ErrorCode)


def test_no_error_code_exists_for_test_failure() -> None:
    """VerificationOutcome.TEST_FAILURE is an expected domain outcome,
    not an operational failure -- it must never gain its own ErrorCode,
    unlike TIMEOUT/ENVIRONMENT_FAILURE/COMMAND_START_FAILURE which do."""
    assert not any("test_failure" in c.value for c in ErrorCode)


def test_no_error_code_exists_for_budget_exhaustion() -> None:
    """domain.TerminalReason.BUDGET_EXCEEDED already covers this at the
    state-machine level; the error taxonomy adds no parallel code for
    it (budget exhaustion is an expected outcome, not a failure)."""
    assert not any("budget" in c.value for c in ErrorCode)


def test_no_error_code_exists_for_cancellation() -> None:
    assert not any("cancel" in c.value for c in ErrorCode)


def test_no_error_code_exists_for_plan_rejection() -> None:
    assert not any("plan_reject" in c.value for c in ErrorCode)


def test_no_error_code_exists_for_incomplete_model_response() -> None:
    """events.ModelResponseStatus.INCOMPLETE plus incomplete_reason
    already cover this; it's a model-behavior outcome, not a system
    failure, so no ErrorCode represents it."""
    assert not any("incomplete" in c.value for c in ErrorCode)


def test_severe_policy_violation_is_the_only_policy_error_code() -> None:
    """Confirms exactly one policy-domain error code exists, and it is
    the severe one -- not a generic "policy" catch-all that could be
    confused with an ordinary denial."""
    policy_codes = [c for c in ErrorCode if domain_of(c) is ErrorDomain.POLICY]
    assert policy_codes == [ErrorCode.POLICY_VIOLATION_SEVERE]


# --------------------------------------------------------------------
# IllegalTransitionError / IllegalToolCallError stay local exceptions
# --------------------------------------------------------------------


def test_domain_module_does_not_expose_error_taxonomy_names() -> None:
    """domain.IllegalTransitionError/IllegalToolCallError are local,
    synchronous programming-error exceptions raised by domain.py's own
    fail-closed checks -- distinct from INTERNAL_INVARIANT_VIOLATION,
    which is only for a controller that catches one of those exceptions
    unexpectedly and translates it for persistence. This only proves
    domain.py doesn't expose ErrorCode/OperationalError as attributes
    (e.g. via a wildcard re-export) -- it does NOT prove domain.py has
    no import dependency on errors.py; see the source-level check
    below for that."""
    import codeagent.domain as domain_module

    assert not hasattr(domain_module, "ErrorCode")
    assert not hasattr(domain_module, "OperationalError")


def test_domain_module_does_not_import_errors_module() -> None:
    """Genuine import-graph check (not just an attribute-exposure
    check): parses domain.py's own source with the stdlib ast module
    and confirms no import statement anywhere in it references
    codeagent.errors or a bare `errors` module."""
    import ast
    import inspect

    import codeagent.domain as domain_module

    source = inspect.getsource(domain_module)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        module == "errors" or module.endswith(".errors") for module in imported_modules
    )


def test_domain_exceptions_are_raised_independently_of_errors_module() -> None:
    with pytest.raises(domain.IllegalTransitionError):
        domain.transition(domain.RunState.INIT, domain.Trigger.PATCH_APPLIED)
    with pytest.raises(domain.IllegalToolCallError):
        domain.check_tool_call(domain.RunState.INIT, domain.ToolName.READ_FILE)
