"""Trace pairing checks for ledger D4b (D4 plan revision 6 §7.1a and
readiness addendum J1).

`assert_model_pairing` is strict: every `ModelRequestStarted` is followed
immediately by exactly one outcome event with the same `request_id`. A run
ending `UNCLASSIFIED_FAILURE` never excuses a missing pair; only a request ID
named in `allow_unpaired` may lack one, and every named ID must actually be
unpaired. `assert_tool_pairing` requires each model-requested or dispatched
tool call to emit `ToolRequested`, `PolicyDecisionRecorded` and
`ToolCompleted` once each, in that order and contiguously for model calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from codeagent import domain, events

MODEL_OUTCOMES = (events.ModelResponseReceived, events.ModelResponseRejected, events.ModelRequestFailed)


def assert_model_pairing(trace: Sequence[events.Event], allow_unpaired: Iterable[str] = ()) -> None:
    allowed = frozenset(allow_unpaired)
    started: list[str] = []
    unpaired: set[str] = set()
    paired: set[str] = set()
    for index, event in enumerate(trace):
        if isinstance(event, MODEL_OUTCOMES):
            previous = trace[index - 1] if index else None
            if not (isinstance(previous, events.ModelRequestStarted) and previous.request_id == event.request_id):
                raise AssertionError(f"{type(event).__name__} for {event.request_id!r} does not follow its start")
            if event.request_id in paired:
                raise AssertionError(f"request {event.request_id!r} has more than one outcome")
            paired.add(event.request_id)
        if not isinstance(event, events.ModelRequestStarted):
            continue
        if event.request_id in started:
            raise AssertionError(f"request {event.request_id!r} started twice")
        started.append(event.request_id)
        following = trace[index + 1] if index + 1 < len(trace) else None
        if not (isinstance(following, MODEL_OUTCOMES) and following.request_id == event.request_id):
            unpaired.add(event.request_id)
    if unpaired != allowed:
        raise AssertionError(f"unpaired model requests {sorted(unpaired)} != allowed {sorted(allowed)}")


def assert_tool_pairing(trace: Sequence[events.Event]) -> None:
    order: dict[str, list[type]] = {}
    positions: dict[str, list[int]] = {}
    for index, event in enumerate(trace):
        if isinstance(event, (events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted)):
            order.setdefault(event.tool_call_id, []).append(type(event))
            positions.setdefault(event.tool_call_id, []).append(index)
    expected = [events.ToolRequested, events.PolicyDecisionRecorded, events.ToolCompleted]
    for tool_call_id, kinds in order.items():
        if kinds != expected:
            raise AssertionError(f"tool call {tool_call_id!r} emitted {[k.__name__ for k in kinds]}")
        request = trace[positions[tool_call_id][0]]
        if request.tool is not domain.ToolName.APPLY_PATCH and positions[tool_call_id] != list(
            range(positions[tool_call_id][0], positions[tool_call_id][0] + 3)
        ):
            raise AssertionError(f"model-requested tool call {tool_call_id!r} is not contiguous")
