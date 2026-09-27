"""Tests for `codeagent.container_lifecycle` (Milestone 3 Slice 3B-6,
ADR 0004 Amendment 6)."""

from __future__ import annotations

import pytest

from codeagent import container_lifecycle as cl


def test_is_valid_hex32_accepts_exact_grammar():
    assert cl.is_valid_hex32("a" * 32)
    assert not cl.is_valid_hex32("A" * 32)  # uppercase
    assert not cl.is_valid_hex32("a" * 31)  # too short
    assert not cl.is_valid_hex32("a" * 33)  # too long
    assert not cl.is_valid_hex32("g" * 32)  # non-hex
    assert not cl.is_valid_hex32("")
    assert not cl.is_valid_hex32(None)  # type: ignore[arg-type]


def test_deterministic_container_name_is_stable_per_role():
    lifecycle_id = "a" * 32
    name1 = cl.deterministic_container_name(role=cl.ContainerRole.VERIFICATION, lifecycle_id=lifecycle_id)
    name2 = cl.deterministic_container_name(role=cl.ContainerRole.VERIFICATION, lifecycle_id=lifecycle_id)
    assert name1 == name2 == f"codeagent-verification-{lifecycle_id}"

    baseline_name = cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=lifecycle_id)
    assert baseline_name == f"codeagent-baseline-{lifecycle_id}"
    assert baseline_name != name1


def test_deterministic_container_name_rejects_malformed_lifecycle_id():
    with pytest.raises(ValueError):
        cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id="not-hex32")


def test_required_labels_sets_exactly_four_and_no_attempt_label():
    labels = cl.required_labels(state_root_id="b" * 32, lifecycle_id="c" * 32, role=cl.ContainerRole.BASELINE)
    assert set(labels.keys()) == {
        cl.CONTAINER_LABEL_SCHEMA,
        cl.CONTAINER_LABEL_STATE_ROOT_ID,
        cl.CONTAINER_LABEL_ID,
        cl.CONTAINER_LABEL_ROLE,
    }
    assert "attempt" not in labels
    assert not any("attempt" in k for k in labels)


def test_labels_match_requires_all_four_exact_values():
    state_root_id = "d" * 32
    lifecycle_id = "e" * 32
    role = cl.ContainerRole.VERIFICATION
    labels = cl.required_labels(state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)
    assert cl.labels_match(labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)

    for missing_key in labels:
        degraded = {k: v for k, v in labels.items() if k != missing_key}
        assert not cl.labels_match(degraded, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)

    wrong_role = {**labels, cl.CONTAINER_LABEL_ROLE: "baseline"}
    assert not cl.labels_match(wrong_role, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)


def test_labels_match_ignores_extra_image_provided_labels():
    """Extra/unrelated labels (e.g. baked into an image's own Dockerfile)
    must never defeat ownership proof — only the four required labels'
    presence/value is checked, never exclusivity."""
    state_root_id = "f" * 32
    lifecycle_id = "1" * 32
    role = cl.ContainerRole.BASELINE
    labels = {
        **cl.required_labels(state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role),
        "org.opencontainers.image.version": "3.12-slim",
        "maintainer": "someone@example.com",
    }
    assert cl.labels_match(labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)


def test_container_publication_error_carries_reason_and_message():
    exc = cl.ContainerPublicationError(cl.ContainerPublicationFailure.ILLEGAL_TRANSITION, "boom")
    assert exc.reason is cl.ContainerPublicationFailure.ILLEGAL_TRANSITION
    assert exc.message == "boom"
    assert str(exc) == "boom"
