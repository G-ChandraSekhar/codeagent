"""Neutral tool contract shared by the repository toolkit and the controller
(ledger D4a; D4 plan revisions 5 and 6).

Holds only declarative, filesystem-free pieces:

- argument dataclasses for the four model-callable tools, every instance
  validated at construction;
- `parse_tool_arguments(tool, raw: bytes)`, the single strict-UTF-8,
  strict-JSON entry point for model-supplied arguments;
- the static path rules (shape, depth, the Git administrative name);
- `list_directory` cursor encoding and the static half of `search_text`
  cursor validation (the stateful chain check lives in `repo_tools`);
- `redact_arguments`, the canonical `ToolRequested` representation;
- `compute_digests`, the only plan/change digest implementation;
- `ToolOutcome`, operator-note helpers, and `ToolCleanupUnconfirmedError`.

Imports only `domain` and `errors`, so both `repo_tools` and `controller`
can depend on it without a cycle. Nothing here touches the filesystem.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, unique
from typing import Any

from codeagent.domain import ToolName
from codeagent.errors import OperationalError

# --- Limits -------------------------------------------------------------

MAX_READ_TOOL_ARGUMENT_BYTES = 4096
MAX_PROPOSE_PLAN_ARGUMENT_BYTES = 917_504
MAX_PATH_BYTES = 512
MAX_PATH_DEPTH = 32
# Equal to `_git_safety.MAX_PATCH_BLOB_BYTES` (pinned by a parity test; not
# imported so this module stays free of the Git layer).
MAX_CHANGE_TEXT_BYTES = 65_536
MAX_HYPOTHESIS_CHARS = 2000
MAX_VERIFICATION_INTENT_CHARS = 500
MAX_RISK_NOTES_CHARS = 1500
MAX_EVIDENCE_REFS = 20
MAX_QUERY_CHARS = 200
MAX_CURSOR_BYTES = 2048
MAX_LIST_AFTER_BYTES = 1024
MAX_NOTE_BYTES = 2048
LIST_LIMIT_MAX = 500
SEARCH_LIMIT_MAX = 100
READ_MAX_LINES_PER_CALL = 2000
MAX_SUMMARY_CHARS = 200

_EVIDENCE_REF_RE = re.compile(r"tc-[0-9]{1,9}-[0-9]{1,9}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")

# Git's own `is_hfs_dotgit` ignorable code points.
_HFS_IGNORABLE = frozenset(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)
_ADMINISTRATIVE_NAMES = frozenset({".git", "git~1"})
# Test-only hook (R10/R10b): disabling it leaves only the inode rule.
_ADMIN_NAME_RULE_ENABLED = True


@unique
class ArgumentRejection(str, Enum):
    """Categorical reason a tool argument was rejected. Values are the only
    evidence that reaches events; raw arguments never do."""

    NOT_BYTES = "not_bytes"
    OVERSIZED = "oversized"
    NOT_MODEL_CALLABLE = "not_model_callable"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_JSON = "invalid_json"
    DUPLICATE_KEY = "duplicate_key"
    NON_FINITE_NUMBER = "non_finite_number"
    NOT_AN_OBJECT = "not_an_object"
    UNKNOWN_KEY = "unknown_key"
    MISSING_KEY = "missing_key"
    NULL_VALUE = "null_value"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    TOO_LONG = "too_long"
    EMPTY = "empty"
    LONE_SURROGATE = "lone_surrogate"
    PATH_EMPTY = "path_empty"
    PATH_ABSOLUTE = "path_absolute"
    PATH_DOTDOT = "path_dotdot"
    PATH_DOT = "path_dot"
    PATH_EMPTY_COMPONENT = "path_empty_component"
    PATH_BACKSLASH = "path_backslash"
    PATH_CONTROL_CHARACTER = "path_control_character"
    PATH_TOO_LONG = "path_too_long"
    PATH_TOO_DEEP = "path_too_deep"
    ADMINISTRATIVE_PATH = "administrative_path"
    CURSOR_INVALID = "cursor_invalid"
    CURSOR_MISMATCH = "cursor_mismatch"
    EVIDENCE_REF_INVALID = "evidence_ref_invalid"
    CHANGE_UNCHANGED = "change_unchanged"
    QUERY_CONTROL_CHARACTER = "query_control_character"


class ToolArgumentError(ValueError):
    """A rejected tool argument. `message` is fixed text plus, for path
    rules only, a sanitized fragment of the model-supplied path."""

    def __init__(self, reason: ArgumentRejection, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ToolCleanupUnconfirmedError(Exception):
    """A tool call could not confirm that every descriptor it opened was
    closed (D4 plan revision 5 §5.7). Cleanup dominates: the call's result
    is discarded, and a body failure (including an interrupt) is kept as
    `body_failure` and `__cause__`, never re-raised. The message is fixed
    and never contains wrapped exception text."""

    MESSAGE = "repository tool descriptor cleanup unconfirmed"

    def __init__(
        self, close_failures: tuple[BaseException, ...], body_failure: BaseException | None
    ) -> None:
        super().__init__(self.MESSAGE)
        self.close_failures = close_failures
        self.body_failure = body_failure


# --- Small helpers ---------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """The canonical form `events.ToolRequested` validates."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def safe_path_fragment(value: str) -> str:
    cleaned = "".join("?" if (_is_control(ch) or 0xD800 <= ord(ch) <= 0xDFFF) else ch for ch in value)
    if len(cleaned) > 200:
        cleaned = cleaned[:200] + "...(truncated)"
    return cleaned


def sanitize_recorded_text(value: str) -> str:
    """Replace C0/C1 controls other than newline and tab with '?'."""
    return "".join("?" if _is_control(ch) and ch not in "\n\t" else ch for ch in value)


def is_meaningful_note(value: str) -> bool:
    """True when the *original* text has at least one letter, mark, number,
    punctuation or symbol character (revision 5 §8.4). Whitespace, controls,
    format, surrogate, private-use and unassigned code points do not count,
    so sanitizing can never manufacture meaning."""
    return any(unicodedata.category(ch)[0] in "LMNPS" for ch in value)


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, f"{field} must be a string")
    if _has_surrogate(value):
        raise ToolArgumentError(ArgumentRejection.LONE_SURROGATE, f"{field} must not contain a lone surrogate")
    return value


def _require_int(value: object, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, f"{field} must be an integer")
    if not low <= value <= high:
        raise ToolArgumentError(ArgumentRejection.OUT_OF_RANGE, f"{field} is out of range")
    return value


def _require_chars(value: str, field: str, high: int, *, allow_empty: bool) -> str:
    if not value and not allow_empty:
        raise ToolArgumentError(ArgumentRejection.EMPTY, f"{field} must be nonempty")
    if len(value) > high:
        raise ToolArgumentError(ArgumentRejection.TOO_LONG, f"{field} is too long")
    return value


# --- Static path rules ----------------------------------------------------


def is_administrative_name(component: str) -> bool:
    if not _ADMIN_NAME_RULE_ENABLED:
        return False
    stripped = "".join(ch for ch in component if ord(ch) not in _HFS_IGNORABLE)
    return stripped.casefold() in _ADMINISTRATIVE_NAMES


def check_relative_path(path: object, *, allow_root: bool) -> tuple[str, ...]:
    """Validate a model-supplied worktree path without touching the
    filesystem and return its components (`()` for the root `"."`).

    Depth is the component count from the worktree root."""
    if not isinstance(path, str):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, "path must be a string")
    if path == "":
        raise ToolArgumentError(ArgumentRejection.PATH_EMPTY, "path must be nonempty")
    frag = safe_path_fragment(path)
    if _has_surrogate(path):
        raise ToolArgumentError(ArgumentRejection.LONE_SURROGATE, f"path must not contain a lone surrogate: {frag!r}")
    if path.startswith("/"):
        raise ToolArgumentError(ArgumentRejection.PATH_ABSOLUTE, f"path must not be absolute: {frag!r}")
    if any(_is_control(ch) for ch in path):
        raise ToolArgumentError(
            ArgumentRejection.PATH_CONTROL_CHARACTER, f"path must not contain control characters: {frag!r}"
        )
    if "\\" in path:
        raise ToolArgumentError(ArgumentRejection.PATH_BACKSLASH, f"path must not contain '\\': {frag!r}")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ToolArgumentError(ArgumentRejection.PATH_TOO_LONG, f"path exceeds {MAX_PATH_BYTES} bytes: {frag!r}")
    if path == ".":
        if allow_root:
            return ()
        raise ToolArgumentError(ArgumentRejection.PATH_DOT, f"path must name a file, not the root: {frag!r}")
    components = tuple(path.split("/"))
    for component in components:
        if component == "..":
            raise ToolArgumentError(ArgumentRejection.PATH_DOTDOT, f"path must not contain '..': {frag!r}")
    for component in components:
        if component == "":
            raise ToolArgumentError(
                ArgumentRejection.PATH_EMPTY_COMPONENT, f"path must not contain an empty component: {frag!r}"
            )
        if component == ".":
            raise ToolArgumentError(ArgumentRejection.PATH_DOT, f"path must not contain '.': {frag!r}")
    if len(components) > MAX_PATH_DEPTH:
        raise ToolArgumentError(
            ArgumentRejection.PATH_TOO_DEEP, f"path is deeper than {MAX_PATH_DEPTH} components: {frag!r}"
        )
    for component in components:
        if is_administrative_name(component):
            raise ToolArgumentError(
                ArgumentRejection.ADMINISTRATIVE_PATH, f"path names the Git administrative entry: {frag!r}"
            )
    return components


# --- Cursors ---------------------------------------------------------------


def b64url(data: bytes) -> str:
    """Unpadded base64url, as used in cursors and non-UTF-8 listing names."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


_b64encode = b64url


def _b64decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None


def _decode_cursor_object(cursor: object) -> dict[str, Any]:
    def bad() -> ToolArgumentError:
        return ToolArgumentError(ArgumentRejection.CURSOR_INVALID, "cursor is invalid")

    if not isinstance(cursor, str):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, "cursor must be a string")
    if not cursor or len(cursor) > MAX_CURSOR_BYTES:
        raise bad()
    try:
        raw = _b64decode(cursor)
        obj = _strict_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, ToolArgumentError):
        raise bad() from None
    if not isinstance(obj, dict) or canonical_json_bytes(obj) != raw:
        raise bad()
    return obj


def encode_list_cursor(path: str, after: bytes) -> str:
    return _b64encode(
        canonical_json_bytes({"after": _b64encode(after), "path": path, "tool": "list_directory", "v": 1})
    )


def decode_list_cursor(cursor: object, path: str) -> bytes:
    obj = _decode_cursor_object(cursor)
    if set(obj) != {"after", "path", "tool", "v"} or obj["v"] != 1 or obj["tool"] != "list_directory":
        raise ToolArgumentError(ArgumentRejection.CURSOR_INVALID, "cursor is invalid")
    if obj["path"] != path:
        raise ToolArgumentError(ArgumentRejection.CURSOR_MISMATCH, "cursor does not belong to this listing")
    try:
        after = _b64decode(obj["after"])
    except ValueError:
        raise ToolArgumentError(ArgumentRejection.CURSOR_INVALID, "cursor is invalid") from None
    if (
        not 1 <= len(after) <= MAX_LIST_AFTER_BYTES
        or b"/" in after
        or b"\x00" in after
        or after in (b".", b"..")
    ):
        raise ToolArgumentError(ArgumentRejection.CURSOR_INVALID, "cursor is invalid")
    return after


def encode_search_cursor(nonce: str, page: int) -> str:
    return _b64encode(canonical_json_bytes({"chain": nonce, "page": page, "tool": "search_text", "v": 1}))


def decode_search_cursor(cursor: object) -> tuple[str, int]:
    """Static shape check only; whether the cursor is the current one of a
    live chain is decided by `repo_tools.SearchChains`."""
    obj = _decode_cursor_object(cursor)
    if (
        set(obj) != {"chain", "page", "tool", "v"}
        or obj["v"] != 1
        or obj["tool"] != "search_text"
        or not isinstance(obj["chain"], str)
        or not _NONCE_RE.fullmatch(obj["chain"])
        or isinstance(obj["page"], bool)
        or not isinstance(obj["page"], int)
        or obj["page"] < 1
    ):
        raise ToolArgumentError(ArgumentRejection.CURSOR_INVALID, "cursor is invalid")
    return obj["chain"], obj["page"]


# --- Argument dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class ListDirectoryArgs:
    path: str = "."
    cursor: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        check_relative_path(self.path, allow_root=True)
        _require_int(self.limit, "limit", 1, LIST_LIMIT_MAX)
        if self.cursor is not None:
            decode_list_cursor(self.cursor, self.path)


@dataclass(frozen=True)
class ReadFileArgs:
    path: str
    start_line: int = 1
    end_line: int | None = None

    def __post_init__(self) -> None:
        check_relative_path(self.path, allow_root=False)
        _require_int(self.start_line, "start_line", 1, 10**9)
        if self.end_line is not None:
            _require_int(
                self.end_line, "end_line", self.start_line, self.start_line + READ_MAX_LINES_PER_CALL - 1
            )

    @property
    def last_line(self) -> int:
        return self.end_line if self.end_line is not None else self.start_line + READ_MAX_LINES_PER_CALL - 1


@dataclass(frozen=True)
class SearchTextArgs:
    query: str
    path: str = "."
    cursor: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        _require_str(self.query, "query")
        _require_chars(self.query, "query", MAX_QUERY_CHARS, allow_empty=False)
        # Every C0/C1 control except tab (ledger D4b, J4): canonical JSON
        # would escape it, but it stays part of the semantic query, which a
        # later audit or report layer could decode or render.
        if any(_is_control(ch) and ch != "\t" for ch in self.query):
            raise ToolArgumentError(
                ArgumentRejection.QUERY_CONTROL_CHARACTER, "query must not contain control characters other than tab"
            )
        check_relative_path(self.path, allow_root=True)
        _require_int(self.limit, "limit", 1, SEARCH_LIMIT_MAX)
        if self.cursor is not None:
            decode_search_cursor(self.cursor)

    @property
    def chain_key(self) -> str:
        """Normalized arguments a chain is bound to (everything but the cursor)."""
        return canonical_json({"limit": self.limit, "path": self.path, "query": self.query})


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


@dataclass(frozen=True)
class ProposedChange:
    path: str
    expected_text: str
    new_text: str

    def __post_init__(self) -> None:
        check_relative_path(self.path, allow_root=False)
        for field, value in (("expected_text", self.expected_text), ("new_text", self.new_text)):
            _require_str(value, field)
            if _utf8_len(value) > MAX_CHANGE_TEXT_BYTES:
                raise ToolArgumentError(ArgumentRejection.TOO_LONG, f"{field} is too long")
        if not self.expected_text:
            raise ToolArgumentError(ArgumentRejection.EMPTY, "expected_text must be nonempty")
        if self.expected_text == self.new_text:
            raise ToolArgumentError(ArgumentRejection.CHANGE_UNCHANGED, "new_text must differ from expected_text")


@dataclass(frozen=True)
class ProposePlanArgs:
    problem_hypothesis: str
    evidence_refs: tuple[str, ...]
    change: ProposedChange
    verification_intent: str
    risk_notes: str = ""

    def __post_init__(self) -> None:
        _require_chars(
            _require_str(self.problem_hypothesis, "problem_hypothesis"),
            "problem_hypothesis",
            MAX_HYPOTHESIS_CHARS,
            allow_empty=False,
        )
        _require_chars(
            _require_str(self.verification_intent, "verification_intent"),
            "verification_intent",
            MAX_VERIFICATION_INTENT_CHARS,
            allow_empty=False,
        )
        _require_chars(_require_str(self.risk_notes, "risk_notes"), "risk_notes", MAX_RISK_NOTES_CHARS, allow_empty=True)
        if not isinstance(self.change, ProposedChange):
            raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, "change must be a proposed change")
        if not isinstance(self.evidence_refs, tuple) or not 1 <= len(self.evidence_refs) <= MAX_EVIDENCE_REFS:
            raise ToolArgumentError(ArgumentRejection.EVIDENCE_REF_INVALID, "evidence_refs must hold 1 to 20 ids")
        for ref in self.evidence_refs:
            if not isinstance(ref, str) or not _EVIDENCE_REF_RE.fullmatch(ref):
                raise ToolArgumentError(ArgumentRejection.EVIDENCE_REF_INVALID, "evidence_refs holds an invalid id")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ToolArgumentError(ArgumentRejection.EVIDENCE_REF_INVALID, "evidence_refs holds a duplicate id")


ToolArgs = ListDirectoryArgs | ReadFileArgs | SearchTextArgs | ProposePlanArgs


# --- Strict parsing ----------------------------------------------------------


class _Duplicate(Exception):
    pass


class _NonFinite(Exception):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate()
        result[key] = value
    return result


def _reject_constant(_token: str) -> float:
    raise _NonFinite()


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise _NonFinite()
    return value


def _strict_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant, parse_float=_parse_float)
    except _Duplicate:
        raise ToolArgumentError(ArgumentRejection.DUPLICATE_KEY, "arguments contain a duplicate key") from None
    except _NonFinite:
        raise ToolArgumentError(ArgumentRejection.NON_FINITE_NUMBER, "arguments contain a non-finite number") from None
    except (ValueError, RecursionError):
        raise ToolArgumentError(ArgumentRejection.INVALID_JSON, "arguments are not valid JSON") from None


def _check_surrogates(value: Any) -> None:
    if isinstance(value, str):
        if _has_surrogate(value):
            raise ToolArgumentError(ArgumentRejection.LONE_SURROGATE, "arguments contain a lone surrogate")
    elif isinstance(value, dict):
        for key, sub in value.items():
            _check_surrogates(key)
            _check_surrogates(sub)
    elif isinstance(value, list):
        for sub in value:
            _check_surrogates(sub)


def _fields(obj: dict[str, Any], required: set[str], optional: set[str]) -> dict[str, Any]:
    unknown = set(obj) - required - optional
    if unknown:
        raise ToolArgumentError(ArgumentRejection.UNKNOWN_KEY, "arguments contain an unknown key")
    if not required <= set(obj):
        raise ToolArgumentError(ArgumentRejection.MISSING_KEY, "arguments are missing a required key")
    if any(value is None for value in obj.values()):
        raise ToolArgumentError(ArgumentRejection.NULL_VALUE, "arguments must omit absent keys, not send null")
    return obj


_ARGUMENT_BYTE_LIMITS = {
    ToolName.LIST_DIRECTORY: MAX_READ_TOOL_ARGUMENT_BYTES,
    ToolName.READ_FILE: MAX_READ_TOOL_ARGUMENT_BYTES,
    ToolName.SEARCH_TEXT: MAX_READ_TOOL_ARGUMENT_BYTES,
    ToolName.PROPOSE_PLAN: MAX_PROPOSE_PLAN_ARGUMENT_BYTES,
}


def parse_tool_arguments(tool: ToolName, raw: bytes) -> ToolArgs:
    """Strictly parse model-supplied argument bytes for `tool`.

    Order: type and byte limit on the raw bytes, strict UTF-8, strict JSON
    (duplicate keys, NaN/Infinity and non-finite numbers rejected), object
    shape with exact keys and no explicit null, then the dataclass's own
    validation. Whitespace and key order are free."""
    if not isinstance(raw, bytes):
        raise ToolArgumentError(ArgumentRejection.NOT_BYTES, "arguments must be bytes")
    limit = _ARGUMENT_BYTE_LIMITS.get(tool)
    if limit is None:
        raise ToolArgumentError(ArgumentRejection.NOT_MODEL_CALLABLE, "tool is not model-callable")
    if len(raw) > limit:
        raise ToolArgumentError(ArgumentRejection.OVERSIZED, "arguments exceed the byte limit")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ToolArgumentError(ArgumentRejection.INVALID_UTF8, "arguments are not valid UTF-8") from None
    obj = _strict_loads(text)
    if not isinstance(obj, dict):
        raise ToolArgumentError(ArgumentRejection.NOT_AN_OBJECT, "arguments must be a JSON object")
    _check_surrogates(obj)
    if tool is ToolName.LIST_DIRECTORY:
        f = _fields(obj, set(), {"path", "cursor", "limit"})
        return ListDirectoryArgs(**f)
    if tool is ToolName.READ_FILE:
        f = _fields(obj, {"path"}, {"start_line", "end_line"})
        return ReadFileArgs(**f)
    if tool is ToolName.SEARCH_TEXT:
        f = _fields(obj, {"query"}, {"path", "cursor", "limit"})
        return SearchTextArgs(**f)
    f = _fields(obj, {"problem_hypothesis", "evidence_refs", "change", "verification_intent"}, {"risk_notes"})
    change = f["change"]
    if not isinstance(change, dict):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, "change must be an object")
    _fields(change, {"path", "expected_text", "new_text"}, set())
    refs = f["evidence_refs"]
    if not isinstance(refs, list):
        raise ToolArgumentError(ArgumentRejection.WRONG_TYPE, "evidence_refs must be a list")
    return ProposePlanArgs(
        problem_hypothesis=f["problem_hypothesis"],
        evidence_refs=tuple(refs),
        change=ProposedChange(**change),
        verification_intent=f["verification_intent"],
        risk_notes=f.get("risk_notes", ""),
    )


@dataclass(frozen=True)
class PatchOperation:
    """Replace exactly one occurrence of `expected_text` with `new_text`
    in the file at `relative_path` (relative to the worktree root).

    Deliberately unvalidated: `patch.GitPatchApplier` performs its own
    defence-in-depth checks. Lives here, re-exported by `patch`, so the
    controller can build one from an approved `ProposedChange` without an
    import cycle (`patch` imports `controller`)."""

    relative_path: str
    expected_text: str
    new_text: str

    @classmethod
    def from_change(cls, change: ProposedChange) -> PatchOperation:
        return cls(change.path, change.expected_text, change.new_text)


# --- Redaction and digests ------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanDigests:
    plan_digest: str
    change_digest: str


def compute_digests(args: ProposePlanArgs) -> PlanDigests:
    """The only plan/change digest implementation (revision 4 §6.6)."""
    if not isinstance(args, ProposePlanArgs):
        raise TypeError("compute_digests requires ProposePlanArgs")
    change = {"path": args.change.path, "expected_text": args.change.expected_text, "new_text": args.change.new_text}
    change_digest = hashlib.sha256(canonical_json_bytes({"schema": "codeagent.change.v1", **change})).hexdigest()
    plan_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "codeagent.plan.v1",
                "change_digest": change_digest,
                "change": change,
                "evidence_refs": list(args.evidence_refs),
                "problem_hypothesis": args.problem_hypothesis,
                "risk_notes": args.risk_notes,
                "verification_intent": args.verification_intent,
            }
        )
    ).hexdigest()
    return PlanDigests(plan_digest=plan_digest, change_digest=change_digest)


def redact_arguments(args: ToolArgs) -> str:
    """Canonical `ToolRequested.redacted_arguments_json` for valid
    arguments. Never contains `expected_text`, `new_text` or plan prose."""
    if isinstance(args, ListDirectoryArgs):
        obj: dict[str, Any] = {"path": args.path, "limit": args.limit}
        if args.cursor is not None:
            obj["cursor"] = args.cursor
    elif isinstance(args, ReadFileArgs):
        obj = {"path": args.path, "start_line": args.start_line}
        if args.end_line is not None:
            obj["end_line"] = args.end_line
    elif isinstance(args, SearchTextArgs):
        obj = {"query": args.query, "path": args.path, "limit": args.limit}
        if args.cursor is not None:
            obj["cursor"] = args.cursor
    elif isinstance(args, ProposePlanArgs):
        digests = compute_digests(args)
        obj = {
            "change": {
                "path": args.change.path,
                "expected_bytes": _utf8_len(args.change.expected_text),
                "expected_sha256": _sha256(args.change.expected_text),
                "new_bytes": _utf8_len(args.change.new_text),
                "new_sha256": _sha256(args.change.new_text),
            },
            "evidence_refs": list(args.evidence_refs),
            "plan_digest": digests.plan_digest,
            "change_digest": digests.change_digest,
        }
    else:
        raise TypeError("redact_arguments requires tool arguments")
    return canonical_json(obj)


def redact_invalid_arguments(reason: ArgumentRejection, raw: bytes) -> str:
    """Categorical evidence for rejected arguments: never the raw content."""
    return canonical_json(
        {"invalid": reason.value, "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest()}
    )


# --- Outcomes ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOutcome:
    """What a repository tool call produced. `payload` becomes the model's
    TOOL_RESULT item (D4b); `summary` (counts only) goes to ToolCompleted."""

    ok: bool
    payload: dict[str, Any] | None
    summary: str
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        if self.ok != (self.payload is not None) or self.ok == (self.error is not None):
            raise ValueError("ToolOutcome is ok iff it has a payload and no error")
        if len(self.summary) > MAX_SUMMARY_CHARS:
            raise ValueError("ToolOutcome summary is too long")
