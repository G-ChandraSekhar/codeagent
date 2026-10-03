"""Milestone 3 worktree-attribution substrate: dependency-light
lifecycle-aware worktree leaf types.

(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 10.)

Deliberately depends on nothing but the standard library, mirroring
`container_lifecycle.py`/`lifecycle_owner.py` exactly, so a future
`workspace.py` integration could adopt lifecycle-aware worktree
publication without ever importing `codeagent.lifecycle_store` (or
anything else in the persistence stack). `lifecycle_store.
LifecycleWorktreePublisher` is the *only* place a `LifecycleStoreError`
is ever translated into the `WorktreePublicationError` this module
defines.

`WorktreeIntent` is defined here (not in `lifecycle_store.py`) so both a
future leaf producer and the persistence writer (`lifecycle_store.py`,
which re-imports it for source compatibility) share the identical
vocabulary without either depending on the other's home module — the
same move `container_lifecycle.py` made for `ContainerIntent` in Slice
3B-6.

`WorktreeTransition` is the one write-ahead worktree record (ADR 0004
Amendment 10's exact persisted-combination table), reused directly as
`LifecycleProjection.worktree`'s own field type — the same choice
`checkpoint_session.CheckpointTransition` already made for
`checkpoint_ref`, and for the identical reason: the worktree's
combination rule (every non-`absent` intent requires a non-null
`expected_head`; `absent` requires a null one) is a real cross-field
invariant, not a simple per-intent-nullable value like a container's
`id`. Centralizing it in one validated value object, reused by the
schema validator, the writer, and the publisher, avoids duplicating that
combination check in three places.

**`expected_head` is the immutable materialization/origin commit for
one worktree incarnation** (ADR 0004 Amendment 10) — the commit the
worktree was created from, fixed at `creating`, retained unchanged
through `present` and `disposing`, cleared only at `disposing->absent`.
It is **never** republished on checkpoint advances and is **not**
intended to equal the worktree's continuously advancing live `HEAD` —
see the module-level `WorktreeTransition` docstring for the full
rationale, including why a live-`HEAD` equality rule would be wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol


@unique
class WorktreeIntent(str, Enum):
    ABSENT = "absent"
    CREATING = "creating"
    PRESENT = "present"
    DISPOSING = "disposing"


# Structural shape only: the two object-id lengths Git defines. The
# *repository's* actual format is enforced separately, by
# `lifecycle_store._is_valid_oid_for_format` against the real
# `object_format`, so a `WorktreeTransition` can still be constructed
# and validated standalone (in a test, or by a future reader of a
# persisted record) without a live repository — the identical split
# `checkpoint_session.CheckpointTransition`/`_OID_RE` already uses. This
# is a second, independent definition of the same regex (the first is
# `checkpoint_session._OID_RE`), accepted as unavoidable given the
# layering constraint that keeps this module stdlib-only and therefore
# unable to import `checkpoint_session.py`.
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _is_all_zero_oid(value: str) -> bool:
    return set(value) == {"0"}


def _require_oid_shape(name: str, value: str) -> None:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise ValueError(f"{name} must be exactly 40 or 64 lowercase hexadecimal characters")
    if _is_all_zero_oid(value):
        raise ValueError(f"{name} must not be the all-zero object id")


@dataclass(frozen=True)
class WorktreeTransition:
    """One worktree-attribution record (ADR 0004 Amendment 10).

    `expected_head` is **not** a representation of the worktree's live,
    continuously-advancing `HEAD` — it is the immutable commit the
    worktree was materialized from, fixed once at `creating` and
    retained unchanged through `present`/`disposing`:

        intent      expected_head
        absent      None
        creating    <materialization commit>
        present     <same commit, unchanged>
        disposing   <same commit, unchanged>

    This is a deliberate correction of an earlier, rejected design: a
    worktree's real `HEAD` moves on every patch application (each
    checkpoint commit lands directly in the worktree, via
    `codeagent.patch.GitPatchApplier`, *before*
    `codeagent.checkpoint_session.CheckpointSession.advance()` is ever
    called) — so during the entirely ordinary window between that commit
    and the checkpoint-ref CAS resolving, `checkpoint_ref.intent ==
    ADVANCING` with `accepted_sha == A`, `expected_old_sha == A`,
    `proposed_new_sha == B`, while the real worktree `HEAD` is *already*
    `B`. A rule requiring `HEAD == checkpoint_ref.accepted_sha` (or any
    other live-`HEAD` equality rule) would misclassify this routine,
    successful-path window as a conflict. This field therefore never
    tracks live `HEAD` and is never compared against it — whatever a
    worktree's *current* trusted `HEAD` should be remains entirely
    `checkpoint_ref`'s and ADR 0003's own concern (the entry/resume gate
    checks `HEAD` against `checkpoint_ref.accepted_sha`/
    `workspace.initial_commit`, not against this field), unaffected by
    this module.

    Ownership/removal eligibility (a later slice, not this one) follows
    ADR 0004 section 8's own already-accepted, non-`HEAD`-based rule:
    deterministic contained path, safe filesystem identity/type, exact
    `git worktree list --porcelain` registration, and persisted
    non-`absent` intent. A live `HEAD` mismatch alone is not, and must
    never become, an ownership-conflict or removal-refusal condition.

    The legal combinations are exactly ADR 0004 Amendment 10's table,
    and every other combination is refused at construction. Only the
    generic 40-or-64-lowercase-hex/non-zero shape is checked here (this
    module has no live repository to know which length is correct) —
    the repository's actual object-format-aware exact length is
    enforced separately by `lifecycle_store._is_valid_oid_for_format`.
    """

    intent: WorktreeIntent
    expected_head: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, WorktreeIntent):
            raise ValueError(f"intent must be a WorktreeIntent, got {self.intent!r}")
        if self.intent is WorktreeIntent.ABSENT:
            if self.expected_head is not None:
                raise ValueError("an absent worktree transition must carry no expected_head")
        else:
            if self.expected_head is None:
                raise ValueError(f"intent {self.intent.value!r} requires a non-null expected_head")
            _require_oid_shape("expected_head", self.expected_head)


ABSENT_WORKTREE_TRANSITION = WorktreeTransition(intent=WorktreeIntent.ABSENT)


class WorktreeTransitionPublisher(Protocol):
    """Structural boundary a future `workspace.GitWorktree` integration
    would depend on — implemented by `lifecycle_store.
    LifecycleWorktreePublisher`, but this leaf module has no import of
    that class or its module. Matching the existing
    `container_lifecycle.ContainerTransitionPublisher`/
    `checkpoint_session.CheckpointTransitionPublisher` precedent, this
    Protocol is not runtime-checked (`@runtime_checkable`/`isinstance`)
    anywhere — fail-on-first-use duck typing only.

    `lifecycle_id`/`state_root_id` are immutable, read-only identity
    properties every implementation must expose, mirroring
    `ContainerTransitionPublisher`'s own correction-pass rationale: a
    future integration's Git-side identity should derive from this same
    source rather than a second, independently supplied value that
    could disagree with it.

    `repo_key` (ADR 0004 Amendment 11) was added after this Protocol
    first shipped. Protocols are not runtime-checked, so existing code
    imports and runs unchanged, but this *is* a contract change: every
    conforming implementation must now also expose `repo_key`. It is
    required because the deterministic worktree path contains
    `repo_key` — a caller reserving under the wrong repository key with
    the correct `lifecycle_id`/`state_root_id` would otherwise go
    undetected. `workspace.GitWorktree` binds all three against its
    reservation before any Git call; that integration is an optional,
    unwired producer seam — no production composition path supplies a
    publisher.

    `publish()` takes the single, pre-validated `WorktreeTransition`
    value object directly — mirroring
    `CheckpointTransitionPublisher.publish(transition)`, not
    `ContainerTransitionPublisher.publish(*, role, intent, id)` — since
    the worktree, like the checkpoint ref and unlike a container, has
    exactly one combination-validated record, not one record per role.
    """

    @property
    def repo_key(self) -> str: ...

    @property
    def lifecycle_id(self) -> str: ...

    @property
    def state_root_id(self) -> str: ...

    def publish(self, transition: WorktreeTransition) -> None: ...


@unique
class WorktreePublicationFailure(str, Enum):
    """Producer-facing translation of every `lifecycle_store.
    LifecycleStoreFailure` reason reachable (or not) from
    `record_worktree_transition`. `UNCLASSIFIED` is reserved
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


class WorktreePublicationError(Exception):
    """The public exception type a `WorktreeTransitionPublisher`
    implementation raises to report a durable lifecycle-projection
    publication failure (ADR 0004 Amendment 10). In production,
    `lifecycle_store.LifecycleWorktreePublisher.publish()` is the sole
    translation boundary that raises it -- catching `LifecycleStoreError`
    and re-raising this instead (`raise WorktreePublicationError(...)
    from exc`, preserving the original as `__cause__`) -- but this is a
    public type, importable from this module: any conforming
    `WorktreeTransitionPublisher` implementation (a test double, or a
    future alternate production publisher) may raise it directly too,
    and any caller handles either origin identically. `message` is
    fixed, sanitized categorical text only — never a raw persistence-
    layer message, path, or traceback. A future integration catching
    this should catch only this specific type, never `Exception`/
    `BaseException`: a genuine programming bug or `KeyboardInterrupt`/
    cancellation must propagate unchanged."""

    def __init__(self, reason: WorktreePublicationFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
