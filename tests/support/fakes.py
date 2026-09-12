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
from codeagent.controller import PlanProposal, SUPPORTED_VERIFICATION_OUTCOMES


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
    def __init__(self, should_fail: bool = False) -> None:
        self._should_fail = should_fail

    def apply(self, iteration: int, plan: PlanProposal) -> bool:
        return not self._should_fail


class SteppingClock:
    """Deterministic Clock: each call to now() advances by a fixed step
    from a fixed epoch. Fully reproducible — no real time, no sleeping,
    no monkeypatching datetime."""

    def __init__(
        self,
        epoch: datetime | None = None,
        step: timedelta = timedelta(milliseconds=1),
    ) -> None:
        self._next = epoch or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._step = step

    def now(self) -> datetime:
        current = self._next
        self._next = self._next + self._step
        return current
