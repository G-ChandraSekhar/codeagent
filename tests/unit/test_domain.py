"""Exhaustive tests for the Milestone 0 state machine in domain.py.

Every (state, trigger) pair and every (state, tool) pair is covered:
either it is asserted legal with the expected outcome, or asserted to
fail closed with the documented exception. Nothing is left unchecked.
"""

from __future__ import annotations

import pytest

from codeagent.domain import (
    ABORT_TRIGGERS,
    APPROVAL_DECISION_TO_TRIGGER,
    ApprovalDecision,
    ApprovalMode,
    BudgetKind,
    IllegalToolCallError,
    IllegalTransitionError,
    RunState,
    TerminalReason,
    ToolName,
    Trigger,
    TransitionResult,
    check_tool_call,
    is_terminal,
    legal_tools,
    transition,
)

ALL_STATES = list(RunState)
ALL_TRIGGERS = list(Trigger)
ALL_TOOLS = list(ToolName)

# The legal, non-abort transitions this milestone defines. Single source
# of truth for the happy-path and "everything else is illegal" tests, so
# the test file cannot drift from what it's actually asserting.
LEGAL_TRANSITIONS: dict[tuple[RunState, Trigger], RunState] = {
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

TERMINAL_REASONS_BY_TRIGGER: dict[Trigger, TerminalReason] = {
    Trigger.VERIFICATION_PASSED: TerminalReason.VERIFICATION_PASSED,
    Trigger.PLAN_REJECTED: TerminalReason.PLAN_REJECTED,
    Trigger.BUDGET_EXCEEDED: TerminalReason.BUDGET_EXCEEDED,
    Trigger.POLICY_VIOLATION: TerminalReason.POLICY_VIOLATION,
    Trigger.CANCELLED: TerminalReason.CANCELLED,
    Trigger.UNRECOVERABLE_ERROR: TerminalReason.UNRECOVERABLE_ERROR,
}

EXPECTED_ABORT_TRIGGERS = {
    Trigger.BUDGET_EXCEEDED,
    Trigger.POLICY_VIOLATION,
    Trigger.CANCELLED,
    Trigger.UNRECOVERABLE_ERROR,
}

EXPECTED_LEGAL_TOOLS: dict[RunState, frozenset[ToolName]] = {
    RunState.INIT: frozenset(),
    RunState.BASELINE: frozenset(),
    RunState.EXPLORE: frozenset(
        {
            ToolName.LIST_DIRECTORY,
            ToolName.READ_FILE,
            ToolName.SEARCH_TEXT,
            ToolName.PROPOSE_PLAN,
        }
    ),
    RunState.PLAN: frozenset(),
    RunState.APPROVAL: frozenset(),
    RunState.EXECUTE: frozenset({ToolName.APPLY_PATCH}),
    RunState.VERIFY: frozenset(),
    RunState.DONE: frozenset(),
}


def test_abort_trigger_set_matches_expected() -> None:
    assert ABORT_TRIGGERS == EXPECTED_ABORT_TRIGGERS


def test_every_trigger_is_covered_by_the_fixtures_above() -> None:
    """Guards the exhaustive tests below against a new Trigger member
    being added without also being classified as legal-table or abort."""
    covered = {t for (_, t) in LEGAL_TRANSITIONS} | ABORT_TRIGGERS
    assert covered == set(ALL_TRIGGERS)


def test_every_reachable_terminal_trigger_has_a_reason() -> None:
    terminal_triggers = {t for (_, t), s in LEGAL_TRANSITIONS.items() if s is RunState.DONE}
    terminal_triggers |= ABORT_TRIGGERS
    assert terminal_triggers == set(TERMINAL_REASONS_BY_TRIGGER)


def test_every_state_is_covered_by_the_tool_fixture_above() -> None:
    assert set(EXPECTED_LEGAL_TOOLS) == set(ALL_STATES)


# --------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------


def test_happy_path_solved_on_first_attempt() -> None:
    state = RunState.INIT
    steps = [
        (Trigger.RUN_STARTED, RunState.BASELINE),
        (Trigger.BASELINE_RECORDED, RunState.EXPLORE),
        (Trigger.PLAN_PROPOSED, RunState.PLAN),
        (Trigger.PLAN_RECORDED, RunState.APPROVAL),
        (Trigger.PLAN_APPROVED, RunState.EXECUTE),
        (Trigger.PATCH_APPLIED, RunState.VERIFY),
        (Trigger.VERIFICATION_PASSED, RunState.DONE),
    ]
    for trigger, expected_state in steps:
        result = transition(state, trigger)
        assert result.next_state is expected_state
        state = result.next_state

    assert is_terminal(state)
    assert result.terminal_reason is TerminalReason.VERIFICATION_PASSED


def test_repair_loop_completes_through_second_attempt_to_success() -> None:
    """A first verification failure sends the run back through EXPLORE
    for a full second attempt, ending in success — not just the single
    VERIFY -> EXPLORE hop in isolation."""
    state = RunState.EXECUTE
    steps = [
        (Trigger.PATCH_APPLIED, RunState.VERIFY),
        (Trigger.VERIFICATION_FAILED, RunState.EXPLORE),
        (Trigger.PLAN_PROPOSED, RunState.PLAN),
        (Trigger.PLAN_RECORDED, RunState.APPROVAL),
        (Trigger.PLAN_APPROVED, RunState.EXECUTE),
        (Trigger.PATCH_APPLIED, RunState.VERIFY),
        (Trigger.VERIFICATION_PASSED, RunState.DONE),
    ]
    result = None
    for trigger, expected_state in steps:
        result = transition(state, trigger)
        assert result.next_state is expected_state
        state = result.next_state

    assert is_terminal(state)
    assert result.terminal_reason is TerminalReason.VERIFICATION_PASSED


def test_plan_revision_loop_completes_to_success() -> None:
    """The second, plan-revision loop (APPROVAL -> EXPLORE) also needs an
    end-to-end exercise, not just the single-hop transition covered by
    the exhaustive parametrized tests below."""
    state = RunState.APPROVAL
    steps = [
        (Trigger.PLAN_REVISION_REQUESTED, RunState.EXPLORE),
        (Trigger.PLAN_PROPOSED, RunState.PLAN),
        (Trigger.PLAN_RECORDED, RunState.APPROVAL),
        (Trigger.PLAN_APPROVED, RunState.EXECUTE),
        (Trigger.PATCH_APPLIED, RunState.VERIFY),
        (Trigger.VERIFICATION_PASSED, RunState.DONE),
    ]
    result = None
    for trigger, expected_state in steps:
        result = transition(state, trigger)
        assert result.next_state is expected_state
        state = result.next_state

    assert is_terminal(state)
    assert result.terminal_reason is TerminalReason.VERIFICATION_PASSED


def test_plan_revision_requested_loops_back_to_explore_nonterminal() -> None:
    result = transition(RunState.APPROVAL, Trigger.PLAN_REVISION_REQUESTED)
    assert result.next_state is RunState.EXPLORE
    assert result.terminal_reason is None
    assert not is_terminal(result.next_state)


def test_plan_rejected_is_terminal_and_distinct_from_revision_requested() -> None:
    result = transition(RunState.APPROVAL, Trigger.PLAN_REJECTED)
    assert result.next_state is RunState.DONE
    assert result.terminal_reason is TerminalReason.PLAN_REJECTED


# --------------------------------------------------------------------
# Exhaustive legal-transition coverage
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "trigger", "expected_state"),
    [(s, t, dest) for (s, t), dest in LEGAL_TRANSITIONS.items()],
    ids=[f"{s.value}+{t.value}" for (s, t) in LEGAL_TRANSITIONS],
)
def test_every_legal_transition_lands_on_the_expected_state(
    state: RunState, trigger: Trigger, expected_state: RunState
) -> None:
    result = transition(state, trigger)
    assert result.next_state is expected_state
    expected_reason = TERMINAL_REASONS_BY_TRIGGER.get(trigger)
    assert result.terminal_reason is expected_reason


@pytest.mark.parametrize(
    ("state", "trigger"),
    [(s, t) for s in ALL_STATES for t in ABORT_TRIGGERS if s is not RunState.DONE],
    ids=[
        f"{s.value}+{t.value}"
        for s in ALL_STATES
        for t in ABORT_TRIGGERS
        if s is not RunState.DONE
    ],
)
def test_every_abort_trigger_ends_the_run_from_every_nonterminal_state(
    state: RunState, trigger: Trigger
) -> None:
    result = transition(state, trigger)
    assert result.next_state is RunState.DONE
    assert result.terminal_reason is TERMINAL_REASONS_BY_TRIGGER[trigger]


# --------------------------------------------------------------------
# Exhaustive illegal-transition coverage: fail closed, no exceptions
# --------------------------------------------------------------------


ALL_STATE_TRIGGER_PAIRS = [(s, t) for s in ALL_STATES for t in ALL_TRIGGERS]
ILLEGAL_STATE_TRIGGER_PAIRS = [
    (s, t)
    for (s, t) in ALL_STATE_TRIGGER_PAIRS
    if (s, t) not in LEGAL_TRANSITIONS
    and not (t in ABORT_TRIGGERS and s is not RunState.DONE)
]


def test_illegal_pairs_partition_covers_every_combination() -> None:
    """Sanity check on the test data itself: every (state, trigger) pair
    is classified as exactly legal or exactly illegal, none missed."""
    legal = set(LEGAL_TRANSITIONS) | {
        (s, t) for s in ALL_STATES for t in ABORT_TRIGGERS if s is not RunState.DONE
    }
    illegal = set(ILLEGAL_STATE_TRIGGER_PAIRS)
    assert legal | illegal == set(ALL_STATE_TRIGGER_PAIRS)
    assert legal & illegal == set()


@pytest.mark.parametrize(
    ("state", "trigger"),
    ILLEGAL_STATE_TRIGGER_PAIRS,
    ids=[f"{s.value}+{t.value}" for (s, t) in ILLEGAL_STATE_TRIGGER_PAIRS],
)
def test_every_illegal_state_trigger_combination_fails_closed(
    state: RunState, trigger: Trigger
) -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition(state, trigger)
    assert excinfo.value.state is state
    assert excinfo.value.trigger is trigger


def test_abort_triggers_are_illegal_once_already_done() -> None:
    for trigger in ABORT_TRIGGERS:
        with pytest.raises(IllegalTransitionError):
            transition(RunState.DONE, trigger)


# --------------------------------------------------------------------
# TransitionResult invariant: terminal_reason iff next_state is DONE
# --------------------------------------------------------------------


def test_transition_result_rejects_done_without_a_reason() -> None:
    with pytest.raises(ValueError):
        TransitionResult(RunState.DONE, None)


@pytest.mark.parametrize("state", [s for s in ALL_STATES if s is not RunState.DONE])
def test_transition_result_rejects_a_reason_on_a_nonterminal_state(state: RunState) -> None:
    with pytest.raises(ValueError):
        TransitionResult(state, TerminalReason.VERIFICATION_PASSED)


def test_transition_result_accepts_done_with_a_reason() -> None:
    result = TransitionResult(RunState.DONE, TerminalReason.CANCELLED)
    assert result.next_state is RunState.DONE
    assert result.terminal_reason is TerminalReason.CANCELLED


@pytest.mark.parametrize("state", [s for s in ALL_STATES if s is not RunState.DONE])
def test_transition_result_accepts_nonterminal_state_without_a_reason(state: RunState) -> None:
    result = TransitionResult(state, None)
    assert result.next_state is state
    assert result.terminal_reason is None


def test_transition_never_returns_a_result_violating_its_own_invariant() -> None:
    """Every result transition() can actually produce must itself satisfy
    TransitionResult's invariant — exercised over every legal pair."""
    for (state, trigger), expected_state in LEGAL_TRANSITIONS.items():
        result = transition(state, trigger)
        assert (result.next_state is RunState.DONE) == (result.terminal_reason is not None)
    for state in ALL_STATES:
        for trigger in ABORT_TRIGGERS:
            if state is RunState.DONE:
                continue
            result = transition(state, trigger)
            assert (result.next_state is RunState.DONE) == (result.terminal_reason is not None)


# --------------------------------------------------------------------
# is_terminal
# --------------------------------------------------------------------


@pytest.mark.parametrize("state", ALL_STATES)
def test_is_terminal_matches_done_state(state: RunState) -> None:
    assert is_terminal(state) == (state is RunState.DONE)


# --------------------------------------------------------------------
# Exhaustive tool-legality coverage
# --------------------------------------------------------------------


@pytest.mark.parametrize("state", ALL_STATES)
def test_legal_tools_matches_expected_set_per_state(state: RunState) -> None:
    assert legal_tools(state) == EXPECTED_LEGAL_TOOLS[state]


LEGAL_STATE_TOOL_PAIRS = [
    (s, tool) for s in ALL_STATES for tool in EXPECTED_LEGAL_TOOLS[s]
]
ILLEGAL_STATE_TOOL_PAIRS = [
    (s, tool)
    for s in ALL_STATES
    for tool in ALL_TOOLS
    if tool not in EXPECTED_LEGAL_TOOLS[s]
]


def test_state_tool_pairs_partition_covers_every_combination() -> None:
    legal = set(LEGAL_STATE_TOOL_PAIRS)
    illegal = set(ILLEGAL_STATE_TOOL_PAIRS)
    all_pairs = {(s, tool) for s in ALL_STATES for tool in ALL_TOOLS}
    assert legal | illegal == all_pairs
    assert legal & illegal == set()


@pytest.mark.parametrize(
    ("state", "tool"),
    LEGAL_STATE_TOOL_PAIRS,
    ids=[f"{s.value}+{tool.value}" for (s, tool) in LEGAL_STATE_TOOL_PAIRS],
)
def test_every_legal_tool_call_is_accepted(state: RunState, tool: ToolName) -> None:
    check_tool_call(state, tool)  # must not raise


@pytest.mark.parametrize(
    ("state", "tool"),
    ILLEGAL_STATE_TOOL_PAIRS,
    ids=[f"{s.value}+{tool.value}" for (s, tool) in ILLEGAL_STATE_TOOL_PAIRS],
)
def test_every_illegal_tool_call_fails_closed(state: RunState, tool: ToolName) -> None:
    with pytest.raises(IllegalToolCallError) as excinfo:
        check_tool_call(state, tool)
    assert excinfo.value.state is state
    assert excinfo.value.tool is tool


def test_run_process_does_not_exist_as_a_tool() -> None:
    """Pins ADR 0001: no model-callable process-execution tool in v1."""
    assert "RUN_PROCESS" not in ToolName.__members__
    assert all(tool.value != "run_process" for tool in ToolName)


# --------------------------------------------------------------------
# Stable enum serialization values (corrections 6 / 11)
# --------------------------------------------------------------------


def test_run_state_values_are_pinned() -> None:
    assert {s.value for s in RunState} == {
        "INIT",
        "BASELINE",
        "EXPLORE",
        "PLAN",
        "APPROVAL",
        "EXECUTE",
        "VERIFY",
        "DONE",
    }


def test_trigger_values_are_pinned() -> None:
    assert {t.value for t in Trigger} == {
        "run_started",
        "baseline_recorded",
        "plan_proposed",
        "plan_recorded",
        "plan_approved",
        "plan_rejected",
        "plan_revision_requested",
        "patch_applied",
        "verification_passed",
        "verification_failed",
        "budget_exceeded",
        "policy_violation",
        "cancelled",
        "unrecoverable_error",
    }


def test_terminal_reason_values_are_pinned() -> None:
    assert {r.value for r in TerminalReason} == {
        "verification_passed",
        "plan_rejected",
        "budget_exceeded",
        "policy_violation",
        "cancelled",
        "unrecoverable_error",
    }


def test_tool_name_values_are_pinned() -> None:
    assert {t.value for t in ToolName} == {
        "list_directory",
        "read_file",
        "search_text",
        "propose_plan",
        "apply_patch",
    }


def test_budget_kind_values_are_pinned() -> None:
    assert {k.value for k in BudgetKind} == {
        "repair_iterations",
        "plan_revisions",
        "tool_calls",
        "tokens",
        "cost",
        "wall_clock",
    }


def test_approval_decision_values_are_pinned() -> None:
    assert {d.value for d in ApprovalDecision} == {
        "approved",
        "rejected",
        "revision_requested",
    }


def test_approval_mode_values_are_pinned() -> None:
    assert {m.value for m in ApprovalMode} == {
        "interactive",
        "plan-file",
        "none",
    }


# --------------------------------------------------------------------
# ApprovalDecision -> Trigger mapping
# --------------------------------------------------------------------


def test_every_approval_decision_maps_to_exactly_one_trigger() -> None:
    assert set(APPROVAL_DECISION_TO_TRIGGER) == set(ApprovalDecision)
    assert len(set(APPROVAL_DECISION_TO_TRIGGER.values())) == len(ApprovalDecision)


@pytest.mark.parametrize(
    ("decision", "expected_trigger", "expected_state"),
    [
        (ApprovalDecision.APPROVED, Trigger.PLAN_APPROVED, RunState.EXECUTE),
        (ApprovalDecision.REJECTED, Trigger.PLAN_REJECTED, RunState.DONE),
        (
            ApprovalDecision.REVISION_REQUESTED,
            Trigger.PLAN_REVISION_REQUESTED,
            RunState.EXPLORE,
        ),
    ],
)
def test_approval_decision_maps_to_a_trigger_legal_in_approval_state(
    decision: ApprovalDecision, expected_trigger: Trigger, expected_state: RunState
) -> None:
    trigger = APPROVAL_DECISION_TO_TRIGGER[decision]
    assert trigger is expected_trigger
    result = transition(RunState.APPROVAL, trigger)
    assert result.next_state is expected_state
