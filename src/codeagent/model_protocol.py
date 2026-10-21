"""The general model protocol (ledger D4b; D4 plan revisions 5 and 6 and
the D4b readiness addendum).

A `ModelClient` answers one `ModelContext` with one `ModelTurn`: an
envelope of byte-valued metadata, a `ModelResponseStatus` and up to eight
tool calls. Everything a client returns is untrusted (threat-model A1);
`classify_turn` validates it without raising, and the controller decides
what each classification means for events, budgets and termination.

Feedback reaches the model only through `ModelContext.transcript`, an
in-memory value handed to the client; nothing here persists it. D4 has no
live provider, so no repository or test content leaves the process.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Protocol

from codeagent.domain import ToolName
from codeagent.events import ModelResponseStatus, ResponseRejectionReason
from codeagent.tool_schema import (
    MAX_NOTE_BYTES,
    ArgumentRejection,
    ToolArgs,
    ToolArgumentError,
    canonical_json_bytes,
    parse_tool_arguments,
    sanitize_recorded_text,
)

MAX_CALLS_PER_TURN = 8
MALFORMED_CHARGE_CAP = MAX_CALLS_PER_TURN + 1
MAX_PROVIDER_CALL_ID_BYTES = 128
MAX_TOOL_NAME_BYTES = 64
MAX_RESPONSE_BYTES = 1_048_576
MAX_TOKENS = 10**9
CONTEXT_CAP_BYTES = 224 * 1024
VERIFICATION_TAIL_BYTES = 8 * 1024
BASELINE_TAIL_BYTES = 2 * 1024

# Conservative per-item maxima for the static context-fit proof
# (`RunConfig`): canonical sizes after sanitization, envelopes included.
TOOL_RESULT_ITEM_MAX_BYTES = 80 * 1024 + 3 * 1024
VERIFICATION_ITEM_MAX_BYTES = 2 * VERIFICATION_TAIL_BYTES + 1024
REVISION_ITEM_MAX_BYTES = 2 * MAX_NOTE_BYTES + 512
BASELINE_MAX_BYTES = 2 * BASELINE_TAIL_BYTES + 512
CONTEXT_ENVELOPE_MAX_BYTES = 512

_METADATA_RE = re.compile(rb"[A-Za-z0-9._:-]{1,128}")
_INCOMPLETE_REASON_RE = re.compile(rb"[a-z0-9_]{1,64}")
_PROVIDER_CALL_ID_RE = re.compile(rb"[A-Za-z0-9_-]{1,128}")

INVALID_MODEL_NAME = "codeagent.invalid-model-name"
INVALID_RESPONSE_ID = "codeagent.invalid-response-id"
INVALID_INCOMPLETE_REASON = "codeagent_invalid_reason"

# Argument failures that make the whole turn malformed; every other
# `ArgumentRejection` is a per-call policy denial.
TURN_LEVEL_REJECTIONS = frozenset(
    {
        ArgumentRejection.NOT_BYTES,
        ArgumentRejection.OVERSIZED,
        ArgumentRejection.INVALID_UTF8,
        ArgumentRejection.INVALID_JSON,
        ArgumentRejection.DUPLICATE_KEY,
        ArgumentRejection.NON_FINITE_NUMBER,
        ArgumentRejection.NOT_AN_OBJECT,
        ArgumentRejection.LONE_SURROGATE,
    }
)


@dataclass(frozen=True)
class ToolCall:
    provider_call_id: bytes
    name: bytes
    arguments_json: bytes


@dataclass(frozen=True)
class ModelTurn:
    model_name: bytes
    response_id: bytes
    status: ModelResponseStatus
    incomplete_reason: bytes | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    calls: tuple[ToolCall, ...] = ()


@unique
class FeedbackKind(str, Enum):
    TOOL_RESULT = "tool_result"
    TOOL_DENIED = "tool_denied"
    MALFORMED_RESPONSE = "malformed_response"
    VERIFICATION_FAILED = "verification_failed"
    REVISION_REQUESTED = "revision_requested"


DROPPABLE_KINDS = frozenset({FeedbackKind.TOOL_RESULT, FeedbackKind.TOOL_DENIED, FeedbackKind.MALFORMED_RESPONSE})


@dataclass(frozen=True)
class FeedbackItem:
    kind: FeedbackKind
    pass_index: int
    body: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "pass_index": self.pass_index, **self.body}

    @property
    def size(self) -> int:
        return len(canonical_json_bytes(self.as_json()))


@dataclass(frozen=True)
class ModelContext:
    task_statement: str
    baseline: dict[str, Any]
    pass_index: int
    tool_calls_remaining: int
    transcript: tuple[FeedbackItem, ...]
    dropped_count: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "dropped_count": self.dropped_count,
            "pass_index": self.pass_index,
            "task_statement": self.task_statement,
            "tool_calls_remaining": self.tool_calls_remaining,
            "transcript": [item.as_json() for item in self.transcript],
        }

    @property
    def size(self) -> int:
        return len(canonical_json_bytes(self.as_json()))


class ModelClient(Protocol):
    @property
    def model_name(self) -> str: ...
    def respond(self, context: ModelContext) -> ModelTurn: ...


def valid_model_name(value: object) -> bool:
    return isinstance(value, str) and _METADATA_RE.fullmatch(value.encode("ascii", "replace")) is not None


def encoded_tail(text: str, limit: int) -> str:
    """The longest suffix of `sanitize_recorded_text(text)` whose canonical
    JSON string encoding is at most `limit` bytes."""
    clean = sanitize_recorded_text(text)

    def size(s: str) -> int:
        return len(json.dumps(s, ensure_ascii=False).encode("utf-8"))

    if size(clean) <= limit:
        return clean
    lo, hi = 0, len(clean)  # find the smallest start index that fits
    while lo < hi:
        mid = (lo + hi) // 2
        if size(clean[mid:]) <= limit:
            hi = mid
        else:
            lo = mid + 1
    return clean[lo:]


# --- envelope classification ------------------------------------------------


@dataclass(frozen=True)
class Defect:
    """Categorical evidence for an invalid field: never the raw value."""

    reason: str
    byte_length: int
    sha256: str | None
    call_index: int | None = None

    def describe(self) -> str:
        parts = [f"reason={self.reason}"]
        if self.call_index is not None:
            parts.append(f"call_index={self.call_index}")
        parts.append(f"field_bytes={self.byte_length}")
        if self.sha256 is not None:
            parts.append(f"field_sha256={self.sha256}")
        return "; ".join(parts)


def _defect(reason: str, value: object, call_index: int | None = None) -> Defect:
    if isinstance(value, bytes):
        return Defect(reason, len(value), hashlib.sha256(value).hexdigest(), call_index)
    return Defect(reason, -1, None, call_index)


@dataclass(frozen=True)
class ParsedCall:
    """A call in a structurally valid turn. `args` is set when strict
    parsing and the dataclass's own validation succeeded; otherwise
    `rejection` holds the per-call denial category."""

    index: int
    provider_call_id: str
    tool: ToolName
    raw_arguments: bytes
    args: ToolArgs | None
    rejection: ArgumentRejection | None


@dataclass(frozen=True)
class Classified:
    rejected: ResponseRejectionReason | None = None
    status: ModelResponseStatus | None = None
    model_name: str = INVALID_MODEL_NAME
    response_id: str = INVALID_RESPONSE_ID
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_call_count: int = 0
    declared_calls: int = 0
    metadata_defect: Defect | None = None
    malformed: Defect | None = None  # turn-level defect in calls (COMPLETED only)
    calls: tuple[ParsedCall, ...] = field(default_factory=tuple)


def _decode_metadata(value: object, pattern: re.Pattern[bytes], reason: str) -> tuple[str | None, Defect | None]:
    if isinstance(value, bytes) and pattern.fullmatch(value):
        return value.decode("ascii"), None
    return None, _defect(reason, value)


def _token(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        return None, False
    return value, True


def _classify_calls(calls: tuple[object, ...]) -> tuple[tuple[ParsedCall, ...], Defect | None]:
    if not calls:
        return (), Defect("no_tool_calls", 0, None)
    if len(calls) > MAX_CALLS_PER_TURN:
        return (), Defect("too_many_tool_calls", len(calls), None)
    total = 0
    seen: set[str] = set()
    parsed: list[ParsedCall] = []
    for index, call in enumerate(calls):
        if not isinstance(call, ToolCall):
            return (), Defect("call_not_a_tool_call", -1, None, index)
        for value, reason in (
            (call.provider_call_id, "provider_call_id_invalid"),
            (call.name, "tool_name_invalid"),
            (call.arguments_json, "arguments_not_bytes"),
        ):
            if not isinstance(value, bytes):
                return (), _defect(reason, value, index)
        if not _PROVIDER_CALL_ID_RE.fullmatch(call.provider_call_id):
            return (), _defect("provider_call_id_invalid", call.provider_call_id, index)
        provider_call_id = call.provider_call_id.decode("ascii")
        if provider_call_id in seen:
            return (), _defect("provider_call_id_duplicate", call.provider_call_id, index)
        seen.add(provider_call_id)
        if not 1 <= len(call.name) <= MAX_TOOL_NAME_BYTES:
            return (), _defect("tool_name_invalid", call.name, index)
        try:
            tool = ToolName(call.name.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            return (), _defect("tool_name_unknown", call.name, index)
        total += len(call.provider_call_id) + len(call.name) + len(call.arguments_json)
        if total > MAX_RESPONSE_BYTES:
            return (), Defect("response_too_large", total, None, index)
        args: ToolArgs | None = None
        rejection: ArgumentRejection | None = None
        try:
            args = parse_tool_arguments(tool, call.arguments_json)
        except ToolArgumentError as exc:
            if exc.reason in TURN_LEVEL_REJECTIONS:
                return (), _defect(f"arguments_{exc.reason.value}", call.arguments_json, index)
            rejection = exc.reason
        parsed.append(ParsedCall(index, provider_call_id, tool, call.arguments_json, args, rejection))
    return tuple(parsed), None


def classify_turn(turn: object) -> Classified:
    """Validate an untrusted `respond()` result without raising."""
    if not isinstance(turn, ModelTurn):
        return Classified(rejected=ResponseRejectionReason.NOT_A_MODEL_TURN)
    if not isinstance(turn.status, ModelResponseStatus):
        return Classified(rejected=ResponseRejectionReason.STATUS_UNRECOGNIZED)
    status = turn.status
    defect: Defect | None = None
    model_name, d = _decode_metadata(turn.model_name, _METADATA_RE, "model_name_invalid")
    defect = defect or d
    response_id, d = _decode_metadata(turn.response_id, _METADATA_RE, "response_id_invalid")
    defect = defect or d
    input_tokens, ok_in = _token(turn.input_tokens)
    output_tokens, ok_out = _token(turn.output_tokens)
    if not (ok_in and ok_out):
        defect = defect or Defect("token_count_invalid", -1, None)
    incomplete_reason: str | None = None
    if status is ModelResponseStatus.INCOMPLETE:
        incomplete_reason, d = _decode_metadata(turn.incomplete_reason, _INCOMPLETE_REASON_RE, "incomplete_reason_invalid")
        if d is not None:
            defect = defect or d
            incomplete_reason = INVALID_INCOMPLETE_REASON
    elif turn.incomplete_reason is not None:
        defect = defect or _defect("incomplete_reason_unexpected", turn.incomplete_reason)
    calls_value = turn.calls
    declared = len(calls_value) if isinstance(calls_value, tuple) else 1
    tool_call_count = len(calls_value) if isinstance(calls_value, tuple) else 0
    parsed: tuple[ParsedCall, ...] = ()
    malformed: Defect | None = None
    if status is ModelResponseStatus.COMPLETED:
        if not isinstance(calls_value, tuple):
            malformed = Defect("calls_not_a_tuple", -1, None)
        else:
            parsed, malformed = _classify_calls(calls_value)
    return Classified(
        status=status,
        model_name=model_name or INVALID_MODEL_NAME,
        response_id=response_id or INVALID_RESPONSE_ID,
        incomplete_reason=incomplete_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_call_count=tool_call_count,
        declared_calls=declared,
        metadata_defect=defect,
        malformed=malformed,
        calls=parsed,
    )
