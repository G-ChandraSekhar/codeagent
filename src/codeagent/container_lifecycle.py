"""Milestone 3 Slice 3B-6: dependency-light lifecycle-aware container
leaf types.

(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 6.)

Deliberately depends on nothing but the standard library, so
`codeagent.executor` can adopt lifecycle-aware deterministic container
naming, labeling, and durable transition publication without ever
importing `codeagent.lifecycle_store` (or anything else in the
persistence stack: `checkpoint_ref.py`, `_lifecycle_fs.py`,
`state_locks.py`, ...). `lifecycle_store.LifecycleContainerPublisher` is
the *only* place a `LifecycleStoreError` is ever translated into the
`ContainerPublicationError` this module defines — `executor.py` never
imports, catches, compares, or annotates with `LifecycleStoreError`/
`LifecycleStoreFailure`.

`ContainerIntent` is defined here (not in `lifecycle_store.py`) so both
the leaf producer (`executor.py`) and the persistence writer
(`lifecycle_store.py`, which re-imports it for source compatibility)
share the identical vocabulary without either depending on the other's
home module.
"""

from __future__ import annotations

import re
from enum import Enum, unique
from typing import Protocol

# ADR 0004 section 7's four required labels. Values reused verbatim from
# the constants `codeagent.reconciliation` already defined for Slice
# 3B-5's own real-Docker fixtures — this module is now their one home;
# `reconciliation.py` re-imports them for source compatibility.
CONTAINER_LABEL_SCHEMA = "codeagent.lifecycle.schema"
CONTAINER_LABEL_STATE_ROOT_ID = "codeagent.lifecycle.state-root-id"
CONTAINER_LABEL_ID = "codeagent.lifecycle.id"
CONTAINER_LABEL_ROLE = "codeagent.lifecycle.role"

# The canonical 32-lowercase-hex grammar shared by `lifecycle_id` and
# `state_root_id` (`checkpoint_ref.LIFECYCLE_ID_RE` /
# `_lifecycle_fs.validate_hex32`) — defined here, independently, as this
# leaf module's own copy, since importing either of those would pull the
# persistence stack into `executor.py`'s dependency graph. This is the
# fourth independent definition of this exact regex in the codebase
# (`_lifecycle_fs.py`, `checkpoint_ref.py`, and now here), accepted as
# unavoidable given the layering constraint.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


@unique
class ContainerRole(str, Enum):
    BASELINE = "baseline"
    VERIFICATION = "verification"


@unique
class ContainerIntent(str, Enum):
    ABSENT = "absent"
    CREATING = "creating"
    PRESENT = "present"
    REMOVING = "removing"


def is_valid_hex32(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX32_RE.fullmatch(value))


def deterministic_container_name(*, role: ContainerRole, lifecycle_id: str) -> str:
    """`codeagent-<role>-<lifecycle_id>` — stable across sequential
    attempts within the same role, so `verification` can be reused
    across attempts instead of minting a fresh UUID-suffixed name each
    time (the legacy, still-unchanged `lifecycle_context is None`
    naming scheme)."""
    if not is_valid_hex32(lifecycle_id):
        raise ValueError(f"lifecycle_id must be exactly 32 lowercase hex characters, got {lifecycle_id!r}")
    return f"codeagent-{role.value}-{lifecycle_id}"


def required_labels(*, state_root_id: str, lifecycle_id: str, role: ContainerRole) -> dict[str, str]:
    """The exact four labels a lifecycle-aware `docker create` must set
    — and no `attempt` label. Extra, unrelated, or image-provided labels
    are never asserted here and never defeat ownership proof (see
    `labels_match`)."""
    return {
        CONTAINER_LABEL_SCHEMA: "1",
        CONTAINER_LABEL_STATE_ROOT_ID: state_root_id,
        CONTAINER_LABEL_ID: lifecycle_id,
        CONTAINER_LABEL_ROLE: role.value,
    }


def labels_match(labels: dict[str, str], *, state_root_id: str, lifecycle_id: str, role: ContainerRole) -> bool:
    """ADR 0004 section 7's exactly-four-required-labels ownership
    check. Extra, unrecognized labels (including ones an image itself
    bakes in) are always ignored — every one of the four required
    labels must be present with the exact expected value, but this is
    presence/value proof, never an exclusivity claim."""
    return (
        labels.get(CONTAINER_LABEL_SCHEMA) == "1"
        and labels.get(CONTAINER_LABEL_STATE_ROOT_ID) == state_root_id
        and labels.get(CONTAINER_LABEL_ID) == lifecycle_id
        and labels.get(CONTAINER_LABEL_ROLE) == role.value
    )


class ContainerTransitionPublisher(Protocol):
    """Structural boundary `executor.DockerVerifierLifecycleContext`
    depends on — implemented by `lifecycle_store.
    LifecycleContainerPublisher`, but this leaf module has no import of
    that class or its module. Matching the existing
    `checkpoint_session.CheckpointTransitionPublisher` precedent, this
    Protocol is not runtime-checked (`@runtime_checkable`/`isinstance`)
    anywhere — fail-on-first-use duck typing only.

    `lifecycle_id`/`state_root_id` (Slice 3B-6 correction pass) are
    immutable, read-only identity properties every implementation must
    expose — the *single* source of truth for which lifecycle this
    publisher durably records transitions for. Binding
    `DockerVerifierLifecycleContext`'s Docker-side identity (names,
    labels) to this same source, rather than accepting it as a second,
    independently supplied value, makes the mismatch this correction
    closes structurally unrepresentable: a publisher recording
    lifecycle A's transitions can never be paired with Docker names/
    labels for a different lifecycle B."""

    @property
    def lifecycle_id(self) -> str: ...

    @property
    def state_root_id(self) -> str: ...

    def publish(self, *, role: ContainerRole, intent: ContainerIntent, id: str | None) -> None: ...


@unique
class ContainerPublicationFailure(str, Enum):
    """Producer-facing translation of every `lifecycle_store.
    LifecycleStoreFailure` reason reachable (or not) from
    `record_container_transition`. `UNCLASSIFIED` is reserved
    exclusively for reasons confirmed unreachable from that call path
    today (`LIFECYCLE_ID_COLLISION`, `RECONCILIATION_BLOCKED` — both
    `prepare_lifecycle()`-only) — never a silent default for a reason
    nobody has classified."""

    NOT_INSTALLED = "not_installed"
    DURABILITY_UNCONFIRMED = "durability_unconfirmed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    STALE_EXPECTATION = "stale_expectation"
    WRONG_LOCK_SCOPE = "wrong_lock_scope"
    ILLEGAL_TRANSITION = "illegal_transition"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    SCHEMA_INVALID = "schema_invalid"
    OVERSIZED = "oversized"
    UNCLASSIFIED = "unclassified"


class ContainerPublicationError(Exception):
    """Raised only by `lifecycle_store.LifecycleContainerPublisher.
    publish()` — the one place a `LifecycleStoreError` is ever caught
    and translated (`raise ContainerPublicationError(...) from exc`,
    preserving the original as `__cause__`). `message` is fixed,
    sanitized categorical text only — never a raw persistence-layer
    message, path, or traceback. `executor.py` catches only this
    specific type, never `Exception`/`BaseException`: a genuine
    programming bug or `KeyboardInterrupt`/cancellation propagates
    unchanged."""

    def __init__(self, reason: ContainerPublicationFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
