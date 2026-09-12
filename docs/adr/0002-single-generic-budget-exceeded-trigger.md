# ADR 0002: Single generic BUDGET_EXCEEDED trigger, with detail carried by BudgetKind

Status: Accepted

## Context

`domain.py`'s state machine needs to end a run when some resource limit is
hit (repair iterations, plan revisions, tool calls, tokens, cost, or wall
clock). The first design split this into three separate `Trigger`/
`TerminalReason` pairs (`REPAIR_ITERATIONS_EXHAUSTED`, `COST_LIMIT_EXCEEDED`,
`WALL_CLOCK_EXCEEDED`), reasoning that `PROJECT_BRIEF.md`'s completion
criteria require the final report to explain cost/token/latency
specifically, and a single generic reason "won't support that."

On review, this conflated two different layers: the *state machine*, which
only needs to know a budget ended the run and route to `DONE`, and the
*event log*, which is where the specific, human-facing detail belongs. The
three-way split meant every new budget dimension (there are six —
`BudgetKind` already lists `TOOL_CALLS`, `PLAN_REVISIONS` in addition to the
original three) would require adding new `Trigger`/`TerminalReason` members,
growing `domain.py`'s core vocabulary for something that is fundamentally
event-payload detail, not state-machine detail.

## Decision

`Trigger.BUDGET_EXCEEDED` and `TerminalReason.BUDGET_EXCEEDED` are each a
single, generic member. `BudgetKind` (a separate enum: `REPAIR_ITERATIONS`,
`PLAN_REVISIONS`, `TOOL_CALLS`, `TOKENS`, `COST`, `WALL_CLOCK`) is not
consulted by the state machine at all — it exists purely for whoever fires
the trigger (the controller) and for the `events.BudgetExceeded` event,
which carries `kind`, `limit_value`, `observed_value`, and
`projected_value`. The state-transition table (`domain._TRANSITIONS`,
`ABORT_TRIGGERS`) stays independent of how many budget dimensions the
system tracks.

## Consequences

- Adding a budget dimension may require extending `BudgetKind` in
  `domain.py` and its pinned-value tests, plus `budgets`/reporting
  behavior. It does not require adding `Trigger` or `TerminalReason`
  members or changing the state-transition topology.
- `PROJECT_BRIEF.md`'s cost/token/latency reporting requirement is still
  met: `events.BudgetExceeded.kind` plus `RunFinished`'s cost/token/duration
  fields carry the specific detail a report needs — that detail was never
  actually state-machine-shaped information.
- This was implemented, then reverted from the three-way split, then
  re-implemented as the generic form within the same Milestone-0 work — the
  domain test suite (`tests/unit/test_domain.py`) was re-run after the
  revert specifically to confirm exhaustive (state, trigger) coverage
  survived intact, not just that the code compiled. See `ENGINEERING_LOG.md`
  for the full back-and-forth.
- A downside accepted knowingly: `Trigger.BUDGET_EXCEEDED` alone doesn't
  tell a reader glancing at `domain.py` *which* budget ended a run — they
  have to know to look at the accompanying `events.BudgetExceeded.kind`.
  This is judged acceptable because `domain.py` is the mechanical
  state-machine layer, not the human-facing report.
