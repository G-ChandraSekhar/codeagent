"""Deterministic scripted model support (ledger D4b).

`ScriptedModel` answers each request with the next scripted step: a
`ModelTurn`, any other object (to exercise rejection), a callable that
receives the `ModelContext`, or an exception to raise. The builders produce
well-formed byte-level calls from ordinary Python values.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from codeagent.events import ModelResponseStatus
from codeagent.model_protocol import ModelContext, ModelTurn, ToolCall
from codeagent.tool_schema import ProposePlanArgs

_counter = {"n": 0}


def _next_call_id() -> bytes:
    _counter["n"] += 1
    return f"call_{_counter['n']}".encode()


def tool_call(name: str, arguments: dict[str, Any] | bytes, call_id: bytes | None = None) -> ToolCall:
    raw = arguments if isinstance(arguments, bytes) else json.dumps(arguments).encode("utf-8")
    return ToolCall(provider_call_id=call_id or _next_call_id(), name=name.encode(), arguments_json=raw)


def read_file_call(path: str, start_line: int | None = None, end_line: int | None = None, **kw) -> ToolCall:
    args: dict[str, Any] = {"path": path}
    if start_line is not None:
        args["start_line"] = start_line
    if end_line is not None:
        args["end_line"] = end_line
    return tool_call("read_file", args, **kw)


def list_directory_call(path: str = ".", **kw) -> ToolCall:
    return tool_call("list_directory", {"path": path}, **kw)


def search_text_call(query: str, path: str = ".", cursor: str | None = None, limit: int | None = None, **kw) -> ToolCall:
    args: dict[str, Any] = {"query": query, "path": path}
    if cursor is not None:
        args["cursor"] = cursor
    if limit is not None:
        args["limit"] = limit
    return tool_call("search_text", args, **kw)


def propose_plan_call(plan: ProposePlanArgs, **kw) -> ToolCall:
    return tool_call(
        "propose_plan",
        {
            "change": {
                "expected_text": plan.change.expected_text,
                "new_text": plan.change.new_text,
                "path": plan.change.path,
            },
            "evidence_refs": list(plan.evidence_refs),
            "problem_hypothesis": plan.problem_hypothesis,
            "risk_notes": plan.risk_notes,
            "verification_intent": plan.verification_intent,
        },
        **kw,
    )


def turn(
    *calls: ToolCall,
    status: ModelResponseStatus = ModelResponseStatus.COMPLETED,
    model_name: bytes = b"fake-model-v1",
    response_id: bytes = b"resp-1",
    incomplete_reason: bytes | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> ModelTurn:
    return ModelTurn(
        model_name=model_name,
        response_id=response_id,
        status=status,
        incomplete_reason=incomplete_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls=tuple(calls),
    )


Step = ModelTurn | object | Callable[[ModelContext], Any] | BaseException


class ScriptedModel:
    """Plays `steps` in order, one per request; running out is an
    assertion failure, never a silent repeat."""

    model_name = "fake-model-v1"

    def __init__(self, steps: list[Step]) -> None:
        self._steps = list(steps)
        self.contexts: list[ModelContext] = []
        self.returned: list[Any] = []

    def respond(self, context: ModelContext) -> Any:
        self.contexts.append(context)
        if not self._steps:
            raise AssertionError("ScriptedModel ran out of scripted turns")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step) and not isinstance(step, ModelTurn):
            step = step(context)
            if isinstance(step, BaseException):
                raise step
        self.returned.append(step)
        return step


def last_result(context: ModelContext, tool: str | None = None) -> dict[str, Any] | None:
    """The newest TOOL_RESULT body in the context (optionally for one tool)."""
    for item in reversed(context.transcript):
        if item.kind.value == "tool_result" and (tool is None or item.body["tool"] == tool):
            return item.body
    return None
