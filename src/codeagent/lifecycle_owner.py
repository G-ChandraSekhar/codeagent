"""Dependency-light owner-state lifecycle-publication boundary (Milestone
3 Slice 3C-1, ADR 0004 Amendment 8).

Mirrors `checkpoint_session.CheckpointTransitionPublisher`/
`CheckpointPublicationFailure` and
`container_lifecycle.ContainerTransitionPublisher`/
`ContainerPublicationFailure` exactly: a stdlib-only leaf module with no
import of `lifecycle_store.py` or `controller.py`, so `RunController`
can depend on this vocabulary without ever touching the persistence
stack, and `lifecycle_store.py` can implement it without importing
`controller.py`.
"""

from __future__ import annotations

from enum import Enum, unique


@unique
class OwnerStatePublicationFailure(str, Enum):
    """Producer-facing translation of every `lifecycle_store.
    LifecycleStoreFailure` reason reachable (or not) from
    `LifecycleProjectionCursor.advance_state()`. Mirrors
    `container_lifecycle.ContainerPublicationFailure` and
    `checkpoint_session.CheckpointPublicationFailure` exactly -- same 10
    members, same reachability analysis. `UNCLASSIFIED` is reserved
    exclusively for reasons confirmed unreachable from this call path
    (`LIFECYCLE_ID_COLLISION`, `RECONCILIATION_BLOCKED` -- both
    `prepare_lifecycle()`-only) -- never a silent default for a reason
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


class OwnerStatePublicationError(Exception):
    """The public exception type a `controller.LifecycleOwnerPublisher`
    implementation raises to report a durable owner-state lifecycle-
    publication failure (Slice 3C-1, ADR 0004 Amendment 8). In
    production, `lifecycle_store.LifecycleOwnerStatePublisher` is the
    sole translation boundary that raises it -- catching
    `LifecycleStoreError` and re-raising this instead (`raise
    OwnerStatePublicationError(...) from exc`, preserving the original
    as `__cause__`) -- but this is a public type, importable from this
    module: any conforming `LifecycleOwnerPublisher` implementation (a
    test double, such as `tests.support.fakes.
    FakeLifecycleOwnerPublisher`, or a future alternate production
    publisher) may raise it directly too, and `RunController` handles
    either origin identically. `message` is fixed, sanitized
    categorical text only -- never a raw persistence-layer message,
    path, or traceback. `RunController` catches only this specific
    type, never `Exception`/`BaseException`: a genuine programming bug
    or `KeyboardInterrupt`/cancellation propagates unchanged."""

    def __init__(self, reason: OwnerStatePublicationFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
