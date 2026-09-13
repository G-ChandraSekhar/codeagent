"""Milestone 1: a deterministic run controller.

This is production orchestration code — it depends only on narrow
Protocols (`ModelClient`, `ApprovalProvider`, `Verifier`, `PatchApplier`,
`Clock`), never on any concrete test double. Deterministic fakes
implementing those Protocols live in `tests/support/fakes.py`, not
here. `codeagent.patch.GitPatchApplier` and `codeagent.workspace.GitWorktree`
are real (non-fake) implementations of part of this seam, added in
slice B.

Scope so far:
- Slice A: exercises domain.py's state machine, events.py's schemas,
  and errors.py's taxonomy together, end-to-end, through one real
  callable code path — with every collaborator faked.
- Slice B: `PatchApplier` can now be a real implementation
  (`codeagent.patch.GitPatchApplier`) that validates and applies one
  controlled patch operation inside a real, disposable `git worktree`
  (`codeagent.workspace.GitWorktree`) and commits it as the checkpoint.
  `RunController` itself is unchanged by this — it already only ever
  used `PatchResult`'s fields, never invented data.

This still isn't full Milestone 1 completion: no Docker verification,
no live model provider, no report generation (slice C).

Not implemented here: a process executor, a model provider adapter,
redaction, persistence (the `EventLog` below is in-memory only, not the
real EventSink), or a *considered* retry/abort/escalation policy for
operational errors — a controller now exists, but it has no such policy
yet; this controller's placeholder is documented at the one place it
applies (`_dispatch_apply_patch`).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from codeagent import domain, events
from codeagent.errors import ErrorCode, OperationalError


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_nonnegative_int(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


@dataclass(frozen=True)
class PlanProposal:
    problem_hypothesis: str
    proposed_file_paths: tuple[str, ...]
    verification_intent: str


@dataclass(frozen=True)
class ReadResult:
    """What actually happened reading one file — RunController reports
    these fields verbatim, same discipline as PatchResult/
    VerificationResult: it never invents content or a byte count.

    Deliberately narrow: this is Milestone 1's single-file,
    read-before-plan vertical slice, not Milestone 2's general
    repository-read result shape. No directory listing, no pagination,
    no binary content — see codeagent.reader.WorktreeFileReader's
    module docstring.
    """

    success: bool
    content: str | None
    byte_count: int
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        if self.success:
            if self.content is None:
                raise ValueError("a successful ReadResult must have content")
            if self.byte_count < 0:
                raise ValueError("a successful ReadResult must have byte_count >= 0")
            if self.error is not None:
                raise ValueError("a successful ReadResult must not carry an error")
        else:
            if self.content is not None:
                raise ValueError("a failed ReadResult must have content is None")
            if self.byte_count != 0:
                raise ValueError("a failed ReadResult must have byte_count == 0")
            if self.error is None:
                raise ValueError("a failed ReadResult must carry an error")


@dataclass(frozen=True)
class PatchResult:
    """What actually happened when a PatchApplier ran — RunController
    only ever reports these fields; it never invents a checkpoint id,
    diff size, or changed-file list itself.

    Invariants (enforced in __post_init__, not just documented): a
    successful result has a real commit hash, a positive operation
    count, at least one changed path, a positive diff size, and no
    error. A failed result has none of that success-shaped metadata —
    only an error.
    """

    success: bool
    operation_count: int
    changed_paths: tuple[str, ...]
    diff_bytes: int
    commit_hash: str | None = None
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        if self.success:
            if not self.commit_hash:
                raise ValueError("a successful PatchResult must have a nonempty commit_hash")
            if self.operation_count <= 0:
                raise ValueError("a successful PatchResult must have operation_count > 0")
            if not self.changed_paths:
                raise ValueError("a successful PatchResult must have nonempty changed_paths")
            if self.diff_bytes <= 0:
                raise ValueError("a successful PatchResult must have diff_bytes > 0")
            if self.error is not None:
                raise ValueError("a successful PatchResult must not carry an error")
        else:
            if self.operation_count != 0:
                raise ValueError("a failed PatchResult must have operation_count == 0")
            if self.changed_paths != ():
                raise ValueError("a failed PatchResult must have changed_paths == ()")
            if self.diff_bytes != 0:
                raise ValueError("a failed PatchResult must have diff_bytes == 0")
            if self.commit_hash is not None:
                raise ValueError("a failed PatchResult must have commit_hash is None")
            if self.error is None:
                raise ValueError("a failed PatchResult must carry an error")


_MAX_OUTPUT_BYTES = 64 * 1024
# Slack above the hard 64 KiB collection limit, to accommodate a
# truncation marker appended after the limit is reached — this is a
# courtesy sanity check on the *result*, not the enforcement mechanism
# itself (that lives in whatever collected the output, e.g.
# executor._BoundedCollector).
_MAX_OUTPUT_BYTES_WITH_MARKER = _MAX_OUTPUT_BYTES + 256


@dataclass(frozen=True)
class VerificationResult:
    """What actually happened running the baseline/verification command
    — RunController reports these fields verbatim, same discipline as
    PatchResult.

    Its exit-code/outcome/error shape is validated with the same public
    function events.py exposes for BaselineRecorded/VerificationCompleted
    (`events.validate_verification_outcome_shape`) rather than a
    separately maintained copy of that matrix — see
    tests/unit/test_verification_result_parity.py, which exists
    specifically to catch the day these two are ever validated by
    different rules. This is the one place production code outside
    events.py is allowed to reach into its validation logic, and it
    does so through that module's public function, never its
    underscore-prefixed internals.
    """

    outcome: events.VerificationOutcome
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        events.validate_verification_outcome_shape(self.outcome, self.exit_code, self.error)
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError(
                f"duration_seconds must be finite and >= 0, got {self.duration_seconds!r}"
            )
        if len(self.stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES_WITH_MARKER:
            raise ValueError("stdout exceeds the bounded output limit")
        if len(self.stderr.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES_WITH_MARKER:
            raise ValueError("stderr exceeds the bounded output limit")


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    """Production default: real wall-clock time (`now`) and a real
    monotonic clock (`monotonic`, for measuring actual operation
    durations). Tests inject a deterministic Clock instead (see
    tests/support/fakes.SteppingClock) so traces are reproducible
    without sleeping or patching datetime/time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class ModelClient(Protocol):
    """Two-step and deliberately narrow, scoped only to Milestone 1's
    read-before-plan vertical slice: the model first names one file to
    read (`request_read_path`), then proposes a plan once given that
    file's actual read evidence (`propose_plan`). RunController never
    calls `propose_plan` without first dispatching the read the model
    asked for and passing back its real `ReadResult` — see
    `RunController._explore_and_propose_plan`.

    This is NOT the future live-provider protocol. A real model makes
    arbitrary, budget-aware tool calls in whatever order it chooses
    (list_directory, read_file, search_text, repeated reads, then
    propose_plan) — that general multi-tool loop is separate, later
    work. This shape exists only so the deterministic fake model used
    today is genuinely evidence-driven instead of proposing a plan
    independently of the read it triggers; do not extend it to carry
    more of the future protocol's responsibility than that.
    """

    def request_read_path(self) -> str: ...
    def propose_plan(self, read_result: ReadResult) -> PlanProposal: ...


class RepositoryReader(Protocol):
    """Reads exactly one UTF-8 text file and reports what happened —
    same verbatim-report discipline as PatchApplier/Verifier: never
    derives or invents content, always reports a structured ReadResult
    rather than letting a filesystem exception escape.

    Deliberately narrow, matching ModelClient's scope above: Milestone
    1's read-before-plan slice only. No directory listing, no
    pagination, no binary content, no search — see
    codeagent.reader.WorktreeFileReader (the real implementation) and
    tests/support/fakes.FakeRepositoryReader (the deterministic fake).
    """

    def read_file(self, relative_path: str) -> ReadResult: ...


class ApprovalProvider(Protocol):
    def decide(self, visit_index: int) -> domain.ApprovalDecision: ...


class Verifier(Protocol):
    """`run_baseline` and `run` both return a `VerificationResult` —
    RunController trusts every field verbatim (outcome, exit_code,
    duration, output, error), it never derives or invents any of them.
    A real implementation (codeagent.executor.DockerVerifier) may
    return any VerificationOutcome; a fake may choose to support only
    a subset (see tests/support/fakes.FakeVerifier) — that's the
    collaborator's choice, not something this Protocol restricts.

    `command` is the single source of truth for what verification
    actually runs: RunController records this value on RunStarted,
    BaselineRecorded, and VerificationCompleted rather than a
    separately configured `RunConfig.verify_command` that could
    disagree with what the Verifier itself executes.
    """

    @property
    def command(self) -> tuple[str, ...]: ...
    def run_baseline(self) -> VerificationResult: ...
    def run(self, attempt_index: int) -> VerificationResult: ...


class PatchApplier(Protocol):
    """Applies whatever patch the implementation is configured with and
    reports what actually happened. Deliberately does not take the full
    `plan` as an argument: `codeagent.patch.GitPatchApplier` (slice B)
    is configured with its PatchOperation at construction, not derived
    from the model's plan — see patch.py's module docstring for why
    that's the right scope for this milestone.

    `approved_paths` *is* passed, and is the primary, preventive
    authorization boundary: an implementation must reject an operation
    whose target isn't in this set before making any change at all —
    see `codeagent.patch.GitPatchApplier.apply`'s docstring. It is not
    merely advisory data for a postcondition check.
    """

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str]
    ) -> PatchResult: ...


@dataclass(frozen=True)
class RunConfig:
    """General run parameters — not fixture-scripting data. What the
    fake (or, later, real) collaborators decide dynamically is not part
    of this config."""

    run_id: str
    task_statement: str
    approval_mode: domain.ApprovalMode
    # Real worktree path when one exists (slice B); a placeholder URI
    # when the run has no real repository at all (slice A, fully faked).
    repository_path: str = "fixture://synthetic"
    # The commit RunController should cite as the first patch's parent
    # checkpoint — the real worktree's starting commit, when known.
    # None when there's no real repository (slice A).
    initial_checkpoint_id: str | None = None
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
        if not self.repository_path:
            raise ValueError("repository_path must be a nonempty string")
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
        reader: RepositoryReader,
        clock: Clock | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._c = config
        self._model = model
        self._approval = approval
        self._verifier = verifier
        self._patch_applier = patch_applier
        self._reader = reader
        self._clock = clock or SystemClock()
        self.log = event_log or EventLog()
        self.state = domain.RunState.INIT
        self._total_duration = 0.0
        self._last_checkpoint_id: str | None = config.initial_checkpoint_id

    def _now(self) -> datetime:
        return self._clock.now()

    def _fake_tick(self, amount: float = 0.01) -> float:
        """Fabricated duration for a still-fake collaborator (model,
        approval, verifier) — there is no real operation to time. Real
        operations (patch application, slice B onward) measure actual
        elapsed time via `self._clock.monotonic()` instead; see
        `_dispatch_apply_patch`."""
        self._total_duration += amount
        return amount

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
                repository_path=c.repository_path,
                task_statement=c.task_statement,
                verify_command=self._verifier.command,
                approval_mode=c.approval_mode,
            )
        )
        self._transition(0, domain.Trigger.RUN_STARTED)

        baseline_result = self._verifier.run_baseline()
        self._total_duration += baseline_result.duration_seconds
        self._emit(
            events.BaselineRecorded(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=0,
                outcome=baseline_result.outcome,
                command=self._verifier.command,
                exit_code=baseline_result.exit_code,
                duration_seconds=baseline_result.duration_seconds,
                error=baseline_result.error,
            )
        )
        if baseline_result.outcome not in (
            events.VerificationOutcome.PASSED,
            events.VerificationOutcome.TEST_FAILURE,
        ):
            # An operational failure running the baseline itself (the
            # executor couldn't even establish what "broken" looks
            # like) aborts the run outright — there's nothing to repair
            # towards yet.
            result = self._transition(0, domain.Trigger.UNRECOVERABLE_ERROR)
            return self._finish(0, result.terminal_reason, error=baseline_result.error)
        self._transition(0, domain.Trigger.BASELINE_RECORDED)

        pass_index = 0
        approval_visit = 0
        verification_attempt = 0
        repair_iterations_used = 0
        plan_revisions_used = 0

        while True:
            plan, read_error = self._explore_and_propose_plan(pass_index)
            if plan is None:
                # The read the model requested before proposing a plan
                # failed (bad path, symlink escape, oversized, non-UTF-8,
                # etc.) — an operational failure, not a normal domain
                # outcome, so it aborts the run the same way a baseline
                # or verification operational failure does, without
                # consuming any repair/revision budget.
                result = self._transition(pass_index, domain.Trigger.UNRECOVERABLE_ERROR)
                return self._finish(pass_index, result.terminal_reason, error=read_error)
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

            verification_result = self._verifier.run(verification_attempt)
            verification_attempt += 1
            self._total_duration += verification_result.duration_seconds
            self._emit(
                events.VerificationCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    outcome=verification_result.outcome,
                    command=self._verifier.command,
                    exit_code=verification_result.exit_code,
                    duration_seconds=verification_result.duration_seconds,
                    fail_to_pass=(),
                    pass_to_pass_broken=(),
                    error=verification_result.error,
                )
            )

            if verification_result.outcome is events.VerificationOutcome.PASSED:
                result = self._transition(pass_index, domain.Trigger.VERIFICATION_PASSED)
                return self._finish(pass_index, result.terminal_reason)

            if verification_result.outcome is events.VerificationOutcome.TEST_FAILURE:
                # An expected domain outcome, not an error: repair if
                # budget allows.
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
                continue  # loop back to EXPLORE for another attempt

            # An operational outcome (TIMEOUT / ENVIRONMENT_FAILURE /
            # COMMAND_START_FAILURE): abort outright, without consuming
            # repair budget — this isn't a normal repair-triggering
            # failure, it's the executor itself failing to produce a
            # trustworthy result at all.
            result = self._transition(pass_index, domain.Trigger.UNRECOVERABLE_ERROR)
            return self._finish(pass_index, result.terminal_reason, error=verification_result.error)

    def _explore_and_propose_plan(
        self, iteration: int
    ) -> tuple[PlanProposal | None, OperationalError | None]:
        """Read-before-plan, in that order: ask the model which file it
        wants (`request_read_path`), actually dispatch that READ_FILE
        tool call against the real worktree, and only then hand the
        model its real ReadResult so `propose_plan` is genuinely
        evidence-driven rather than independent of the read it
        triggered — see ModelClient's docstring. Returns
        (plan, None) on success, or (None, error) if the read failed
        (an operational failure, handled by the caller the same way a
        patch or verification operational failure is)."""
        c = self._c

        read_path = self._model.request_read_path()
        read_result, read_error, read_tool_call_id = self._dispatch_read_file(
            iteration, read_path
        )
        if read_result is None:
            return None, read_error

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
        plan = self._model.propose_plan(read_result)
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
                latency_seconds=self._fake_tick(),
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
                duration_seconds=self._fake_tick(),
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
                # The read this plan was actually based on — a real
                # cross-reference, not an invented citation.
                evidence_refs=(read_tool_call_id,),
                proposed_file_paths=plan.proposed_file_paths,
                verification_intent=plan.verification_intent,
                risk_notes="",
            )
        )
        return plan, None

    def _dispatch_read_file(
        self, iteration: int, relative_path: str
    ) -> tuple[ReadResult | None, OperationalError | None, str]:
        """Dispatch READ_FILE as a real tool call in EXPLORE and report
        exactly what the RepositoryReader says happened — never invented
        data, same discipline as `_dispatch_apply_patch`. Returns
        (result, error, tool_call_id): result is None (with error set)
        on failure.

        Placeholder disposition policy, same honest-but-unconsidered
        shape as `_dispatch_apply_patch`'s: a failed read aborts the run
        as UNRECOVERABLE_ERROR rather than being retried or reported to
        the model as a recoverable tool error. A future controller with
        budget/attempt-count context may replace this wholesale.
        """
        c = self._c
        tool_call_id = f"{c.run_id}-tc-read-{iteration}"
        self._dispatch_tool(
            iteration,
            domain.ToolName.READ_FILE,
            tool_call_id,
            {"relative_path": relative_path},
        )

        start = self._clock.monotonic()
        result = self._reader.read_file(relative_path)
        duration = self._clock.monotonic() - start
        self._total_duration += duration

        if not result.success:
            self._emit(
                events.ToolCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=iteration,
                    tool=domain.ToolName.READ_FILE,
                    tool_call_id=tool_call_id,
                    success=False,
                    duration_seconds=duration,
                    result_summary="",
                    error=result.error,
                )
            )
            return None, result.error, tool_call_id

        # Bounded summary and the tool_call_id above as the evidence
        # reference — never the raw file content: that would persist
        # (unbounded, potentially sensitive) repository content into
        # the event log, which no event here is meant to carry.
        self._emit(
            events.ToolCompleted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=domain.ToolName.READ_FILE,
                tool_call_id=tool_call_id,
                success=True,
                duration_seconds=duration,
                result_summary=f"read {result.byte_count} byte(s) from {relative_path!r}",
            )
        )
        return result, None, tool_call_id

    def _dispatch_apply_patch(
        self, iteration: int, plan: PlanProposal
    ) -> tuple[bool, OperationalError | None]:
        """Dispatch the patch as a tool call and report exactly what the
        PatchApplier says happened — never invented data. Returns
        (ok, error).

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

        # Real elapsed time — applying a patch is real I/O from slice B
        # onward (git add/diff/commit for GitPatchApplier), unlike the
        # still-fake model/approval/verifier durations above.
        start = self._clock.monotonic()
        result = self._patch_applier.apply(
            c.run_id, iteration, frozenset(plan.proposed_file_paths)
        )
        duration = self._clock.monotonic() - start
        self._total_duration += duration

        if not result.success:
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
                    duration_seconds=duration,
                    result_summary="",
                    error=result.error,
                )
            )
            return False, result.error

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
                duration_seconds=duration,
                result_summary=f"{result.operation_count} operation(s) applied",
            )
        )
        # The checkpoint's identity is the real commit hash — no
        # separately invented id.
        checkpoint_id = result.commit_hash
        self._emit(
            events.CheckpointCreated(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=self._last_checkpoint_id,
                commit_hash=result.commit_hash,
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
                operation_count=result.operation_count,
                files_changed=result.changed_paths,
                checkpoint_id=checkpoint_id,
                diff_bytes=result.diff_bytes,
            )
        )

        # Defense-in-depth postcondition, NOT the primary enforcement:
        # the primary, preventive check is inside PatchApplier.apply
        # itself (see PatchApplier's docstring and
        # codeagent.patch.GitPatchApplier.apply) — it must reject an
        # unapproved target before any write or commit happens at all.
        # This check only catches a PatchApplier implementation that
        # got that wrong (or a future implementation that doesn't
        # enforce it as strictly) — by the time this runs, the mutation
        # this checks for would already have happened. What it *does*
        # still guarantee even then: an out-of-scope change is never
        # treated as a legitimate step by the rest of the run — the run
        # is aborted here, before any further budget (a verification
        # attempt) is spent on it.
        approved_paths = set(plan.proposed_file_paths)
        actual_paths = set(result.changed_paths)
        if not actual_paths.issubset(approved_paths):
            scope_error = OperationalError(
                code=ErrorCode.INTERNAL_INVARIANT_VIOLATION,
                error_id=f"{c.run_id}-scope-{iteration}",
                message="patch changed files outside the approved plan's proposed_file_paths",
            )
            return False, scope_error

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
