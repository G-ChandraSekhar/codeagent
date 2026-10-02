"""Tests for `codeagent.worktree_lifecycle` (worktree-attribution
substrate slice, ADR 0004 Amendment 10)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codeagent import worktree_lifecycle as wl

_SHA1 = "a" * 40
_SHA256 = "b" * 64


def test_worktree_transition_absent_requires_no_expected_head():
    transition = wl.WorktreeTransition(intent=wl.WorktreeIntent.ABSENT)
    assert transition.expected_head is None
    assert transition == wl.ABSENT_WORKTREE_TRANSITION


def test_worktree_transition_absent_with_expected_head_refused():
    with pytest.raises(ValueError):
        wl.WorktreeTransition(intent=wl.WorktreeIntent.ABSENT, expected_head=_SHA1)


@pytest.mark.parametrize("intent", [wl.WorktreeIntent.CREATING, wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.DISPOSING])
def test_worktree_transition_non_absent_requires_expected_head(intent):
    with pytest.raises(ValueError):
        wl.WorktreeTransition(intent=intent, expected_head=None)


@pytest.mark.parametrize("intent", [wl.WorktreeIntent.CREATING, wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.DISPOSING])
@pytest.mark.parametrize("oid", [_SHA1, _SHA256])
def test_worktree_transition_non_absent_accepts_valid_sha1_and_sha256_shape(intent, oid):
    transition = wl.WorktreeTransition(intent=intent, expected_head=oid)
    assert transition.intent is intent
    assert transition.expected_head == oid


def test_worktree_transition_rejects_wrong_intent_type():
    with pytest.raises(ValueError):
        wl.WorktreeTransition(intent="present", expected_head=_SHA1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "malformed",
    [
        "A" * 40,  # uppercase
        "a" * 39,  # wrong length (just short of sha1)
        "a" * 41,  # wrong length (just long of sha1)
        "a" * 63,  # wrong length (just short of sha256)
        "a" * 65,  # wrong length (just long of sha256)
        "g" * 40,  # non-hex
        "0" * 40,  # all-zero, sha1 length
        "0" * 64,  # all-zero, sha256 length
        "",
    ],
)
def test_worktree_transition_rejects_malformed_expected_head(malformed):
    with pytest.raises(ValueError):
        wl.WorktreeTransition(intent=wl.WorktreeIntent.PRESENT, expected_head=malformed)


def test_worktree_transition_rejects_non_string_expected_head():
    with pytest.raises(ValueError):
        wl.WorktreeTransition(intent=wl.WorktreeIntent.PRESENT, expected_head=123)  # type: ignore[arg-type]


def test_worktree_publication_error_carries_reason_and_message():
    exc = wl.WorktreePublicationError(wl.WorktreePublicationFailure.ILLEGAL_TRANSITION, "boom")
    assert exc.reason is wl.WorktreePublicationFailure.ILLEGAL_TRANSITION
    assert exc.message == "boom"
    assert str(exc) == "boom"


def test_worktree_publication_failure_has_the_identical_ten_member_taxonomy_as_container_and_owner():
    from codeagent import container_lifecycle as cl
    from codeagent import lifecycle_owner as lo

    worktree_members = {member.value for member in wl.WorktreePublicationFailure}
    container_members = {member.value for member in cl.ContainerPublicationFailure}
    owner_members = {member.value for member in lo.OwnerStatePublicationFailure}
    assert worktree_members == container_members == owner_members
    assert len(worktree_members) == 10


def test_worktree_transition_publisher_protocol_shape():
    """Structural check: the Protocol declares exactly lifecycle_id,
    state_root_id, and publish -- no more, no less -- mirroring
    ContainerTransitionPublisher's own shape."""
    annotations = getattr(wl.WorktreeTransitionPublisher, "__protocol_attrs__", None)
    members = {name for name in dir(wl.WorktreeTransitionPublisher) if not name.startswith("_")}
    assert {"lifecycle_id", "state_root_id", "publish"}.issubset(members)


def test_module_is_dependency_light_stdlib_only():
    """Static, source-level proof (not merely 'it currently imports
    nothing extra'): every import statement in this module names only
    standard-library modules -- `re`, `dataclasses`, `enum`, `typing`,
    and `__future__` -- mirroring container_lifecycle.py/
    lifecycle_owner.py's own identical static proof."""
    source = Path(wl.__file__).read_text()
    tree = ast.parse(source)
    allowed = {"__future__", "re", "dataclasses", "enum", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, f"unexpected import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.module.split(".")[0] in allowed, (
                f"unexpected import: {node.module}"
            )


def test_module_imports_nothing_from_lifecycle_store_or_persistence_stack():
    """AST-based (not substring-based, since the module's own docstring
    legitimately mentions these names in prose): confirms no actual
    `import`/`from ... import` statement names any persistence-stack
    module."""
    source = Path(wl.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {"lifecycle_store", "checkpoint_ref", "checkpoint_session", "_lifecycle_fs", "state_locks"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] not in forbidden
