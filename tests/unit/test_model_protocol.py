"""Ledger D5-A (ADR 0007 Amendment 1 §A1.6): the PATCH_FAILED feedback kind
and its bounded message."""

from __future__ import annotations

from codeagent.model_protocol import (
    DROPPABLE_KINDS,
    PATCH_FAILED_ITEM_MAX_BYTES,
    PATCH_FAILED_MESSAGE_MAX_BYTES,
    VERIFICATION_ITEM_MAX_BYTES,
    FeedbackKind,
    utf8_prefix,
)


def test_patch_failed_is_never_dropped() -> None:
    assert FeedbackKind.PATCH_FAILED.value == "patch_failed"
    assert FeedbackKind.PATCH_FAILED not in DROPPABLE_KINDS
    assert DROPPABLE_KINDS == {FeedbackKind.TOOL_RESULT, FeedbackKind.TOOL_DENIED, FeedbackKind.MALFORMED_RESPONSE}


def test_patch_failed_bound_is_within_the_repair_allowance() -> None:
    assert PATCH_FAILED_MESSAGE_MAX_BYTES == 512
    assert PATCH_FAILED_ITEM_MAX_BYTES <= VERIFICATION_ITEM_MAX_BYTES


def test_utf8_prefix_cuts_on_a_character_boundary() -> None:
    assert utf8_prefix("abc", 512) == "abc"
    assert utf8_prefix("a" * 600, 512) == "a" * 512
    # A 2-byte character straddling the limit is dropped, never split.
    assert utf8_prefix("a" + "é" * 300, 512) == "a" + "é" * 255
    assert utf8_prefix("😀" * 200, 10) == "😀" * 2
    assert len(utf8_prefix("😀" * 200, 512).encode("utf-8")) <= 512


def test_utf8_prefix_sanitizes_controls_and_lone_surrogates() -> None:
    assert utf8_prefix("a\x07b\nc\td", 512) == "a?b\nc\td"
    assert utf8_prefix("x\ud800y", 512) == "x?y"
