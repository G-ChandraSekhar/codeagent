"""Ledger D4a: the neutral tool contract (`codeagent.tool_schema`).

Covers the D4 plan's R1 (byte limits), R3 (strict JSON), R4-unit
(redaction), R24-static (administrative names before any filesystem
access), R27 (bytes boundary), R30 (digest ownership), R34-unit (meaningful
notes) and R35-static (depth from the worktree root).
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from codeagent import _git_safety, events, tool_schema
from codeagent.domain import RunState, ToolName
from codeagent.tool_schema import (
    ArgumentRejection,
    ListDirectoryArgs,
    ProposedChange,
    ProposePlanArgs,
    ReadFileArgs,
    SearchTextArgs,
    ToolArgumentError,
    ToolCleanupUnconfirmedError,
    check_relative_path,
    compute_digests,
    decode_list_cursor,
    decode_search_cursor,
    encode_list_cursor,
    encode_search_cursor,
    is_meaningful_note,
    parse_tool_arguments,
    redact_arguments,
    redact_invalid_arguments,
    sanitize_recorded_text,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "codeagent"


def _reason(fn, *args, **kwargs) -> ArgumentRejection:
    with pytest.raises(ToolArgumentError) as info:
        fn(*args, **kwargs)
    return info.value.reason


def _plan_json(**overrides) -> dict:
    obj = {
        "problem_hypothesis": "retry_count is incremented before the duplicate check",
        "evidence_refs": ["tc-1-1"],
        "change": {"path": "jobs/worker.py", "expected_text": "a", "new_text": "b"},
        "verification_intent": "python3 -B -m unittest tests.test_worker",
        "risk_notes": "",
    }
    obj.update(overrides)
    return obj


# --- R27 / R1: bytes boundary and byte limits --------------------------------


def test_str_arguments_are_rejected_not_decoded():
    assert _reason(parse_tool_arguments, ToolName.READ_FILE, '{"path":"a"}') is ArgumentRejection.NOT_BYTES


@pytest.mark.parametrize("tool", [ToolName.LIST_DIRECTORY, ToolName.READ_FILE, ToolName.SEARCH_TEXT])
def test_read_tool_byte_limit_is_exact(tool):
    key = "query" if tool is ToolName.SEARCH_TEXT else "path"
    prefix, suffix = b'{"' + key.encode() + b'":"', b'"}'
    filler = tool_schema.MAX_READ_TOOL_ARGUMENT_BYTES - len(prefix) - len(suffix)
    at_limit = prefix + b"a" * filler + suffix
    assert len(at_limit) == 4096
    # Parsing may still fail on the decoded value; it must not fail on size.
    try:
        parse_tool_arguments(tool, at_limit)
    except ToolArgumentError as exc:
        assert exc.reason is not ArgumentRejection.OVERSIZED
    over = prefix + b"a" * (filler + 1) + suffix
    assert _reason(parse_tool_arguments, tool, over) is ArgumentRejection.OVERSIZED


def test_propose_plan_byte_limit_is_exact():
    base = json.dumps(_plan_json()).encode()
    pad = tool_schema.MAX_PROPOSE_PLAN_ARGUMENT_BYTES - len(base)
    at_limit = base[:-1] + b" " * pad + b"}"
    assert len(at_limit) == 917_504
    assert isinstance(parse_tool_arguments(ToolName.PROPOSE_PLAN, at_limit), ProposePlanArgs)
    over = base[:-1] + b" " * (pad + 1) + b"}"
    assert _reason(parse_tool_arguments, ToolName.PROPOSE_PLAN, over) is ArgumentRejection.OVERSIZED


def test_worst_case_escaping_is_measured_on_decoded_bytes():
    blob = "\x01" * tool_schema.MAX_CHANGE_TEXT_BYTES
    raw = json.dumps(_plan_json(change={"path": "a.py", "expected_text": "x", "new_text": blob})).encode()
    assert len(raw) > 6 * 65_536 - 1  # every control byte escaped as \u0001
    args = parse_tool_arguments(ToolName.PROPOSE_PLAN, raw)
    assert len(args.change.new_text.encode()) == 65_536
    raw_over = json.dumps(_plan_json(change={"path": "a.py", "expected_text": "x", "new_text": blob + "y"})).encode()
    assert _reason(parse_tool_arguments, ToolName.PROPOSE_PLAN, raw_over) is ArgumentRejection.TOO_LONG


def test_multibyte_text_is_measured_in_bytes_not_characters():
    text = "é" * 32_768  # 65,536 bytes
    assert isinstance(ProposedChange("a.py", "x", text), ProposedChange)
    assert _reason(ProposedChange, "a.py", "x", text + "é") is ArgumentRejection.TOO_LONG


def test_invalid_utf8_bytes_are_rejected_strictly():
    assert _reason(parse_tool_arguments, ToolName.READ_FILE, b'{"path":"\xff"}') is ArgumentRejection.INVALID_UTF8


def test_apply_patch_is_not_model_callable():
    assert _reason(parse_tool_arguments, ToolName.APPLY_PATCH, b"{}") is ArgumentRejection.NOT_MODEL_CALLABLE


def test_change_text_limit_matches_the_patch_blob_limit():
    assert tool_schema.MAX_CHANGE_TEXT_BYTES == _git_safety.MAX_PATCH_BLOB_BYTES


# --- R3: strict JSON ----------------------------------------------------------


def test_whitespace_and_key_order_are_free():
    a = parse_tool_arguments(ToolName.READ_FILE, b'{"path":"a.py","start_line":2}')
    b = parse_tool_arguments(ToolName.READ_FILE, b' {\n "start_line" : 2 ,\t"path": "a.py" } ')
    assert a == b == ReadFileArgs("a.py", 2)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b'{"path":"a","path":"b"}', ArgumentRejection.DUPLICATE_KEY),
        (b'{"path":"a","start_line":NaN}', ArgumentRejection.NON_FINITE_NUMBER),
        (b'{"path":"a","start_line":Infinity}', ArgumentRejection.NON_FINITE_NUMBER),
        (b'{"path":"a","start_line":-Infinity}', ArgumentRejection.NON_FINITE_NUMBER),
        (b'{"path":"a","start_line":1e999}', ArgumentRejection.NON_FINITE_NUMBER),
        (b'{"path":"a","start_line":true}', ArgumentRejection.WRONG_TYPE),
        (b'{"path":"a","start_line":1.0}', ArgumentRejection.WRONG_TYPE),
        (b'{"path":"a","end_line":null}', ArgumentRejection.NULL_VALUE),
        (b'{"path":"a","extra":1}', ArgumentRejection.UNKNOWN_KEY),
        (b'{"start_line":1}', ArgumentRejection.MISSING_KEY),
        (b'{"path":"\\ud800"}', ArgumentRejection.LONE_SURROGATE),
        (b'["a"]', ArgumentRejection.NOT_AN_OBJECT),
        (b'{"path":', ArgumentRejection.INVALID_JSON),
        (b'{"path":"a","start_line":0}', ArgumentRejection.OUT_OF_RANGE),
    ],
)
def test_strict_parsing_rejections(raw, reason):
    assert _reason(parse_tool_arguments, ToolName.READ_FILE, raw) is reason


def test_nested_duplicate_key_is_rejected():
    raw = b'{"problem_hypothesis":"h","evidence_refs":["tc-1-1"],"verification_intent":"v",' \
          b'"change":{"path":"a.py","path":"b.py","expected_text":"x","new_text":"y"}}'
    assert _reason(parse_tool_arguments, ToolName.PROPOSE_PLAN, raw) is ArgumentRejection.DUPLICATE_KEY


def test_read_range_bounds():
    ReadFileArgs("a.py", 5, 5 + 1999)
    assert _reason(ReadFileArgs, "a.py", 5, 5 + 2000) is ArgumentRejection.OUT_OF_RANGE
    assert _reason(ReadFileArgs, "a.py", 5, 4) is ArgumentRejection.OUT_OF_RANGE
    assert _reason(ReadFileArgs, "a.py", True) is ArgumentRejection.WRONG_TYPE


def test_search_query_rules():
    assert _reason(SearchTextArgs, "") is ArgumentRejection.EMPTY
    assert _reason(SearchTextArgs, "q" * 201) is ArgumentRejection.TOO_LONG
    assert isinstance(SearchTextArgs("q" * 200), SearchTextArgs)
    for bad in ("a\nb", "a\x00b"):
        with pytest.raises(ToolArgumentError):
            SearchTextArgs(bad)
    assert _reason(SearchTextArgs, "q", limit=101) is ArgumentRejection.OUT_OF_RANGE


# --- static path rules (R10-static, R24-static, R35-static) -------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", ArgumentRejection.PATH_EMPTY),
        ("/etc/passwd", ArgumentRejection.PATH_ABSOLUTE),
        ("../x", ArgumentRejection.PATH_DOTDOT),
        ("a/../x", ArgumentRejection.PATH_DOTDOT),
        ("a/./x", ArgumentRejection.PATH_DOT),
        ("a//x", ArgumentRejection.PATH_EMPTY_COMPONENT),
        ("a/", ArgumentRejection.PATH_EMPTY_COMPONENT),
        ("a\\x", ArgumentRejection.PATH_BACKSLASH),
        ("a\x01x", ArgumentRejection.PATH_CONTROL_CHARACTER),
        ("a\x85x", ArgumentRejection.PATH_CONTROL_CHARACTER),
        ("a" * 513, ArgumentRejection.PATH_TOO_LONG),
        ("/".join("d" * 33), ArgumentRejection.PATH_TOO_DEEP),
        (".git", ArgumentRejection.ADMINISTRATIVE_PATH),
        (".GIT", ArgumentRejection.ADMINISTRATIVE_PATH),
        (".Git/config", ArgumentRejection.ADMINISTRATIVE_PATH),
        (".gIt", ArgumentRejection.ADMINISTRATIVE_PATH),
        ("sub/.git/HEAD", ArgumentRejection.ADMINISTRATIVE_PATH),
        (".g‌it", ArgumentRejection.ADMINISTRATIVE_PATH),
        ("﻿.git", ArgumentRejection.ADMINISTRATIVE_PATH),
        ("GIT~1", ArgumentRejection.ADMINISTRATIVE_PATH),
    ],
)
def test_static_path_rejections(path, reason):
    assert _reason(check_relative_path, path, allow_root=False) is reason


def test_depth_is_counted_from_the_worktree_root():
    assert len(check_relative_path("/".join("d" * 32), allow_root=False)) == 32
    assert check_relative_path(".", allow_root=True) == ()
    assert _reason(check_relative_path, ".", allow_root=False) is ArgumentRejection.PATH_DOT


def test_lookalike_names_are_not_administrative():
    for name in (".gitignore", "git", ".git2", "x.git", ".gi"):
        assert check_relative_path(name, allow_root=False) == (name,)


def test_name_rule_hook_disables_only_the_name_rule(monkeypatch):
    monkeypatch.setattr(tool_schema, "_ADMIN_NAME_RULE_ENABLED", False)
    assert check_relative_path(".git", allow_root=False) == (".git",)
    assert _reason(check_relative_path, "../x", allow_root=False) is ArgumentRejection.PATH_DOTDOT


def test_path_messages_never_echo_control_characters():
    with pytest.raises(ToolArgumentError) as info:
        check_relative_path("a\x1b[31mx", allow_root=False)
    assert "\x1b" not in info.value.message


# --- cursors (static half) -------------------------------------------------------


def test_list_cursor_round_trips_raw_non_utf8_bytes():
    cursor = encode_list_cursor("sub", b"\xff\xfe")
    assert decode_list_cursor(cursor, "sub") == b"\xff\xfe"
    assert _reason(decode_list_cursor, cursor, "other") is ArgumentRejection.CURSOR_MISMATCH


@pytest.mark.parametrize("after", [b"a/b", b"a\x00", b".", b"..", b"", b"x" * 1025])
def test_list_cursor_rejects_bad_names(after):
    assert _reason(decode_list_cursor, encode_list_cursor(".", after), ".") is ArgumentRejection.CURSOR_INVALID


@pytest.mark.parametrize("cursor", ["", "!!!", "e30", "a", "x" * 2049])
def test_cursor_decoding_is_strict(cursor):
    assert _reason(decode_list_cursor, cursor, ".") is ArgumentRejection.CURSOR_INVALID
    assert _reason(decode_search_cursor, cursor) is ArgumentRejection.CURSOR_INVALID


def test_search_cursor_shape():
    nonce = "0123456789abcdef" * 2
    assert decode_search_cursor(encode_search_cursor(nonce, 3)) == (nonce, 3)
    tampered = tool_schema.b64url(b'{"chain":"' + nonce.upper().encode() + b'","page":1,"tool":"search_text","v":1}')
    assert _reason(decode_search_cursor, tampered) is ArgumentRejection.CURSOR_INVALID
    non_canonical = tool_schema.b64url(b'{"v":1,"tool":"search_text","page":1,"chain":"' + nonce.encode() + b'"}')
    assert _reason(decode_search_cursor, non_canonical) is ArgumentRejection.CURSOR_INVALID
    assert _reason(decode_search_cursor, encode_search_cursor(nonce, 0)) is ArgumentRejection.CURSOR_INVALID
    wrong_tool = encode_list_cursor(".", b"a")
    assert _reason(decode_search_cursor, wrong_tool) is ArgumentRejection.CURSOR_INVALID


# --- plans, digests and redaction (R2-unit, R4-unit, R30) -----------------------


def _plan(**kw) -> ProposePlanArgs:
    base = dict(
        problem_hypothesis="h",
        evidence_refs=("tc-1-1",),
        change=ProposedChange("a.py", "old", "new"),
        verification_intent="v",
        risk_notes="r",
    )
    base.update(kw)
    return ProposePlanArgs(**base)


def test_plan_validation():
    assert _reason(_plan, evidence_refs=()) is ArgumentRejection.EVIDENCE_REF_INVALID
    assert _reason(_plan, evidence_refs=("tc-1-1", "tc-1-1")) is ArgumentRejection.EVIDENCE_REF_INVALID
    assert _reason(_plan, evidence_refs=("call_abc",)) is ArgumentRejection.EVIDENCE_REF_INVALID
    assert _reason(_plan, evidence_refs=tuple(f"tc-1-{i}" for i in range(21))) is ArgumentRejection.EVIDENCE_REF_INVALID
    assert _reason(_plan, problem_hypothesis="") is ArgumentRejection.EMPTY
    assert _reason(_plan, risk_notes="r" * 1501) is ArgumentRejection.TOO_LONG
    assert _plan(risk_notes="").risk_notes == ""
    assert _reason(ProposedChange, "a.py", "same", "same") is ArgumentRejection.CHANGE_UNCHANGED
    assert _reason(ProposedChange, "a.py", "", "x") is ArgumentRejection.EMPTY
    assert _reason(ProposedChange, ".git", "a", "b") is ArgumentRejection.ADMINISTRATIVE_PATH


def test_risk_notes_is_a_single_string_like_the_event():
    assert events.PlanProposed.__dataclass_fields__["risk_notes"].type in ("str", str)
    assert isinstance(_plan().risk_notes, str)


@pytest.mark.parametrize(
    "field",
    ["problem_hypothesis", "evidence_refs", "verification_intent", "risk_notes", "path", "expected_text", "new_text"],
)
def test_every_field_changes_the_plan_digest(field):
    base = _plan()
    if field in ("path", "expected_text", "new_text"):
        change = ProposedChange(**{**base.change.__dict__, field: base.change.__dict__[field] + "2"})
        other = _plan(change=change)
    elif field == "evidence_refs":
        other = _plan(evidence_refs=("tc-1-2",))
    else:
        other = _plan(**{field: getattr(base, field) + "2"})
    a, b = compute_digests(base), compute_digests(other)
    assert a.plan_digest != b.plan_digest
    assert (a.change_digest != b.change_digest) == (field in ("path", "expected_text", "new_text"))


def test_digests_are_deterministic_hex():
    a = compute_digests(_plan())
    assert a == compute_digests(_plan())
    assert len(a.plan_digest) == len(a.change_digest) == 64


def test_digest_schema_strings_appear_only_in_tool_schema():
    for module in SRC.glob("*.py"):
        text = module.read_text()
        if module.name == "tool_schema.py":
            continue
        assert "codeagent.plan.v1" not in text and "codeagent.change.v1" not in text, module.name


def test_propose_plan_redaction_never_contains_blobs_or_prose():
    plan = _plan(
        problem_hypothesis="SENTINEL_HYPOTHESIS",
        risk_notes="SENTINEL_RISK",
        change=ProposedChange("a.py", "SENTINEL_EXPECTED", "SENTINEL_NEW"),
    )
    redacted = redact_arguments(plan)
    assert "SENTINEL" not in redacted
    obj = json.loads(redacted)
    assert obj["plan_digest"] == compute_digests(plan).plan_digest
    assert obj["change"]["expected_bytes"] == len("SENTINEL_EXPECTED")


def test_invalid_redaction_is_categorical_only():
    raw = b'{"path":"SENTINEL_RAW"'
    redacted = redact_invalid_arguments(ArgumentRejection.INVALID_JSON, raw)
    assert "SENTINEL" not in redacted
    assert json.loads(redacted) == {
        "invalid": "invalid_json",
        "raw_bytes": len(raw),
        "raw_sha256": __import__("hashlib").sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    "args",
    [
        ListDirectoryArgs(),
        ListDirectoryArgs("sub", encode_list_cursor("sub", b"x"), 7),
        ReadFileArgs("a.py", 3, 9),
        SearchTextArgs("needle é", "sub", None, 9),
        _plan(),
    ],
)
def test_redactions_are_accepted_by_tool_requested(args):
    tool = {
        ListDirectoryArgs: ToolName.LIST_DIRECTORY,
        ReadFileArgs: ToolName.READ_FILE,
        SearchTextArgs: ToolName.SEARCH_TEXT,
        ProposePlanArgs: ToolName.PROPOSE_PLAN,
    }[type(args)]
    from tests.support.fakes import SteppingClock

    events.ToolRequested(
        run_id="r",
        sequence=0,
        timestamp=SteppingClock().now(),
        state=RunState.EXPLORE,
        iteration=0,
        tool=tool,
        tool_call_id="tc-1-1",
        redacted_arguments_json=redact_arguments(args),
    )


# --- notes (R34-unit) -------------------------------------------------------------


@pytest.mark.parametrize("note", ["", "   ", "\x01\x02", "\t\n\x7f", "​‮", " \x00 ​", ""])
def test_meaningless_notes(note):
    assert not is_meaningful_note(note)


@pytest.mark.parametrize("note", ["ok\x01", "é", "?", "1", "́"])
def test_meaningful_notes(note):
    assert is_meaningful_note(note)


def test_sanitize_keeps_newline_and_tab():
    assert sanitize_recorded_text("ok\x01\n\t\x9b") == "ok?\n\t?"
    # Sanitizing a meaningless note yields '?' characters, which is exactly
    # why meaning is judged on the original text.
    assert not is_meaningful_note("\x01") and is_meaningful_note(sanitize_recorded_text("\x01"))


# --- the neutral exception and module boundary ------------------------------------


def test_cleanup_error_has_fixed_message_and_retains_identity():
    body = KeyboardInterrupt()
    failure = OSError("SENTINEL_CLOSE")
    err = ToolCleanupUnconfirmedError((failure,), body)
    assert str(err) == "repository tool descriptor cleanup unconfirmed"
    assert err.close_failures[0] is failure and err.body_failure is body
    assert "SENTINEL" not in str(err)


def test_tool_schema_imports_only_domain_and_errors():
    tree = ast.parse((SRC / "tool_schema.py").read_text())
    internal = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("codeagent"):
            internal.add(node.module)
        if isinstance(node, ast.Import):
            internal.update(a.name for a in node.names if a.name.startswith("codeagent"))
    assert internal == {"codeagent.domain", "codeagent.errors"}
