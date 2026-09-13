"""Milestone 1 slice C: parity tests between VerificationResult and
events.py's own BaselineRecorded/VerificationCompleted validation.

controller.VerificationResult.__post_init__ calls
events.validate_verification_outcome_shape — the one public function
events.py exposes for this — directly, so parity is achieved by
construction, not by two independently written rule sets. These tests
exist to catch a future regression where someone "simplifies"
VerificationResult by inlining its own copy of the rules instead of
reusing events.py's public function — at which point the two could
silently drift apart. Every (outcome, exit_code, error) combination is
checked against both VerificationResult and a real BaselineRecorded
event, and the two must always agree on accept/reject.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from codeagent import domain, events
from codeagent.controller import VerificationResult
from codeagent.errors import ErrorCode, OperationalError

_SOME_ERROR = OperationalError(
    code=ErrorCode.EXECUTOR_TIMEOUT, error_id="err-1", message="timed out"
)
_OTHER_ERROR = OperationalError(
    code=ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE, error_id="err-2", message="env broke"
)

_EXIT_CODE_CANDIDATES = (None, 0, 1, 137)
_ERROR_CANDIDATES = (None, _SOME_ERROR, _OTHER_ERROR)


def _build_baseline_event(outcome, exit_code, error) -> None:
    events.BaselineRecorded(
        run_id="r",
        sequence=0,
        timestamp=datetime.now(timezone.utc),
        state=domain.RunState.BASELINE,
        iteration=0,
        outcome=outcome,
        command=("pytest",),
        exit_code=exit_code,
        duration_seconds=1.0,
        error=error,
    )


def _build_verification_result(outcome, exit_code, error) -> None:
    VerificationResult(
        outcome=outcome,
        exit_code=exit_code,
        duration_seconds=1.0,
        stdout="",
        stderr="",
        error=error,
    )


@pytest.mark.parametrize("outcome", list(events.VerificationOutcome))
@pytest.mark.parametrize("exit_code", _EXIT_CODE_CANDIDATES)
@pytest.mark.parametrize("error", _ERROR_CANDIDATES)
def test_verification_result_and_baseline_event_agree(outcome, exit_code, error) -> None:
    event_outcome: BaseException | None = None
    result_outcome: BaseException | None = None

    try:
        _build_baseline_event(outcome, exit_code, error)
    except ValueError as exc:
        event_outcome = exc

    try:
        _build_verification_result(outcome, exit_code, error)
    except ValueError as exc:
        result_outcome = exc

    event_rejected = event_outcome is not None
    result_rejected = result_outcome is not None
    assert event_rejected == result_rejected, (
        f"VerificationResult and BaselineRecorded disagree for "
        f"outcome={outcome!r}, exit_code={exit_code!r}, error={error!r}: "
        f"event_rejected={event_rejected} ({event_outcome}), "
        f"result_rejected={result_rejected} ({result_outcome})"
    )


def test_at_least_one_accepted_and_one_rejected_combination_exists() -> None:
    """Sanity check that the parametrized sweep above isn't vacuously
    true because every combination is accepted or every one is
    rejected."""
    accepted = 0
    rejected = 0
    for outcome in events.VerificationOutcome:
        for exit_code in _EXIT_CODE_CANDIDATES:
            for error in _ERROR_CANDIDATES:
                try:
                    _build_verification_result(outcome, exit_code, error)
                    accepted += 1
                except ValueError:
                    rejected += 1
    assert accepted > 0
    assert rejected > 0


def test_public_validator_is_not_shadowed_by_a_private_copy() -> None:
    """Guards the parity mechanism itself: VerificationResult must call
    events.py's real public validator, not some local reimplementation
    that happens to agree today. Monkeypatching the public function to
    always raise must make VerificationResult reject everything —
    proving VerificationResult actually calls it at construction time,
    not merely a same-named local copy."""
    import pytest

    from codeagent import events as events_module
    from codeagent.controller import VerificationResult

    original = events_module.validate_verification_outcome_shape

    def always_raise(outcome, exit_code, error):
        raise ValueError("forced by test")

    events_module.validate_verification_outcome_shape = always_raise
    try:
        with pytest.raises(ValueError, match="forced by test"):
            VerificationResult(
                outcome=events.VerificationOutcome.PASSED,
                exit_code=0,
                duration_seconds=1.0,
                stdout="",
                stderr="",
            )
    finally:
        events_module.validate_verification_outcome_shape = original
