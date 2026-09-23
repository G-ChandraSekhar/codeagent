"""Milestone 3 Slice 3A-2: durable lifecycle storage.

(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1, sections 5, 6, 12, 16.)

Implements the exact accepted 3A-2 composition, reusing Slice 3A-1's
primitives unchanged:

    check_git_preflight()
      -> discover_repository_identity_and_context(source_repo_path)
      -> resolve_state_root_path() / open_or_create_canonical_root()
      -> validate_state_root_containment() / init_state_root()
      -> acquire_repository_lock()
      -> load_or_create_repo_json()
      -> new_lifecycle_id()
      -> exclusively create repos/<repo_key>/runs/<lifecycle_id>/
      -> acquire_lifecycle_lock()
      -> atomically publish the initial PREPARING lifecycle.json

All of this happens before any Docker container, disposable worktree,
or checkpoint ref exists. The returned `LifecycleLease` retains every
acquired resource (state-root descriptor, run-directory descriptor,
repository lock, lifecycle lock) for the caller's lifetime and releases
them, on `close()`, in the exact order: lifecycle lock, run-directory
descriptor, repository lock, state-root descriptor — every step
attempted regardless of an earlier step's outcome, with a cleanup
failure dominating and chaining from whatever failure was already
active.

Slice 3B-1 (ADR 0004 Amendment 2, `reconciliation.py`) now also wires
automatic pre-run reconciliation in, immediately after
`load_or_create_repo_json` and before a lifecycle_id is ever minted —
recognizing and reconciling only the initial all-absent
`PREPARING`/`RECONCILING` shape; it never removes an external resource.

Not implemented here (later Milestone 3 slices): abandonment, the
maintenance trace's `explicit` trigger and CLI, container/worktree/
checkpoint-ref attribution or *removal* of any kind, and any
`RunController`/CLI wiring. This module is deliberately unwired. T-E1
(concurrent runs against the same repository) is therefore **still not
fully mitigated**: nothing yet calls this composition before a real run
starts, though a call that does happen now also reconciles a dead
prior run first.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, replace
from enum import Enum, unique
from pathlib import Path

from ._git_safety import ObjectFormat, check_git_preflight
from ._lifecycle_fs import (
    STORED_PATH_MAX_FS_BYTES,
    LifecycleFsError,
    LifecycleFsFailure,
    _assert_cloexec,
    _cloexec_flag,
    _nofollow_flag,
    canonical_json_dumps,
    canonical_json_loads_strict,
    close_confirmed,
    create_exclusive_directory_at,
    open_managed_directory_chain,
    publish_private_file_atomically_at,
    read_all_eintr_safe,
    resolve_state_root_path,
    validate_hex32,
)
from .checkpoint_ref import new_lifecycle_id
from .checkpoint_session import ABSENT_TRANSITION, CheckpointIntent, CheckpointTransition
from .repo_identity import discover_repository_identity_and_context, load_or_create_repo_json
from .state_locks import LockError, LockKind, LockScope, acquire_lifecycle_lock, acquire_repository_lock
from .state_root import init_state_root, open_or_create_canonical_root, validate_state_root_containment

LIFECYCLE_JSON_FILENAME = "lifecycle.json"
LIFECYCLE_LOCK_FILENAME = "lifecycle.lock"
RUNS_DIRNAME = "runs"

# ADR 0004 Amendment 1 section 4's own fixed bounds for this slice's
# schema.
LIFECYCLE_JSON_MAX_BYTES = 65536
RUN_ID_MAX_ENCODED_BYTES = 256
# Reused from the ADR's existing "sanitized failure detail" bound
# (section 4) for the two leaf string fields this slice's schema
# defines but never itself populates (`failure.detail`/`failure.phase`,
# and each `recent_failures` entry) — a deliberate, documented reuse of
# an already-accepted bound rather than an invented new one; Slice 3B,
# which actually writes these fields, should confirm this arithmetic
# once its own exact usage is fixed.
_FAILURE_DETAIL_MAX_BYTES = 512


@unique
class LifecycleState(str, Enum):
    """ADR 0004 Amendment 1 section 5. Only `PREPARING` is ever written
    by this slice; the remaining states are defined here so the schema
    this slice publishes is the complete one, not a partial stand-in
    later slices must widen."""

    PREPARING = "PREPARING"
    ACTIVE = "ACTIVE"
    CLEANING = "CLEANING"
    COMPLETE = "COMPLETE"
    RECONCILING = "RECONCILING"
    RECONCILED = "RECONCILED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


@unique
class ContainerIntent(str, Enum):
    ABSENT = "absent"
    CREATING = "creating"
    PRESENT = "present"
    REMOVING = "removing"


@unique
class WorktreeIntent(str, Enum):
    ABSENT = "absent"
    CREATING = "creating"
    PRESENT = "present"
    DISPOSING = "disposing"


@dataclass(frozen=True)
class ContainerAttribution:
    intent: ContainerIntent
    id: str | None = None


@dataclass(frozen=True)
class WorktreeAttribution:
    intent: WorktreeIntent
    expected_head: str | None = None


# `checkpoint_ref`'s attribution type is deliberately *not* redefined
# here: `checkpoint_session.CheckpointIntent`/`CheckpointTransition`
# already implement ADR 0004 section 5's exact write-ahead vocabulary
# and combination table for Milestone 3 reuse (see that module's own
# docstring), so this schema reuses them verbatim rather than
# maintaining a second copy.


@dataclass(frozen=True)
class ReconciliationSummary:
    attempts_total: int = 0
    recent_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureDetail:
    phase: str
    detail: str


@dataclass(frozen=True)
class LifecycleProjection:
    """The complete ADR 0004 Amendment 1 section 5 `lifecycle.json`
    schema (v1). This slice only ever constructs and publishes the
    initial `PREPARING` instance via `build_initial_preparing_projection`;
    every other state/intent this type can represent is defined for
    schema completeness and exercised directly by
    `tests/unit/test_lifecycle_store.py`'s schema-validation tests, not
    by any production write path in this slice."""

    schema_version: int
    lifecycle_id: str
    state_root_id: str
    repo_key: str
    run_id: str
    source_repo_path: str
    state: LifecycleState
    baseline: ContainerAttribution
    verification: ContainerAttribution
    worktree: WorktreeAttribution
    checkpoint_ref: CheckpointTransition
    failure: FailureDetail | None
    reconciliation: ReconciliationSummary


@unique
class LifecycleStoreFailure(str, Enum):
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    LIFECYCLE_ID_COLLISION = "lifecycle_id_collision"
    OVERSIZED = "oversized"
    SCHEMA_INVALID = "schema_invalid"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    # Distinct from PROJECTION_PUBLICATION_FAILED: the projection was
    # never installed at all (Codex correction — see
    # `_classify_publication_failure`).
    PROJECTION_PUBLICATION_FAILED = "projection_publication_failed"
    # Distinct from the above: `os.replace` already confirmed
    # lifecycle.json is installed; only the trailing directory-fsync's
    # durability confirmation failed. lifecycle.json is never deleted
    # or reverted for this reason.
    PROJECTION_DURABILITY_UNCONFIRMED = "projection_durability_unconfirmed"
    # Slice 3B-1 (ADR 0004 Amendment 2): the automatic pre-run
    # reconciliation pass found at least one unresolved entry and
    # refused to mint a new lifecycle_id or create a new run directory.
    RECONCILIATION_BLOCKED = "reconciliation_blocked"
    # Slice 3B-2 (ADR 0004 Amendment 3): a projection write was
    # attempted without the exact matching lifecycle lock held (never
    # a repository lock, never a mismatched repo_key/lifecycle_id, and
    # never an incomplete/closed lease).
    WRONG_LOCK_SCOPE = "wrong_lock_scope"
    # Slice 3B-2: the caller's `expected` projection no longer matches
    # the currently installed authoritative projection. Distinct
    # from ILLEGAL_TRANSITION: the requested target may be perfectly
    # legal from the *actual* current state — the caller's belief about
    # that state is simply out of date (a compare-and-swap rejection,
    # not a graph violation).
    STALE_EXPECTED_PROJECTION = "stale_expected_projection"
    # Slice 3B-2: the requested target is not a legal edge from the
    # authoritative current state — covers the owner state graph, the
    # container and checkpoint-ref transition tables (including SHA
    # continuity), and the CLEANING->COMPLETE clean-final guard.
    ILLEGAL_TRANSITION = "illegal_transition"


class LifecycleStoreError(Exception):
    """`reason` is the stable, matchable identifier. `message` is
    sanitized, fixed categorical text only — never a raw path, raw
    OSError/GitSafetyError text, or a formatted traceback."""

    def __init__(self, reason: LifecycleStoreFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def build_initial_preparing_projection(
    *,
    lifecycle_id: str,
    state_root_id: str,
    repo_key: str,
    run_id: str,
    source_repo_path: str,
) -> LifecycleProjection:
    """Build the initial `PREPARING` projection: every attribution
    field absent, no failure, zero reconciliation attempts, an empty
    recent-failures list — ADR 0004 Amendment 1 section 16 step 7's
    exact initial shape."""
    return LifecycleProjection(
        schema_version=1,
        lifecycle_id=lifecycle_id,
        state_root_id=state_root_id,
        repo_key=repo_key,
        run_id=run_id,
        source_repo_path=source_repo_path,
        state=LifecycleState.PREPARING,
        baseline=ContainerAttribution(intent=ContainerIntent.ABSENT, id=None),
        verification=ContainerAttribution(intent=ContainerIntent.ABSENT, id=None),
        worktree=WorktreeAttribution(intent=WorktreeIntent.ABSENT, expected_head=None),
        checkpoint_ref=ABSENT_TRANSITION,
        failure=None,
        reconciliation=ReconciliationSummary(attempts_total=0, recent_failures=()),
    )


def is_projection_fully_absent_shape(projection: LifecycleProjection) -> bool:
    """True only when both containers, the worktree, and the checkpoint
    ref are all at their initial absent shape and `failure` is null.

    Shared, deliberately public predicate: reconciliation.py's
    terminal/nonterminal recognition and this module's own
    `CLEANING -> COMPLETE` clean-final guard (Slice 3B-2, ADR 0004
    Amendment 3) both depend on the identical check — the one
    predicate ADR 0004's clean-final rule and I15 require."""
    return (
        projection.baseline.intent is ContainerIntent.ABSENT
        and projection.baseline.id is None
        and projection.verification.intent is ContainerIntent.ABSENT
        and projection.verification.id is None
        and projection.worktree.intent is WorktreeIntent.ABSENT
        and projection.worktree.expected_head is None
        and projection.checkpoint_ref == ABSENT_TRANSITION
        and projection.failure is None
    )


def _container_to_dict(container: ContainerAttribution) -> dict:
    return {"intent": container.intent.value, "id": container.id}


def _worktree_to_dict(worktree: WorktreeAttribution) -> dict:
    return {"intent": worktree.intent.value, "expected_head": worktree.expected_head}


def _checkpoint_ref_to_dict(ref: CheckpointTransition) -> dict:
    return {
        "intent": ref.intent.value,
        "accepted_sha": ref.accepted_sha,
        "expected_old_sha": ref.expected_old_sha,
        "proposed_new_sha": ref.proposed_new_sha,
    }


def _failure_to_dict(failure: FailureDetail | None) -> dict | None:
    if failure is None:
        return None
    return {"phase": failure.phase, "detail": failure.detail}


def _reconciliation_to_dict(summary: ReconciliationSummary) -> dict:
    return {
        "attempts_total": summary.attempts_total,
        "recent_failures": list(summary.recent_failures),
    }


def projection_to_dict(projection: LifecycleProjection) -> dict:
    """Encode a `LifecycleProjection` to the exact plain-`dict` shape
    `canonical_json_dumps` persists."""
    return {
        "schema_version": projection.schema_version,
        "lifecycle_id": projection.lifecycle_id,
        "state_root_id": projection.state_root_id,
        "repo_key": projection.repo_key,
        "run_id": projection.run_id,
        "source_repo_path": projection.source_repo_path,
        "state": projection.state.value,
        "containers": {
            "baseline": _container_to_dict(projection.baseline),
            "verification": _container_to_dict(projection.verification),
        },
        "worktree": _worktree_to_dict(projection.worktree),
        "checkpoint_ref": _checkpoint_ref_to_dict(projection.checkpoint_ref),
        "failure": _failure_to_dict(projection.failure),
        "reconciliation": _reconciliation_to_dict(projection.reconciliation),
    }


def _invalid() -> LifecycleStoreError:
    return LifecycleStoreError(LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json failed schema validation")


def _is_hex32(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_hex32(value, field_name="_")
    except LifecycleFsError:
        return False
    return True


def _is_bounded_nonempty_str(value: object, *, max_bytes: int) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8", errors="surrogateescape")) <= max_bytes


def _validate_container_shape(payload: object, *, role: str) -> None:
    if not isinstance(payload, dict) or set(payload.keys()) != {"intent", "id"}:
        raise _invalid()
    intent = payload.get("intent")
    valid_intents = {member.value for member in ContainerIntent}
    if intent not in valid_intents:
        raise _invalid()
    container_id = payload.get("id")
    if intent in (ContainerIntent.ABSENT.value, ContainerIntent.CREATING.value):
        if container_id is not None:
            raise _invalid()
    else:
        if not isinstance(container_id, str) or not container_id:
            raise _invalid()


def _validate_worktree_shape(payload: object) -> None:
    """Narrowed to the one shape ADR 0004 section 5 pins with full
    confidence for this slice: worktree `absent`, no `expected_head`.
    Unlike containers (section 7's own explicit combination table) and
    `checkpoint_ref` (section 5's own explicit table, reused directly
    from `checkpoint_session.CheckpointTransition`), the ADR gives no
    formal per-field combination table for the `creating`/`present`/
    `disposing` worktree shapes — only prose describing when they are
    considered *owned* during reconciliation (section 8), which is not
    the same as a persisted-schema validity rule. Rather than invent
    and silently ship an unreviewed rule for those shapes (including
    what `expected_head` format they would require), this validator
    refuses every non-`absent` worktree shape categorically. A later
    slice that actually produces those shapes must extend this
    validator deliberately, against its own accepted design — at which
    point `expected_head` must be validated as an exact object-format
    OID the same way `checkpoint_ref` already is below."""
    if not isinstance(payload, dict) or set(payload.keys()) != {"intent", "expected_head"}:
        raise _invalid()
    if payload.get("intent") != WorktreeIntent.ABSENT.value:
        raise _invalid()
    if payload.get("expected_head") is not None:
        raise _invalid()


def _is_valid_oid_for_format(value: object, *, object_format: ObjectFormat) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != object_format.hex_length:
        return False
    return re.fullmatch(r"[0-9a-f]+", value) is not None


def _validate_checkpoint_ref_shape(payload: object, *, object_format: ObjectFormat) -> None:
    """Reuses `checkpoint_session.CheckpointIntent`/`CheckpointTransition`
    directly rather than a second copy of ADR 0004 section 5's
    combination table: constructing a `CheckpointTransition` from the
    payload's fields exercises that module's own `__post_init__`
    validation. Each non-null SHA is additionally required to be an
    exact lowercase-hex object id of the *repository's actual* object
    format's length (40 for sha1, 64 for sha256) — `CheckpointTransition`
    on its own only checks the generic 40-or-64 structural shape (by
    design: it can be constructed standalone, without a live
    repository), so a value of the wrong length or hex-case for this
    repository's real format, or a placeholder like `"A"`, is refused
    here even though `CheckpointTransition` alone would not catch it."""
    if not isinstance(payload, dict) or set(payload.keys()) != {
        "intent",
        "accepted_sha",
        "expected_old_sha",
        "proposed_new_sha",
    }:
        raise _invalid()
    intent_value = payload.get("intent")
    valid_intents = {member.value for member in CheckpointIntent}
    if intent_value not in valid_intents:
        raise _invalid()

    accepted_sha = payload.get("accepted_sha")
    expected_old_sha = payload.get("expected_old_sha")
    proposed_new_sha = payload.get("proposed_new_sha")
    for value in (accepted_sha, expected_old_sha, proposed_new_sha):
        if value is not None and not _is_valid_oid_for_format(value, object_format=object_format):
            raise _invalid()

    try:
        CheckpointTransition(
            intent=CheckpointIntent(intent_value),
            accepted_sha=accepted_sha,
            expected_old_sha=expected_old_sha,
            proposed_new_sha=proposed_new_sha,
        )
    except ValueError:
        raise _invalid() from None


def _validate_failure_shape(payload: object) -> None:
    """Narrowed to `null`: ADR 0004 section 5 names the `{phase,
    detail}` shape but, unlike containers/checkpoint_ref, does not pin
    exact field bounds or format for a *populated* failure record, and
    this slice never writes one. Refuse any non-null value categorically
    rather than validating against an unreviewed bound — the same
    narrowing rationale as `_validate_worktree_shape` above."""
    if payload is not None:
        raise _invalid()


def _validate_reconciliation_shape(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload.keys()) != {"attempts_total", "recent_failures"}:
        raise _invalid()
    attempts_total = payload.get("attempts_total")
    if type(attempts_total) is not int or attempts_total < 0:
        raise _invalid()
    recent_failures = payload.get("recent_failures")
    if not isinstance(recent_failures, list) or len(recent_failures) > 10:
        raise _invalid()
    for entry in recent_failures:
        if not _is_bounded_nonempty_str(entry, max_bytes=_FAILURE_DETAIL_MAX_BYTES):
            raise _invalid()


def validate_lifecycle_json_schema(payload: object, *, object_format: str) -> dict:
    """Schema validation for a `lifecycle.json` document, narrowed to
    the shapes ADR 0004 section 5 pins with full confidence: exact
    top-level and nested key sets (unknown fields refused everywhere),
    type/format checks, the ADR's exact container and checkpoint-ref
    valid-combination tables (the latter reusing
    `checkpoint_session.CheckpointTransition` rather than a second
    copy), and object-format-aware exact-hex validation of every
    non-null persisted Git object id (`checkpoint_ref`'s three SHA
    fields). `object_format` is the repository's actual object format
    (`"sha1"` or `"sha256"`, e.g. from that repository's own
    `repo.json`) — the projection itself carries no such field, so a
    caller must supply it. `worktree` and `failure` are narrowed to
    only their initial (`absent`/`null`) shape: the ADR does not yet
    give either one a combination table as complete as containers' or
    checkpoint_ref's, so this validator refuses every other shape
    categorically rather than guessing an unreviewed one (see
    `_validate_worktree_shape`/`_validate_failure_shape`). A pure
    function: it validates shape only, never filesystem or lock state,
    and is not yet called by any production read path in this slice
    (3A-2 only ever writes a freshly built projection; a future slice
    reading an existing one back for transition would call this
    first)."""
    try:
        parsed_object_format = ObjectFormat(object_format)
    except ValueError:
        raise _invalid() from None

    if not isinstance(payload, dict) or set(payload.keys()) != {
        "schema_version",
        "lifecycle_id",
        "state_root_id",
        "repo_key",
        "run_id",
        "source_repo_path",
        "state",
        "containers",
        "worktree",
        "checkpoint_ref",
        "failure",
        "reconciliation",
    }:
        raise _invalid()

    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise _invalid()

    for field_name in ("lifecycle_id", "state_root_id", "repo_key"):
        if not _is_hex32(payload.get(field_name)):
            raise _invalid()

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _invalid()
    if len(canonical_json_dumps(run_id)) > RUN_ID_MAX_ENCODED_BYTES:
        raise _invalid()

    source_repo_path = payload.get("source_repo_path")
    if not isinstance(source_repo_path, str) or not source_repo_path or not os.path.isabs(source_repo_path):
        raise _invalid()
    if len(os.fsencode(source_repo_path)) > STORED_PATH_MAX_FS_BYTES:
        raise _invalid()

    state = payload.get("state")
    if state not in {member.value for member in LifecycleState}:
        raise _invalid()

    containers = payload.get("containers")
    if not isinstance(containers, dict) or set(containers.keys()) != {"baseline", "verification"}:
        raise _invalid()
    _validate_container_shape(containers.get("baseline"), role="baseline")
    _validate_container_shape(containers.get("verification"), role="verification")

    _validate_worktree_shape(payload.get("worktree"))
    _validate_checkpoint_ref_shape(payload.get("checkpoint_ref"), object_format=parsed_object_format)
    _validate_failure_shape(payload.get("failure"))
    _validate_reconciliation_shape(payload.get("reconciliation"))

    return payload


def _encode_and_bound_projection(projection: LifecycleProjection) -> bytes:
    """Enforce the three ADR-fixed bounds this schema is subject to
    (run_id's own 256-byte encoded bound, the diagnostic path's
    4096-filesystem-byte bound, and the whole document's 64 KiB bound)
    as three distinct checks, each raising `OVERSIZED` with a bound-
    specific message — not merely relying on the aggregate document
    bound to catch an oversized individual field."""
    if len(canonical_json_dumps(projection.run_id)) > RUN_ID_MAX_ENCODED_BYTES:
        raise LifecycleStoreError(LifecycleStoreFailure.OVERSIZED, "run_id exceeds its encoded size bound")
    if len(os.fsencode(projection.source_repo_path)) > STORED_PATH_MAX_FS_BYTES:
        raise LifecycleStoreError(
            LifecycleStoreFailure.OVERSIZED, "the diagnostic source repository path exceeds its filesystem-byte bound"
        )
    data = canonical_json_dumps(projection_to_dict(projection))
    if len(data) > LIFECYCLE_JSON_MAX_BYTES:
        raise LifecycleStoreError(LifecycleStoreFailure.OVERSIZED, "lifecycle.json content exceeds its size bound")
    return data


def _projection_from_dict(payload: dict) -> LifecycleProjection:
    """Inverse of `projection_to_dict`. Called only after
    `validate_lifecycle_json_schema` has already accepted `payload` —
    every field access below is therefore known-shape."""
    containers = payload["containers"]
    baseline = containers["baseline"]
    verification = containers["verification"]
    worktree = payload["worktree"]
    ref = payload["checkpoint_ref"]
    failure = payload["failure"]
    reconciliation = payload["reconciliation"]
    return LifecycleProjection(
        schema_version=payload["schema_version"],
        lifecycle_id=payload["lifecycle_id"],
        state_root_id=payload["state_root_id"],
        repo_key=payload["repo_key"],
        run_id=payload["run_id"],
        source_repo_path=payload["source_repo_path"],
        state=LifecycleState(payload["state"]),
        baseline=ContainerAttribution(intent=ContainerIntent(baseline["intent"]), id=baseline["id"]),
        verification=ContainerAttribution(intent=ContainerIntent(verification["intent"]), id=verification["id"]),
        worktree=WorktreeAttribution(
            intent=WorktreeIntent(worktree["intent"]), expected_head=worktree["expected_head"]
        ),
        checkpoint_ref=CheckpointTransition(
            intent=CheckpointIntent(ref["intent"]),
            accepted_sha=ref["accepted_sha"],
            expected_old_sha=ref["expected_old_sha"],
            proposed_new_sha=ref["proposed_new_sha"],
        ),
        failure=FailureDetail(phase=failure["phase"], detail=failure["detail"]) if failure is not None else None,
        reconciliation=ReconciliationSummary(
            attempts_total=reconciliation["attempts_total"],
            recent_failures=tuple(reconciliation["recent_failures"]),
        ),
    )


def load_lifecycle_projection(
    run_dir_fd: int,
    *,
    object_format: str,
    expected_lifecycle_id: str,
    expected_repo_key: str,
    expected_state_root_id: str,
) -> LifecycleProjection:
    """Read and fully validate `lifecycle.json` beneath the caller's own
    open `run_dir_fd` (Milestone 3 Slice 3B-1, ADR 0004 Amendment 2).

    Fixed-name, fd-relative, `O_NOFOLLOW` open — never a fresh
    full-pathname lookup. The opened descriptor is authoritative: every
    check below (`fstat`, size, ownership, permissions, link count) is
    performed on the open file descriptor itself, never re-derived from
    a separate path-based check. A missing file, a symlink, a
    hard-linked file (`st_nlink != 1`), unsafe ownership or permissions,
    an oversized file, malformed or duplicate-keyed JSON, a
    schema-invalid payload, or a mismatch against the separately
    supplied trusted `expected_lifecycle_id`/`expected_repo_key`/
    `expected_state_root_id` are all `LifecycleStoreError(SCHEMA_INVALID)`
    — a run directory without a valid, identity-matched projection is
    never distinguished from one that is merely corrupt, and is never
    repaired, deleted, or adopted automatically (ADR 0004 Amendment 2
    section 7 — future abandonment is the designed recovery path). A
    genuine I/O failure (not "the file doesn't exist") is
    `LifecycleStoreError(SUBSTRATE_UNAVAILABLE)`.
    """
    try:
        fd = os.open(
            LIFECYCLE_JSON_FILENAME,
            os.O_RDONLY | _nofollow_flag() | _cloexec_flag(),
            dir_fd=run_dir_fd,
        )
    except OSError as exc:
        # A positively observed wrong state -- the file is genuinely
        # absent, or O_NOFOLLOW positively refused a symlink at that
        # name -- is REFUSED (SCHEMA_INVALID): never distinguished from
        # "the projection is corrupt," per this function's own accepted
        # contract. Any other open failure (permission truly denied at
        # the OS level, a resource-exhaustion error, an I/O error) is a
        # genuine inability to inspect, not an observed bad shape, and
        # must not be conflated with it.
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            raise LifecycleStoreError(
                LifecycleStoreFailure.SCHEMA_INVALID,
                "lifecycle.json is missing or a symlink",
            ) from None
        raise LifecycleStoreError(
            LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE,
            "lifecycle.json could not be opened",
        ) from exc

    try:
        payload = _read_and_validate_lifecycle_fd(
            fd,
            object_format=object_format,
            expected_lifecycle_id=expected_lifecycle_id,
            expected_repo_key=expected_repo_key,
            expected_state_root_id=expected_state_root_id,
        )
    except BaseException as exc:
        _close_or_chain([fd], exc)
        raise
    else:
        _close_or_chain([fd], None)

    return _projection_from_dict(payload)


def _read_and_validate_lifecycle_fd(
    fd: int,
    *,
    object_format: str,
    expected_lifecycle_id: str,
    expected_repo_key: str,
    expected_state_root_id: str,
) -> dict:
    try:
        _assert_cloexec(fd)
    except LifecycleFsError as exc:
        raise LifecycleStoreError(
            LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE,
            "lifecycle.json's descriptor is not non-inheritable",
        ) from exc

    try:
        st = os.fstat(fd)
    except OSError:
        raise LifecycleStoreError(
            LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE, "lifecycle.json could not be inspected"
        ) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise LifecycleStoreError(LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json is not a regular file")
    if st.st_nlink != 1:
        raise LifecycleStoreError(LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json is unexpectedly hard-linked")
    if st.st_uid != os.getuid():
        raise LifecycleStoreError(
            LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json is not owned by the current user"
        )
    if st.st_mode & 0o077:
        raise LifecycleStoreError(LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json has unsafe permissions")
    if st.st_size > LIFECYCLE_JSON_MAX_BYTES:
        raise LifecycleStoreError(LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json exceeds its size bound")

    try:
        data = read_all_eintr_safe(fd, LIFECYCLE_JSON_MAX_BYTES)
    except LifecycleFsError as exc:
        raise LifecycleStoreError(LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE, "lifecycle.json could not be read") from exc

    try:
        payload = canonical_json_loads_strict(data, max_bytes=LIFECYCLE_JSON_MAX_BYTES)
    except LifecycleFsError:
        raise LifecycleStoreError(
            LifecycleStoreFailure.SCHEMA_INVALID, "lifecycle.json is corrupt and is never regenerated"
        ) from None

    payload = validate_lifecycle_json_schema(payload, object_format=object_format)

    if (
        payload["lifecycle_id"] != expected_lifecycle_id
        or payload["repo_key"] != expected_repo_key
        or payload["state_root_id"] != expected_state_root_id
    ):
        raise LifecycleStoreError(
            LifecycleStoreFailure.SCHEMA_INVALID,
            "lifecycle.json's recorded identity does not match its trusted expected identity",
        )

    return payload


def _publish_projection_state(
    run_dir_fd: int,
    projection: LifecycleProjection,
    *,
    state: LifecycleState,
    attempts_total: int,
) -> LifecycleProjection:
    """Publish `projection` with `state` and
    `reconciliation.attempts_total` replaced, reusing the exact same
    atomic-publish primitive and size bounds as the initial `PREPARING`
    write (Milestone 3 Slice 3B-1). Every other field is unchanged.
    Raises `LifecycleStoreError` classified by
    `_classify_publication_failure` on any publication failure —
    `PROJECTION_PUBLICATION_FAILED` (nothing installed) or
    `PROJECTION_DURABILITY_UNCONFIRMED` (installed, directory-fsync
    unconfirmed) — never conflated. Returns the updated in-memory
    projection only on confirmed success."""
    updated = replace(
        projection,
        state=state,
        reconciliation=replace(projection.reconciliation, attempts_total=attempts_total),
    )
    data = _encode_and_bound_projection(updated)
    try:
        publish_private_file_atomically_at(run_dir_fd, LIFECYCLE_JSON_FILENAME, data, mode=0o600)
    except LifecycleFsError as exc:
        raise _classify_publication_failure(exc) from exc
    return updated


def _close_or_chain(fds: list[int], primary: BaseException | None) -> None:
    """This module's own copy of the established close-confirmed-or-
    chain pattern (`_lifecycle_fs.py`, `state_root.py`, and
    `repo_identity.py` each keep their own inline copy rather than
    sharing a private cross-module helper; this module follows the same
    convention)."""
    if not fds:
        return
    try:
        close_confirmed(fds)
    except LifecycleFsError as cleanup_exc:
        if primary is not None:
            raise LifecycleStoreError(
                LifecycleStoreFailure.CLEANUP_UNCONFIRMED,
                "a descriptor could not be confirmed closed",
            ) from primary
        raise LifecycleStoreError(
            LifecycleStoreFailure.CLEANUP_UNCONFIRMED,
            "a descriptor could not be confirmed closed",
        ) from cleanup_exc


def _classify_publication_failure(exc: LifecycleFsError) -> LifecycleStoreError:
    """Translate `publish_private_file_atomically_at`'s three distinct
    outcomes into this module's own classification, preserving
    causality (`raise ... from exc`) and never reporting "installed but
    durability unconfirmed" under the same reason as "failed before
    installation" (the ambiguity this correction fixes)."""
    if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
        return LifecycleStoreError(
            LifecycleStoreFailure.CLEANUP_UNCONFIRMED,
            "a temporary publication file could not be confirmed removed",
        )
    if exc.reason is LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED:
        return LifecycleStoreError(
            LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED,
            "lifecycle.json was installed but its directory entry's durability could not be confirmed",
        )
    return LifecycleStoreError(
        LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED,
        "lifecycle.json could not be published",
    )


class LifecycleLease:
    """Owns every resource `prepare_lifecycle` acquires, for the
    caller's required lifetime: the state-root descriptor, the
    exclusively-created run-directory descriptor, the repository lock,
    and the lifecycle lock. `close()` (and the context-manager
    protocol) releases them in the exact required order — lifecycle
    lock, run-directory descriptor, repository lock, state-root
    descriptor — attempting every step regardless of an earlier step's
    outcome; any failure is reported together, dominating a clean
    result, and chained from the earliest failure among them (or, via
    the context-manager protocol, from an in-flight body exception
    instead — the same convention `StateRoot`/`LockHandle` already
    use). Never releases a resource this instance does not itself
    hold: an attribute left `None` during partial construction (a
    failed `prepare_lifecycle` call unwinding early) is simply skipped.
    """

    def __init__(
        self,
        *,
        state_root=None,
        run_dir_fd: int | None = None,
        repository_lock=None,
        lifecycle_lock=None,
        lifecycle_id: str | None = None,
        repo_key: str | None = None,
        run_dir_path: str | None = None,
        object_format: str | None = None,
    ) -> None:
        self.state_root = state_root
        self.run_dir_fd = run_dir_fd
        self.repository_lock = repository_lock
        self.lifecycle_lock = lifecycle_lock
        self.lifecycle_id = lifecycle_id
        self.repo_key = repo_key
        self.run_dir_path = run_dir_path
        # The trusted repository object format ("sha1"/"sha256"),
        # discovered once via `RepositoryIdentity.object_format`
        # (Slice 3B-2, ADR 0004 Amendment 3) — never inferred from a
        # caller-supplied SHA or from untrusted projection content.
        self.object_format = object_format
        self._closed = False

    def __enter__(self) -> "LifecycleLease":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except LifecycleStoreError as cleanup_exc:
            if exc_value is not None:
                raise cleanup_exc from exc_value
            raise
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        failed_stages: list[str] = []
        first_failure: BaseException | None = None

        if self.lifecycle_lock is not None:
            try:
                self.lifecycle_lock.release()
            except LockError as exc:
                failed_stages.append("lifecycle lock")
                first_failure = first_failure or exc

        if self.run_dir_fd is not None:
            try:
                close_confirmed([self.run_dir_fd])
            except LifecycleFsError as exc:
                failed_stages.append("run-directory descriptor")
                first_failure = first_failure or exc

        if self.repository_lock is not None:
            try:
                self.repository_lock.release()
            except LockError as exc:
                failed_stages.append("repository lock")
                first_failure = first_failure or exc

        if self.state_root is not None:
            try:
                self.state_root.close()
            except LifecycleFsError as exc:
                failed_stages.append("state-root descriptor")
                first_failure = first_failure or exc

        if failed_stages:
            raise LifecycleStoreError(
                LifecycleStoreFailure.CLEANUP_UNCONFIRMED,
                "one or more lifecycle-lease resources could not be confirmed released: "
                + ", ".join(failed_stages),
            ) from first_failure

    def open_projection_writer(self) -> tuple["_LifecycleProjectionWriter", LifecycleProjection]:
        """The only sanctioned way to obtain a projection writer bound
        to this lease (Slice 3B-2, ADR 0004 Amendment 3). Refuses
        categorically, before any write is possible or any raw
        `TypeError`/`AttributeError`/invalid-descriptor error can
        escape, if the lease is not fully prepared: `run_dir_fd`,
        `object_format`, or `state_root` is unset (an incomplete or
        never-completed construction). Then performs the exact same
        complete lock-scope validation every write method uses —
        held, `LIFECYCLE`-kind, exact `repo_key`/`lifecycle_id` match —
        which also transitively refuses an already-`close()`d lease
        (`release()` clears `is_held`). Performs exactly one
        authoritative read and returns it as the caller's first
        `expected` — the single source of truth for "what is current,"
        never a separately cached in-memory value.
        """
        if self.run_dir_fd is None or self.object_format is None or self.state_root is None:
            raise LifecycleStoreError(
                LifecycleStoreFailure.WRONG_LOCK_SCOPE,
                "a projection writer requires a fully prepared lease",
            )
        writer = _LifecycleProjectionWriter(self)
        writer._require_lease_lock_held()
        current = writer._load_authoritative()
        return writer, current


_OWNER_STATES = (LifecycleState.PREPARING, LifecycleState.ACTIVE, LifecycleState.CLEANING, LifecycleState.COMPLETE)
_OWNER_STATE_EDGES: dict[LifecycleState, LifecycleState] = {
    LifecycleState.PREPARING: LifecycleState.ACTIVE,
    LifecycleState.ACTIVE: LifecycleState.CLEANING,
    LifecycleState.CLEANING: LifecycleState.COMPLETE,
}

# The lifecycle states in which a resource (container or checkpoint-ref)
# transition is ever legal -- ADR 0004 Amendment 3 §1's state gate.
# `COMPLETE` is clean-final (I15) and `RECONCILING`/`RECONCILED`/
# `RECONCILIATION_FAILED` are reconciler-owned (I11): none of those
# projections may ever be mutated again, resource transitions included,
# not even an exact no-op. This enforces the already-accepted clean-
# final/I11/I15 rules -- it is not a new lifecycle state graph.
_RESOURCE_WRITABLE_STATES = (LifecycleState.PREPARING, LifecycleState.ACTIVE, LifecycleState.CLEANING)

_CONTAINER_TRANSITION_EDGES: frozenset[tuple[ContainerIntent, ContainerIntent]] = frozenset(
    {
        (ContainerIntent.ABSENT, ContainerIntent.CREATING),
        (ContainerIntent.CREATING, ContainerIntent.PRESENT),
        # ADR 0004 Amendment 3 clarification: a failed-but-confirmed
        # create may return to absent, enabling the role name to be
        # safely reused for a later attempt. This writer never
        # inspects Docker itself; the caller is solely responsible for
        # having confirmed, by real Docker inspection, that no
        # container was ever created for this attempt before invoking
        # this edge.
        (ContainerIntent.CREATING, ContainerIntent.ABSENT),
        (ContainerIntent.PRESENT, ContainerIntent.REMOVING),
        (ContainerIntent.REMOVING, ContainerIntent.ABSENT),
    }
)


def _validate_checkpoint_ref_edge(current: CheckpointTransition, target: CheckpointTransition) -> None:
    """Cross-transition SHA continuity (ADR 0004 section 5's write-
    ahead and operation-specific recovery prose), not merely intent-
    pair adjacency or per-record shape -- `CheckpointTransition.
    __post_init__` and `_is_valid_oid_for_format` already guarantee a
    single record's own internal shape; this checks that the *target*
    record is a truthful continuation of the *current* one. The caller
    never re-derives an outcome here -- `target` is trusted to already
    be the decided output of `checkpoint_session.CheckpointSession`'s
    own collapse logic."""
    c, t = current.intent, target.intent

    if c is CheckpointIntent.ABSENT and t is CheckpointIntent.CREATING:
        return
    if c is CheckpointIntent.CREATING and t is CheckpointIntent.PRESENT:
        if target.accepted_sha == current.proposed_new_sha:
            return
        raise _illegal_transition("creating->present must confirm the record's own proposed SHA")
    if c is CheckpointIntent.CREATING and t is CheckpointIntent.ABSENT:
        return  # confirmed-unchanged create-failure recovery (ADR 0004 section 5, explicit)
    if c is CheckpointIntent.PRESENT and t is CheckpointIntent.ADVANCING:
        if target.accepted_sha == current.accepted_sha and target.expected_old_sha == current.accepted_sha:
            return
        raise _illegal_transition("advancing must continue from the current accepted SHA")
    if c is CheckpointIntent.ADVANCING and t is CheckpointIntent.PRESENT:
        if target.accepted_sha in (current.accepted_sha, current.proposed_new_sha):
            return
        raise _illegal_transition("an advance collapse must match either the old or the proposed SHA")
    if c is CheckpointIntent.PRESENT and t is CheckpointIntent.REMOVING:
        if target.accepted_sha == current.accepted_sha and target.expected_old_sha == current.accepted_sha:
            return
        raise _illegal_transition("removing must match the current accepted SHA")
    if c is CheckpointIntent.REMOVING and t is CheckpointIntent.ABSENT:
        return  # confirmed-removal collapse
    raise _illegal_transition("not a legal checkpoint-ref transition edge")


def _illegal_transition(message: str) -> LifecycleStoreError:
    return LifecycleStoreError(LifecycleStoreFailure.ILLEGAL_TRANSITION, message)


class _LifecycleProjectionWriter:
    """Locked, authoritative lifecycle-projection writer (Slice 3B-2,
    ADR 0004 Amendment 3). Never constructed directly -- obtained only
    via `LifecycleLease.open_projection_writer()`. Every method
    re-verifies the lease's lifecycle lock at call time (never trusted
    from construction) and loads the currently installed authoritative
    projection fresh before any transition decision, refusing a stale
    caller-supplied `expected` before any publication I/O. Publishes
    only via the existing atomic-publish primitive, unchanged.

    Never writes a populated `failure` or a non-absent worktree shape
    (both deliberately deferred, matching Slice 3A-2/3B-1's own
    narrowing), never performs a Docker or Git call of any kind, and
    never writes `RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED` --
    those remain `reconciliation.py`'s own private, reconciler-owned
    write path, structurally unreachable through this owner-facing
    API even when the requested state is already identical.
    """

    def __init__(self, lease: LifecycleLease) -> None:
        self._lease = lease

    def _require_lease_lock_held(self) -> None:
        lock = self._lease.lifecycle_lock
        repo_key = self._lease.repo_key
        lifecycle_id = self._lease.lifecycle_id
        if lock is None or not lock.is_held or repo_key is None or lifecycle_id is None:
            raise LifecycleStoreError(
                LifecycleStoreFailure.WRONG_LOCK_SCOPE,
                "a projection write requires an already-held lifecycle lock on a fully identified lease",
            )
        try:
            expected_scope = LockScope(kind=LockKind.LIFECYCLE, repo_key=repo_key, lifecycle_id=lifecycle_id)
        except LifecycleFsError as exc:
            raise LifecycleStoreError(
                LifecycleStoreFailure.WRONG_LOCK_SCOPE,
                "the lease's own identity is not a valid lock scope",
            ) from exc
        if lock.scope != expected_scope:
            raise LifecycleStoreError(
                LifecycleStoreFailure.WRONG_LOCK_SCOPE,
                "the held lock's scope does not match this lease (not a lifecycle lock, or a different repo_key/lifecycle_id)",
            )

    def _load_authoritative(self) -> LifecycleProjection:
        return load_lifecycle_projection(
            self._lease.run_dir_fd,
            object_format=self._lease.object_format,
            expected_lifecycle_id=self._lease.lifecycle_id,
            expected_repo_key=self._lease.repo_key,
            expected_state_root_id=self._lease.state_root.state_root_id,
        )

    def _require_current(self, expected: LifecycleProjection) -> LifecycleProjection:
        """The authoritative-read/stale-write-prevention flow: verify
        lock scope, load and fully validate the currently installed
        authoritative projection fresh (a corrupt or identity-mismatched
        file is refused here, unchanged, and never overwritten), then
        refuse a stale `expected` before any publication I/O. A
        successful read here validates the currently installed content
        -- it cannot retroactively prove a prior failed directory-
        `fsync` durable; no power-loss durability claim is made. Note
        this performs one authoritative *read* -- callers should never
        describe a refusal here as "zero I/O"; it is "zero publication/
        write I/O"."""
        self._require_lease_lock_held()
        current = self._load_authoritative()
        if current != expected:
            raise LifecycleStoreError(
                LifecycleStoreFailure.STALE_EXPECTED_PROJECTION,
                "the caller's expected projection no longer matches the currently installed authoritative projection",
            )
        return current

    def refresh(self) -> LifecycleProjection:
        """Explicit read-only recovery operation. Call this after a
        `PROJECTION_DURABILITY_UNCONFIRMED` publication result -- never
        blindly retry with the old `expected`. That result means the
        new complete projection is currently installed (`os.replace`
        already confirmed it), but its directory-entry durability was
        not confirmed by this process -- no power-loss durability claim
        is made either way. This method returns the currently
        installed, fully validated projection for reconsideration.
        Performs one authoritative read -- zero publication/write I/O
        -- and requires the lifecycle lock still be held. Never invoked
        automatically by any write method."""
        self._require_lease_lock_held()
        return self._load_authoritative()

    def _publish(self, updated: LifecycleProjection) -> LifecycleProjection:
        data = _encode_and_bound_projection(updated)
        try:
            publish_private_file_atomically_at(self._lease.run_dir_fd, LIFECYCLE_JSON_FILENAME, data, mode=0o600)
        except LifecycleFsError as exc:
            raise _classify_publication_failure(exc) from exc
        return updated

    def advance_lifecycle_state(self, *, expected: LifecycleProjection, state: LifecycleState) -> LifecycleProjection:
        """Owner-facing state-graph transitions only:
        `PREPARING->ACTIVE->CLEANING->COMPLETE`. `RECONCILING`,
        `RECONCILED`, and `RECONCILIATION_FAILED` are refused
        unconditionally -- including when `state` already equals the
        (reconciler-owned) current state -- since those remain
        `reconciliation.py`'s own private write path. Exact no-op
        (zero publication/write I/O beyond the mandatory authoritative
        read) is permitted only among the four owner states themselves.
        `CLEANING->COMPLETE` additionally requires the complete
        clean-final absent shape (ADR 0004's clean-final rule, I15)."""
        current = self._require_current(expected)
        if state not in _OWNER_STATES or current.state not in _OWNER_STATES:
            raise _illegal_transition("RECONCILING/RECONCILED/RECONCILIATION_FAILED are reconciler-owned and refused here")
        if state == current.state:
            return current
        if _OWNER_STATE_EDGES.get(current.state) != state:
            raise _illegal_transition("not a legal owner lifecycle-state edge")
        if state is LifecycleState.COMPLETE and not is_projection_fully_absent_shape(current):
            raise _illegal_transition("CLEANING->COMPLETE requires the complete clean-final absent shape")
        return self._publish(replace(current, state=state))

    def _require_resource_writable_state(self, current_projection: LifecycleProjection) -> None:
        """ADR 0004 Amendment 3 §1's state gate, shared by both
        resource-transition methods: a resource transition -- even an
        exact no-op -- is legal only while the authoritative lifecycle
        state is owner-controlled and nonterminal (`PREPARING`/
        `ACTIVE`/`CLEANING`). `COMPLETE` is clean-final (I15);
        `RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED` are
        reconciler-owned (I11) -- neither may ever be mutated again by
        this live-owner resource-writer boundary."""
        if current_projection.state not in _RESOURCE_WRITABLE_STATES:
            raise _illegal_transition(
                "resource transitions require an owner-controlled nonterminal state "
                "(PREPARING/ACTIVE/CLEANING) -- refused for COMPLETE or any reconciler-owned state"
            )

    def record_container_transition(
        self,
        *,
        expected: LifecycleProjection,
        role: str,
        intent: ContainerIntent,
        id: str | None,
    ) -> LifecycleProjection:
        """ADR 0004 section 7's persisted-combination table and write-
        ahead edges, per role, independently. This writer never
        inspects Docker; every precondition (that a container was
        actually created/started/removed, or confirmed never created)
        is the caller's own responsibility to have confirmed before
        invoking the corresponding edge.

        Uniform writer-call ordering: lock scope and authoritative
        read/stale comparison happen first (`_require_current`), then
        the state gate, then request-shape validation -- so a wrong
        lock scope or a stale `expected` is never masked by an invalid
        `role`/`intent`/`id` argument being checked first."""
        current_projection = self._require_current(expected)
        self._require_resource_writable_state(current_projection)

        if role not in ("baseline", "verification"):
            raise _illegal_transition("role must be exactly 'baseline' or 'verification'")
        if intent in (ContainerIntent.ABSENT, ContainerIntent.CREATING):
            if id is not None:
                raise _illegal_transition("absent/creating must carry no id")
        else:
            if not isinstance(id, str) or not id:
                raise _illegal_transition("present/removing requires a nonempty id")

        current_attr = current_projection.baseline if role == "baseline" else current_projection.verification
        target_attr = ContainerAttribution(intent=intent, id=id)

        if current_attr == target_attr:
            return current_projection

        if (current_attr.intent, intent) not in _CONTAINER_TRANSITION_EDGES:
            raise _illegal_transition("not a legal container transition edge")
        if current_attr.intent is ContainerIntent.PRESENT and intent is ContainerIntent.REMOVING and id != current_attr.id:
            raise _illegal_transition("present->removing must retain the exact same id")

        if role == "baseline":
            updated = replace(current_projection, baseline=target_attr)
        else:
            updated = replace(current_projection, verification=target_attr)
        return self._publish(updated)

    def record_checkpoint_ref_transition(
        self, *, expected: LifecycleProjection, transition: CheckpointTransition
    ) -> LifecycleProjection:
        """ADR 0004 section 5's write-ahead and operation-specific
        recovery edges, with full cross-transition SHA continuity
        (`_validate_checkpoint_ref_edge`), not merely per-record shape.
        `transition` is trusted to already be the decided output of
        `checkpoint_session.CheckpointSession`'s own collapse logic --
        this method never re-derives an outcome. `CheckpointSession`
        has no seam today to call this at the ADR-required moment (see
        ADR 0004 Amendment 3) -- this method is correctness-tested
        standalone, not yet durably wired to a real checkpoint-ref
        mutation.

        Uniform writer-call ordering: lock scope and authoritative
        read/stale comparison happen first, then the state gate, then
        `transition`'s own type is sanitized before any field access --
        a raw `None` or wrong-type `transition` never reaches an
        unguarded attribute lookup."""
        current_projection = self._require_current(expected)
        self._require_resource_writable_state(current_projection)

        if not isinstance(transition, CheckpointTransition):
            raise _illegal_transition("transition must be a CheckpointTransition")

        current_transition = current_projection.checkpoint_ref

        if current_transition == transition:
            return current_projection

        object_format = ObjectFormat(self._lease.object_format)
        for value in (transition.accepted_sha, transition.expected_old_sha, transition.proposed_new_sha):
            if value is not None and not _is_valid_oid_for_format(value, object_format=object_format):
                raise _illegal_transition("a checkpoint-ref SHA does not match the repository's object format")

        _validate_checkpoint_ref_edge(current_transition, transition)

        updated = replace(current_projection, checkpoint_ref=transition)
        return self._publish(updated)


def prepare_lifecycle(source_repo_path: Path | str, *, run_id: str) -> LifecycleLease:
    """The exact accepted Slice 3A-2 composition (ADR 0004 Amendment 1
    section 16): Git preflight, repository discovery, trusted state
    root, repository lock, `repo.json`, a fresh exclusive
    `runs/<lifecycle_id>/` directory, the lifecycle lock, and the
    initial durable `PREPARING` `lifecycle.json` — strictly before any
    Docker container, disposable worktree, or checkpoint ref exists.
    Returns an owned `LifecycleLease`; the caller is responsible for
    eventually calling `close()` (or using it as a context manager).

    Now also runs automatic pre-run reconciliation (`reconciliation.
    reconcile_repository`, Slice 3B-1) immediately after
    `load_or_create_repo_json` and before `new_lifecycle_id()` — the
    exact ADR 0004 section 16 insertion point. A blocked reconciliation
    pass raises `LifecycleStoreError(RECONCILIATION_BLOCKED)` before a
    lifecycle_id is minted or a run directory is created.

    Deliberately not implemented here: abandonment, the maintenance
    trace's `explicit` trigger, and any container/worktree/
    checkpoint-ref *removal*. This function alone does **not** fully
    mitigate T-E1: nothing here refuses a second *concurrent* run
    beyond the repository lock's own ordinary non-blocking `BUSY`
    refusal, which is `acquire_repository_lock`'s existing 3A-1
    behavior — reconciliation only recovers a *dead* prior run.
    """
    if not isinstance(run_id, str) or not run_id:
        raise LifecycleStoreError(LifecycleStoreFailure.OVERSIZED, "run_id must be a nonempty string")
    if len(canonical_json_dumps(run_id)) > RUN_ID_MAX_ENCODED_BYTES:
        raise LifecycleStoreError(LifecycleStoreFailure.OVERSIZED, "run_id exceeds its encoded size bound")

    check_git_preflight()
    identity, context = discover_repository_identity_and_context(source_repo_path)
    if context.working_tree_root is None:
        # Repository discovery already refuses a bare repository
        # (`BARE_REPOSITORY_UNSUPPORTED`), so this should be
        # unreachable in practice — but the diagnostic source path this
        # composition is about to persist depends on it, so fail
        # closed explicitly rather than persisting a wrong or absent
        # value if that invariant is ever violated.
        raise LifecycleStoreError(
            LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE,
            "the trusted repository's canonical working-tree root is unexpectedly absent",
        )

    location = resolve_state_root_path()
    root_fd, canonical_root_path = open_or_create_canonical_root(location)
    try:
        validate_state_root_containment(canonical_root_path, context)
        state_root = init_state_root(root_fd, canonical_root_path)
    except BaseException as exc:
        _close_or_chain([root_fd], exc)
        raise

    lease = LifecycleLease(state_root=state_root, repo_key=identity.repo_key)
    try:
        repository_lock = acquire_repository_lock(state_root, identity.repo_key)
        lease.repository_lock = repository_lock

        validated_identity = load_or_create_repo_json(state_root, identity, repository_lock)
        lease.object_format = validated_identity.object_format

        # Automatic pre-run reconciliation (ADR 0004 Amendment 1
        # section 10, Amendment 2 — Milestone 3 Slice 3B-1): after
        # repository-lock acquisition and repository-identity
        # validation, but before a new lifecycle_id is ever minted.
        # Imported lazily to avoid a module import cycle
        # (reconciliation.py imports several names from this module).
        from .reconciliation import ReconciliationError, reconcile_repository

        try:
            reconciliation_result = reconcile_repository(
                state_root=state_root,
                identity=validated_identity,
                context=context,
                repository_lock=repository_lock,
            )
        except ReconciliationError as exc:
            raise LifecycleStoreError(
                LifecycleStoreFailure.RECONCILIATION_BLOCKED,
                "automatic pre-run reconciliation could not complete and blocked this run",
            ) from exc
        if reconciliation_result.blocked:
            raise LifecycleStoreError(
                LifecycleStoreFailure.RECONCILIATION_BLOCKED,
                "automatic pre-run reconciliation found an unresolved entry and blocked this run",
            )

        lifecycle_id = new_lifecycle_id()

        runs_fd = open_managed_directory_chain(state_root.root_fd, ["repos", validated_identity.repo_key, RUNS_DIRNAME])
        try:
            try:
                run_dir_fd = create_exclusive_directory_at(runs_fd, lifecycle_id)
            except FileExistsError:
                raise LifecycleStoreError(
                    LifecycleStoreFailure.LIFECYCLE_ID_COLLISION,
                    "a freshly minted lifecycle_id collided with an existing run directory",
                ) from None
        except BaseException as exc:
            _close_or_chain([runs_fd], exc)
            raise
        else:
            _close_or_chain([runs_fd], None)
        lease.run_dir_fd = run_dir_fd
        lease.lifecycle_id = lifecycle_id
        lease.run_dir_path = f"<state-root>/repos/{validated_identity.repo_key}/{RUNS_DIRNAME}/{lifecycle_id}"

        lifecycle_lock = acquire_lifecycle_lock(
            run_dir_fd,
            repo_key=validated_identity.repo_key,
            lifecycle_id=lifecycle_id,
            diagnostic_path=f"{lease.run_dir_path}/{LIFECYCLE_LOCK_FILENAME}",
        )
        lease.lifecycle_lock = lifecycle_lock

        projection = build_initial_preparing_projection(
            lifecycle_id=lifecycle_id,
            state_root_id=state_root.state_root_id,
            repo_key=validated_identity.repo_key,
            run_id=run_id,
            source_repo_path=context.working_tree_root,
        )
        data = _encode_and_bound_projection(projection)
        try:
            publish_private_file_atomically_at(run_dir_fd, LIFECYCLE_JSON_FILENAME, data, mode=0o600)
        except LifecycleFsError as exc:
            raise _classify_publication_failure(exc) from exc

        return lease
    except BaseException as exc:
        try:
            lease.close()
        except LifecycleStoreError as cleanup_exc:
            raise cleanup_exc from exc
        raise
