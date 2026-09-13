"""Deterministic test doubles implementing controller.py's collaborator
Protocols (ModelClient, ApprovalProvider, Verifier, PatchApplier, Clock).

Not production code. Lives under tests/ specifically so it can never be
imported from src/codeagent by accident — see the correction-pass
finding that flagged the previous version of controller.py for
depending on concrete fake types directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codeagent import domain, events
from codeagent.controller import PatchResult, PlanProposal, SUPPORTED_VERIFICATION_OUTCOMES
from codeagent.errors import ErrorCode, OperationalError


class FakeModel:
    """Always proposes the same canned plan. Never asked to do anything
    else in this narrow slice."""

    def __init__(self, plan: PlanProposal) -> None:
        self._plan = plan

    def propose_plan(self) -> PlanProposal:
        return self._plan


class FakeApprovalProvider:
    def __init__(self, decisions: tuple[domain.ApprovalDecision, ...]) -> None:
        if not decisions:
            raise ValueError("decisions must be a nonempty tuple")
        self._decisions = decisions

    def decide(self, visit_index: int) -> domain.ApprovalDecision:
        i = min(visit_index, len(self._decisions) - 1)
        return self._decisions[i]


class FakeVerifier:
    def __init__(self, outcomes: tuple[events.VerificationOutcome, ...]) -> None:
        if not outcomes:
            raise ValueError("outcomes must be a nonempty tuple")
        unsupported = sorted(
            {o.value for o in outcomes} - {o.value for o in SUPPORTED_VERIFICATION_OUTCOMES}
        )
        if unsupported:
            raise ValueError(
                "FakeVerifier only supports "
                f"{sorted(o.value for o in SUPPORTED_VERIFICATION_OUTCOMES)} — this "
                "synthetic harness has no real executor to give TIMEOUT/"
                f"ENVIRONMENT_FAILURE/COMMAND_START_FAILURE real meaning; got {unsupported}"
            )
        self._outcomes = outcomes

    def run(self, attempt_index: int) -> events.VerificationOutcome:
        i = min(attempt_index, len(self._outcomes) - 1)
        return self._outcomes[i]


class FakePatchApplier:
    """No real filesystem/git involved — see codeagent.patch.GitPatchApplier
    (slice B) for the real implementation this fakes.

    `changed_paths` defaults to the fixture's usual single file, but is
    overridable so a test can construct a PatchApplier that (mis)reports
    a file outside whatever the plan actually approved — see
    test_controller.py's plan-scope consistency test.
    """

    def __init__(
        self,
        should_fail: bool = False,
        changed_paths: tuple[str, ...] = ("jobs/worker.py",),
    ) -> None:
        self._should_fail = should_fail
        self._changed_paths = changed_paths
        self._n = 0

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str]
    ) -> PatchResult:
        # Deliberately does NOT enforce approved_paths itself — this
        # fake exists partly to let test_controller.py's plan-scope
        # test simulate a PatchApplier that fails to do the real,
        # preventive check GitPatchApplier does, exercising the
        # controller's defense-in-depth postcondition instead.
        self._n += 1
        if self._should_fail:
            return PatchResult(
                success=False,
                operation_count=0,
                changed_paths=(),
                diff_bytes=0,
                error=OperationalError(
                    code=ErrorCode.PATCH_VALIDATION_FAILED,
                    error_id=f"{run_id}-fake-patch-err-{self._n}",
                    message="fixture-forced patch validation failure",
                ),
            )
        return PatchResult(
            success=True,
            operation_count=1,
            changed_paths=self._changed_paths,
            diff_bytes=42,
            commit_hash=f"{run_id}-fake-commit-{iteration}",
        )


class SteppingClock:
    """Deterministic Clock: each call to now() advances a fixed step
    from a fixed epoch, and each call to monotonic() advances a fixed
    step from zero — two independent counters, fully reproducible. No
    real time, no sleeping, no monkeypatching datetime/time."""

    def __init__(
        self,
        epoch: datetime | None = None,
        step: timedelta = timedelta(milliseconds=1),
        monotonic_step: float = 0.001,
    ) -> None:
        self._next = epoch or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._step = step
        self._next_monotonic = 0.0
        self._monotonic_step = monotonic_step

    def now(self) -> datetime:
        current = self._next
        self._next = self._next + self._step
        return current

    def monotonic(self) -> float:
        current = self._next_monotonic
        self._next_monotonic += self._monotonic_step
        return current
