"""Core domain types for the CodeAgent run state machine.

Scope: Milestone 0, step 1 (domain types + state-transition table only).
Event schemas, error taxonomy, threat model, and ADRs are separate,
later steps. Budget accounting and counters belong to the controller,
not here — this module only defines what transitions are *legal*.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique


# Enum values below are a stable, persisted serialization contract: they
# will be written into JSONL event logs and reports. Do not rename or
# renumber an existing member's value without a migration plan for
# already-persisted data — add a new member instead.


@unique
class RunState(str, Enum):
    INIT = "INIT"
    BASELINE = "BASELINE"
    EXPLORE = "EXPLORE"
    PLAN = "PLAN"
    APPROVAL = "APPROVAL"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    DONE = "DONE"


@unique
class Trigger(str, Enum):
    RUN_STARTED = "run_started"
    BASELINE_RECORDED = "baseline_recorded"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_RECORDED = "plan_recorded"
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"
    # Distinct from PLAN_REJECTED: the operator asks for changes rather
    # than ending the run. This is a second, currently uncapped,
    # APPROVAL -> EXPLORE loop, parallel to VERIFY -> EXPLORE's repair
    # iteration cap. Domain.py stays mechanical and enforces neither cap;
    # both need a controller-level budget check (e.g. max plan revisions,
    # same family as max repair iterations) added later.
    PLAN_REVISION_REQUESTED = "plan_revision_requested"
    PATCH_APPLIED = "patch_applied"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    # A single generic budget-abort trigger. The state machine only needs
    # to know a budget ended the run; BudgetKind (below) and the
    # BudgetExceeded event (events.py) explain which budget it was.
    BUDGET_EXCEEDED = "budget_exceeded"
    # Severe/unrecoverable policy failure only. An ordinary blocked tool
    # request (e.g. a path-escape attempt rejected by the process or
    # patch policy) is a nonterminal, loggable event implemented later —
    # it does not fire this trigger or end the run.
    POLICY_VIOLATION = "policy_violation"
    CANCELLED = "cancelled"
    UNRECOVERABLE_ERROR = "unrecoverable_error"


@unique
class TerminalReason(str, Enum):
    VERIFICATION_PASSED = "verification_passed"
    PLAN_REJECTED = "plan_rejected"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_VIOLATION = "policy_violation"
    CANCELLED = "cancelled"
    UNRECOVERABLE_ERROR = "unrecoverable_error"


@unique
class BudgetKind(str, Enum):
    """Which budget a BUDGET_EXCEEDED trigger/terminal reason refers to.

    Not consulted by the state machine itself — only by whoever fires
    the trigger (the controller) and by the BudgetExceeded event that
    explains, after the fact, which budget ended the run.
    """

    REPAIR_ITERATIONS = "repair_iterations"
    PLAN_REVISIONS = "plan_revisions"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    COST = "cost"
    WALL_CLOCK = "wall_clock"


@unique
class ToolName(str, Enum):
    LIST_DIRECTORY = "list_directory"
    READ_FILE = "read_file"
    SEARCH_TEXT = "search_text"
    PROPOSE_PLAN = "propose_plan"
    APPLY_PATCH = "apply_patch"
    # No model-callable process-execution tool in v1 — see
    # docs/adr/0001-no-model-callable-process-execution.md. Baseline and
    # verification command execution are controller-owned
    # (ProcessExecutor, implemented later), never a tool the model calls.


@unique
class ApprovalDecision(str, Enum):
    """What an approval provider decided about a proposed plan.

    A narrower, dedicated type rather than reusing Trigger directly as
    stored decision data: a caller can only construct one of these three
    values in the first place, instead of accepting any of Trigger's 14
    members and rejecting most of them at construction time.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


@unique
class ApprovalMode(str, Enum):
    """How plan approval is obtained, per the guide's §9.6 / the
    handoff's resolved defaults. Already part of the persisted contract
    (recorded on every run), so defined now rather than deferred to
    approval.py."""

    INTERACTIVE = "interactive"
    PLAN_FILE = "plan-file"
    NONE = "none"


# The FSM trigger each approval decision maps to. Callers (the future
# controller/approval provider) look up the trigger explicitly through
# this table rather than an ApprovalDecision doubling as a Trigger.
APPROVAL_DECISION_TO_TRIGGER: dict[ApprovalDecision, Trigger] = {
    ApprovalDecision.APPROVED: Trigger.PLAN_APPROVED,
    ApprovalDecision.REJECTED: Trigger.PLAN_REJECTED,
    ApprovalDecision.REVISION_REQUESTED: Trigger.PLAN_REVISION_REQUESTED,
}


class IllegalTransitionError(Exception):
    def __init__(self, state: RunState, trigger: Trigger) -> None:
        super().__init__(f"trigger {trigger.value!r} is not legal in state {state.value!r}")
        self.state = state
        self.trigger = trigger


class IllegalToolCallError(Exception):
    def __init__(self, state: RunState, tool: ToolName) -> None:
        super().__init__(f"tool {tool.value!r} is not legal in state {state.value!r}")
        self.state = state
        self.tool = tool


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a legal transition: the next state, and — if and only
    if that state is DONE — the reason the run ended."""

    next_state: RunState
    terminal_reason: TerminalReason | None = None

    def __post_init__(self) -> None:
        is_done = self.next_state is RunState.DONE
        has_reason = self.terminal_reason is not None
        if is_done != has_reason:
            raise ValueError(
                "terminal_reason must be set if and only if next_state is DONE "
                f"(next_state={self.next_state!r}, terminal_reason={self.terminal_reason!r})"
            )


# Triggers that abort the run from any non-terminal state, regardless of
# which state the run is currently in. Everything else is only legal
# from the specific source state(s) listed in _TRANSITIONS.
ABORT_TRIGGERS: frozenset[Trigger] = frozenset(
    {
        Trigger.BUDGET_EXCEEDED,
        Trigger.POLICY_VIOLATION,
        Trigger.CANCELLED,
        Trigger.UNRECOVERABLE_ERROR,
    }
)

# The state-transition table. Maps (current state, trigger) to the next
# state. Absence of an entry means the trigger is illegal in that state.
# Abort triggers are handled separately below since they apply uniformly
# to every non-terminal state rather than needing one row each.
_TRANSITIONS: dict[tuple[RunState, Trigger], RunState] = {
    (RunState.INIT, Trigger.RUN_STARTED): RunState.BASELINE,
    (RunState.BASELINE, Trigger.BASELINE_RECORDED): RunState.EXPLORE,
    (RunState.EXPLORE, Trigger.PLAN_PROPOSED): RunState.PLAN,
    (RunState.PLAN, Trigger.PLAN_RECORDED): RunState.APPROVAL,
    (RunState.APPROVAL, Trigger.PLAN_APPROVED): RunState.EXECUTE,
    (RunState.APPROVAL, Trigger.PLAN_REJECTED): RunState.DONE,
    (RunState.APPROVAL, Trigger.PLAN_REVISION_REQUESTED): RunState.EXPLORE,
    (RunState.EXECUTE, Trigger.PATCH_APPLIED): RunState.VERIFY,
    (RunState.VERIFY, Trigger.VERIFICATION_PASSED): RunState.DONE,
    (RunState.VERIFY, Trigger.VERIFICATION_FAILED): RunState.EXPLORE,
}

# Terminal reason recorded for each trigger that ends a run. Every
# terminal-reaching trigger (whether from the table above or an abort
# trigger) has exactly one reason; every reason maps back to exactly one
# trigger. transition() consults this directly so callers never need a
# separate terminal_reason_for() lookup.
_TERMINAL_REASONS: dict[Trigger, TerminalReason] = {
    Trigger.VERIFICATION_PASSED: TerminalReason.VERIFICATION_PASSED,
    Trigger.PLAN_REJECTED: TerminalReason.PLAN_REJECTED,
    Trigger.BUDGET_EXCEEDED: TerminalReason.BUDGET_EXCEEDED,
    Trigger.POLICY_VIOLATION: TerminalReason.POLICY_VIOLATION,
    Trigger.CANCELLED: TerminalReason.CANCELLED,
    Trigger.UNRECOVERABLE_ERROR: TerminalReason.UNRECOVERABLE_ERROR,
}

# Tools a model may call while the run is in a given state. States not
# listed here (or listed with an empty set) accept no model tool calls
# because that state's work is performed by the controller directly:
# BASELINE (controller runs the baseline verification command), PLAN
# (controller records the plan and invokes the approval provider),
# APPROVAL (awaiting the approval provider's decision), and VERIFY
# (controller-initiated verification — never left to model discretion,
# per the guide's state-machine invariants).
_LEGAL_TOOLS: dict[RunState, frozenset[ToolName]] = {
    RunState.EXPLORE: frozenset(
        {
            ToolName.LIST_DIRECTORY,
            ToolName.READ_FILE,
            ToolName.SEARCH_TEXT,
            ToolName.PROPOSE_PLAN,
        }
    ),
    RunState.EXECUTE: frozenset({ToolName.APPLY_PATCH}),
}


def transition(state: RunState, trigger: Trigger) -> TransitionResult:
    """Return the TransitionResult for (state, trigger), or fail closed.

    Raises IllegalTransitionError if the trigger is not legal in state.
    """
    if trigger in ABORT_TRIGGERS:
        if state is RunState.DONE:
            raise IllegalTransitionError(state, trigger)
        return TransitionResult(RunState.DONE, _TERMINAL_REASONS[trigger])

    next_state = _TRANSITIONS.get((state, trigger))
    if next_state is None:
        raise IllegalTransitionError(state, trigger)
    return TransitionResult(next_state, _TERMINAL_REASONS.get(trigger))


def is_terminal(state: RunState) -> bool:
    return state is RunState.DONE


def legal_tools(state: RunState) -> frozenset[ToolName]:
    """Return the set of tools a model may call while in state."""
    return _LEGAL_TOOLS.get(state, frozenset())


def check_tool_call(state: RunState, tool: ToolName) -> None:
    """Raise IllegalToolCallError if tool is not legal in state."""
    if tool not in legal_tools(state):
        raise IllegalToolCallError(state, tool)
