"""Milestone 1, slice A: a deterministic run controller.

This is production orchestration code — it depends only on narrow
Protocols (`ModelClient`, `ApprovalProvider`, `Verifier`, `Clock`), never
on any concrete test double. Deterministic fakes implementing those
Protocols live in `tests/support/fakes.py`, not here.

Scope: exercises domain.py's state machine, events.py's schemas, and
errors.py's taxonomy together, end-to-end, through one real callable
code path. This intentionally narrows the guide's Milestone 1 paragraph
("a real fixture repository ... temporary worktree ... Docker may
initially run only a fixed verification command") per explicit
instruction: no worktree and no Docker exist yet, and none is built
here. Treat this module as "Milestone 1, slice A" — a contract-
integration harness — not Milestone 1 completion.

Not implemented here (later milestones): a real workspace/worktree, a
process executor, a model provider adapter, redaction, persistence (the
`EventLog` below is in-memory only, not the real EventSink), or a
considered retry/disposition policy for operational errors — this
controller's placeholder policy is documented at the one place it
applies (`_dispatch_apply_patch`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from codeagent import domain, events
from codeagent.errors import ErrorCode, OperationalError

SUPPORTED_VERIFICATION_OUTCOMES = frozenset(
    {events.VerificationOutcome.PASSED, events.VerificationOutcome.TEST_FAILURE}
)

_EXIT_CODE_BY_OUTCOME = {
    events.VerificationOutcome.PASSED: 0,
    events.VerificationOutcome.TEST_FAILURE: 1,
}


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _exit_code_for(outcome: events.VerificationOutcome) -> int:
    try:
        return _EXIT_CODE_BY_OUTCOME[outcome]
    except KeyError:
        raise NotImplementedError(
            f"RunController does not yet support VerificationOutcome.{outcome.name} "
            "— no real executor exists yet to give timeout/environment/"
            "command-start-failure semantics meaning"
        ) from None


def _require_nonnegative_int(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


@dataclass(frozen=True)
class PlanProposal:
    problem_hypothesis: str
    proposed_file_paths: tuple[str, ...]
    verification_intent: str


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Production default: real wall-clock time. Tests inject a
    deterministic Clock instead (see tests/support/fakes.SteppingClock)
    so traces are reproducible without sleeping or patching datetime."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ModelClient(Protocol):
    def propose_plan(self) -> PlanProposal: ...


class ApprovalProvider(Protocol):
    def decide(self, visit_index: int) -> domain.ApprovalDecision: ...


class Verifier(Protocol):
    def run(self, attempt_index: int) -> events.VerificationOutcome: ...


class PatchApplier(Protocol):
    """Whether applying the plan's patch succeeded. No real workspace
    exists yet (Milestone 2); a real implementation will need to return
    enough detail to distinguish PATCH_VALIDATION_FAILED from
    PATCH_APPLICATION_FAILED, but this slice's controller treats any
    failure uniformly (see _dispatch_apply_patch)."""

    def apply(self, iteration: int, plan: PlanProposal) -> bool: ...


@dataclass(frozen=True)
class RunConfig:
    """General run parameters — not fixture-scripting data. What the
    fake (or, later, real) collaborators decide dynamically is not part
    of this config."""

    run_id: str
    task_statement: str
    verify_command: tuple[str, ...]
    approval_mode: domain.ApprovalMode
    baseline_outcome: events.VerificationOutcome = events.VerificationOutcome.TEST_FAILURE
    # Repairs allowed *after* the initial verification attempt: max=0
    # permits the initial attempt but no repair; max=2 permits the
    # initial attempt plus two repairs (3 verification attempts total).
    max_repair_iterations: int = 3
    # Plan revisions allowed before a further REVISION_REQUESTED
    # decision ends the run via BudgetExceeded(kind=PLAN_REVISIONS)
    # instead of looping again. Same "count already used vs. max"
    # semantics as max_repair_iterations.
    max_plan_revisions: int = 2

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must be a nonempty string")
        if not self.task_statement:
            raise ValueError("task_statement must be a nonempty string")
        if not self.verify_command:
            raise ValueError("verify_command must be a nonempty tuple")
        if self.baseline_outcome not in SUPPORTED_VERIFICATION_OUTCOMES:
            raise ValueError(
                "baseline_outcome must be one of "
                f"{sorted(o.value for o in SUPPORTED_VERIFICATION_OUTCOMES)} — this "
                "controller does not yet model timeout/environment/command-start "
                f"failures for the baseline check, got {self.baseline_outcome!r}"
            )
        _require_nonnegative_int("max_repair_iterations", self.max_repair_iterations)
        _require_nonnegative_int("max_plan_revisions", self.max_plan_revisions)


class EventLog:
    """In-memory stand-in for the real EventSink (unbuilt). Its entire
    job is assigning and enforcing monotonic sequence numbers — real
    persistence is separate, later work."""

    def __init__(self) -> None:
        self.events: list[events.Event] = []
        self._next_sequence = 0

    def next_sequence(self) -> int:
        return self._next_sequence

    def append(self, event: events.Event) -> None:
        if event.sequence != self._next_sequence:
            raise AssertionError(
                f"event sequence out of order: expected {self._next_sequence}, "
                f"got {event.sequence} ({type(event).__name__})"
            )
        self.events.append(event)
        self._next_sequence += 1


class RunController:
    """Drives one run through domain.py's state machine, emitting
    events.py-typed events for every step.

    Four counters are kept deliberately distinct — conflating them was
    the source of a real bug in the first version of this controller:
    - `pass_index` (== each event's `iteration` field): which EXPLORE
      pass an event belongs to. An index, not a count — incremented on
      every return to EXPLORE, whether caused by a plan revision or a
      failed verification.
    - `_approval_visit`: which APPROVAL visit this is (for
      ApprovalProvider.decide).
    - `_verification_attempt`: which verification attempt this is (for
      Verifier.run) — only advances when EXECUTE/VERIFY is actually
      reached, so a plan revision (which never reaches VERIFY) cannot
      skip a verifier outcome the way it did before.
    - `_repair_iterations_used` / `_plan_revisions_used`: real counts
      compared against RunConfig's budgets — a plan revision no longer
      consumes repair budget, and vice versa.

    `RunFinished.iterations_used` is a count (`pass_index + 1`), not the
    last zero-based `pass_index` value — see the docstring on `_finish`.
    """

    def __init__(
        self,
        config: RunConfig,
        model: ModelClient,
        approval: ApprovalProvider,
        verifier: Verifier,
        patch_applier: PatchApplier,
        clock: Clock | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._c = config
        self._model = model
        self._approval = approval
        self._verifier = verifier
        self._patch_applier = patch_applier
        self._clock = clock or SystemClock()
        self.log = event_log or EventLog()
        self.state = domain.RunState.INIT
        self._total_duration = 0.0
        self._last_checkpoint_id: str | None = None
        self._error_id_counter = 0

    def _now(self) -> datetime:
        return self._clock.now()

    def _tick(self, amount: float = 0.01) -> float:
        self._total_duration += amount
        return amount

    def _next_error_id(self) -> str:
        self._error_id_counter += 1
        return f"{self._c.run_id}-err-{self._error_id_counter}"

    def _emit(self, event: events.Event) -> None:
        self.log.append(event)

    def _transition(self, iteration: int, trigger: domain.Trigger) -> domain.TransitionResult:
        from_state = self.state
        result = domain.transition(from_state, trigger)
        self._emit(
            events.StateTransitioned(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=result.next_state,
                iteration=iteration,
                trigger=trigger,
                from_state=from_state,
                to_state=result.next_state,
                terminal_reason=result.terminal_reason,
            )
        )
        self.state = result.next_state
        return result

    def _dispatch_tool(
        self, iteration: int, tool: domain.ToolName, tool_call_id: str, arguments: dict[str, object]
    ) -> None:
        """Emit ToolRequested then PolicyDecisionRecorded for a legal
        tool call. Raises domain.IllegalToolCallError (fail closed) if
        the tool isn't legal in the current state — checked before
        anything is emitted."""
        domain.check_tool_call(self.state, tool)
        self._emit(
            events.ToolRequested(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=tool,
                tool_call_id=tool_call_id,
                redacted_arguments_json=_canonical_json(arguments),
            )
        )
        self._emit(
            events.PolicyDecisionRecorded(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=tool,
                tool_call_id=tool_call_id,
                allowed=True,
                reason=f"{tool.value} is legal in state {self.state.value}",
            )
        )

    def run(self) -> events.RunFinished:
        c = self._c

        self._emit(
            events.RunStarted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=domain.RunState.INIT,
                iteration=0,
                repository_path="fixture://synthetic",
                task_statement=c.task_statement,
                verify_command=c.verify_command,
                approval_mode=c.approval_mode,
            )
        )
        self._transition(0, domain.Trigger.RUN_STARTED)

        self._emit(
            events.BaselineRecorded(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=0,
                outcome=c.baseline_outcome,
                command=c.verify_command,
                exit_code=_exit_code_for(c.baseline_outcome),
                duration_seconds=self._tick(),
            )
        )
        self._transition(0, domain.Trigger.BASELINE_RECORDED)

        pass_index = 0
        approval_visit = 0
        verification_attempt = 0
        repair_iterations_used = 0
        plan_revisions_used = 0

        while True:
            plan = self._explore_and_propose_plan(pass_index)
            self._transition(pass_index, domain.Trigger.PLAN_PROPOSED)
            self._transition(pass_index, domain.Trigger.PLAN_RECORDED)

            decision = self._approval.decide(approval_visit)
            self._emit(
                events.ApprovalRecorded(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    decision=decision,
                    approval_mode=c.approval_mode,
                )
            )
            approval_visit += 1
            trigger = domain.APPROVAL_DECISION_TO_TRIGGER[decision]
            result = self._transition(pass_index, trigger)

            if decision is domain.ApprovalDecision.REJECTED:
                return self._finish(pass_index, result.terminal_reason)

            if decision is domain.ApprovalDecision.REVISION_REQUESTED:
                if plan_revisions_used >= c.max_plan_revisions:
                    self._emit(
                        events.BudgetExceeded(
                            run_id=c.run_id,
                            sequence=self.log.next_sequence(),
                            timestamp=self._now(),
                            state=self.state,
                            iteration=pass_index,
                            kind=domain.BudgetKind.PLAN_REVISIONS,
                            limit_value=c.max_plan_revisions,
                            observed_value=plan_revisions_used,
                        )
                    )
                    result = self._transition(pass_index, domain.Trigger.BUDGET_EXCEEDED)
                    return self._finish(pass_index, result.terminal_reason)
                plan_revisions_used += 1
                pass_index += 1
                continue

            # APPROVED -> EXECUTE
            patch_ok, patch_error = self._dispatch_apply_patch(pass_index, plan)
            if not patch_ok:
                result = self._transition(pass_index, domain.Trigger.UNRECOVERABLE_ERROR)
                return self._finish(pass_index, result.terminal_reason, error=patch_error)

            result = self._transition(pass_index, domain.Trigger.PATCH_APPLIED)

            outcome = self._verifier.run(verification_attempt)
            verification_attempt += 1
            self._emit(
                events.VerificationCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    outcome=outcome,
                    command=c.verify_command,
                    exit_code=_exit_code_for(outcome),
                    duration_seconds=self._tick(),
                    fail_to_pass=(),
                    pass_to_pass_broken=(),
                )
            )

            if outcome is events.VerificationOutcome.PASSED:
                result = self._transition(pass_index, domain.Trigger.VERIFICATION_PASSED)
                return self._finish(pass_index, result.terminal_reason)

            # TEST_FAILURE: an expected domain outcome, not an error.
            result = self._transition(pass_index, domain.Trigger.VERIFICATION_FAILED)
            if repair_iterations_used >= c.max_repair_iterations:
                self._emit(
                    events.BudgetExceeded(
                        run_id=c.run_id,
                        sequence=self.log.next_sequence(),
                        timestamp=self._now(),
                        state=self.state,
                        iteration=pass_index,
                        kind=domain.BudgetKind.REPAIR_ITERATIONS,
                        limit_value=c.max_repair_iterations,
                        observed_value=repair_iterations_used,
                    )
                )
                result = self._transition(pass_index, domain.Trigger.BUDGET_EXCEEDED)
                return self._finish(pass_index, result.terminal_reason)
            repair_iterations_used += 1
            pass_index += 1
            # loop back to EXPLORE for another attempt

    def _explore_and_propose_plan(self, iteration: int) -> PlanProposal:
        c = self._c
        request_id = f"{c.run_id}-req-{iteration}"
        self._emit(
            events.ModelRequestStarted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                request_id=request_id,
                model_name="fake-model-v1",
            )
        )
        plan = self._model.propose_plan()
        self._emit(
            events.ModelResponseReceived(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                request_id=request_id,
                model_name="fake-model-v1",
                response_id=f"{c.run_id}-resp-{iteration}",
                status=events.ModelResponseStatus.COMPLETED,
                latency_seconds=self._tick(),
                tool_call_count=1,
            )
        )

        tool_call_id = f"{c.run_id}-tc-plan-{iteration}"
        self._dispatch_tool(
            iteration,
            domain.ToolName.PROPOSE_PLAN,
            tool_call_id,
            {"file_paths": list(plan.proposed_file_paths)},
        )
        self._emit(
            events.ToolCompleted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=domain.ToolName.PROPOSE_PLAN,
                tool_call_id=tool_call_id,
                success=True,
                duration_seconds=self._tick(),
                result_summary="plan proposed",
            )
        )
        self._emit(
            events.PlanProposed(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                problem_hypothesis=plan.problem_hypothesis,
                evidence_refs=(),
                proposed_file_paths=plan.proposed_file_paths,
                verification_intent=plan.verification_intent,
                risk_notes="",
            )
        )
        return plan

    def _dispatch_apply_patch(
        self, iteration: int, plan: PlanProposal
    ) -> tuple[bool, OperationalError | None]:
        """Dispatch the plan's patch as a tool call. Returns (ok, error).

        No retry/disposition policy exists yet (deferred by design — see
        errors.py's module docstring: that decision belongs to a future
        controller with budget/attempt-count context this one doesn't
        model). This controller's placeholder policy is the simplest
        honest one: a failed patch aborts the run as
        UNRECOVERABLE_ERROR. That is a placeholder, not a considered
        disposition policy, and is expected to be replaced wholesale
        once budgets.py exists.
        """
        c = self._c
        tool_call_id = f"{c.run_id}-tc-patch-{iteration}"
        self._dispatch_tool(
            iteration,
            domain.ToolName.APPLY_PATCH,
            tool_call_id,
            {"file_paths": list(plan.proposed_file_paths)},
        )

        applied = self._patch_applier.apply(iteration, plan)
        if not applied:
            error = OperationalError(
                code=ErrorCode.PATCH_VALIDATION_FAILED,
                error_id=self._next_error_id(),
                message="patch validation failed",
            )
            self._emit(
                events.ToolCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=iteration,
                    tool=domain.ToolName.APPLY_PATCH,
                    tool_call_id=tool_call_id,
                    success=False,
                    duration_seconds=self._tick(),
                    result_summary="",
                    error=error,
                )
            )
            return False, error

        self._emit(
            events.ToolCompleted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=domain.ToolName.APPLY_PATCH,
                tool_call_id=tool_call_id,
                success=True,
                duration_seconds=self._tick(),
                result_summary="patch applied",
            )
        )
        checkpoint_id = f"{c.run_id}-ckpt-{iteration}"
        self._emit(
            events.CheckpointCreated(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=self._last_checkpoint_id,
                commit_hash=f"fixture-commit-{iteration}",
            )
        )
        self._last_checkpoint_id = checkpoint_id
        self._emit(
            events.PatchApplied(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                operation_count=max(1, len(plan.proposed_file_paths)),
                files_changed=plan.proposed_file_paths,
                checkpoint_id=checkpoint_id,
                diff_bytes=64,
            )
        )
        return True, None

    def _finish(
        self,
        pass_index: int,
        terminal_reason: domain.TerminalReason,
        error: OperationalError | None = None,
    ) -> events.RunFinished:
        """`iterations_used` is a count of exploration passes actually
        performed (`pass_index + 1`) — not `pass_index` itself, which is
        a zero-based index. A run that reaches DONE on its very first
        EXPLORE pass (`pass_index == 0`) reports `iterations_used == 1`.
        """
        finished = events.RunFinished(
            run_id=self._c.run_id,
            sequence=self.log.next_sequence(),
            timestamp=self._now(),
            state=self.state,
            iteration=pass_index,
            terminal_reason=terminal_reason,
            total_cost_microusd=0,
            total_tokens=0,
            total_duration_seconds=self._total_duration,
            iterations_used=pass_index + 1,
            error=error,
        )
        self._emit(finished)
        return finished
