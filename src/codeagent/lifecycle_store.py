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

Not implemented here (later Milestone 3 slices): automatic
reconciliation (to be inserted after repository-lock acquisition and
repository-identity validation but before lifecycle-ID generation),
abandonment, the maintenance trace, container/worktree/checkpoint-ref
attribution or mutation of any kind, and any `RunController`/CLI
wiring. This module is deliberately unwired. T-E1 (concurrent runs
against the same repository) is therefore **not** mitigated by this
slice alone: nothing yet calls this composition before a real run
starts.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

from ._git_safety import ObjectFormat, check_git_preflight
from ._lifecycle_fs import (
    STORED_PATH_MAX_FS_BYTES,
    LifecycleFsError,
    LifecycleFsFailure,
    canonical_json_dumps,
    close_confirmed,
    create_exclusive_directory_at,
    open_managed_directory_chain,
    publish_private_file_atomically_at,
    resolve_state_root_path,
    validate_hex32,
)
from .checkpoint_ref import new_lifecycle_id
from .checkpoint_session import ABSENT_TRANSITION, CheckpointIntent, CheckpointTransition
from .repo_identity import discover_repository_identity_and_context, load_or_create_repo_json
from .state_locks import LockError, acquire_lifecycle_lock, acquire_repository_lock
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
    ) -> None:
        self.state_root = state_root
        self.run_dir_fd = run_dir_fd
        self.repository_lock = repository_lock
        self.lifecycle_lock = lifecycle_lock
        self.lifecycle_id = lifecycle_id
        self.repo_key = repo_key
        self.run_dir_path = run_dir_path
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


def prepare_lifecycle(source_repo_path: Path | str, *, run_id: str) -> LifecycleLease:
    """The exact accepted Slice 3A-2 composition (ADR 0004 Amendment 1
    section 16): Git preflight, repository discovery, trusted state
    root, repository lock, `repo.json`, a fresh exclusive
    `runs/<lifecycle_id>/` directory, the lifecycle lock, and the
    initial durable `PREPARING` `lifecycle.json` — strictly before any
    Docker container, disposable worktree, or checkpoint ref exists.
    Returns an owned `LifecycleLease`; the caller is responsible for
    eventually calling `close()` (or using it as a context manager).

    Deliberately not implemented here: automatic reconciliation (to be
    inserted after repository-lock acquisition and repository-identity
    validation, but before `new_lifecycle_id()` — i.e. exactly where
    this function currently proceeds straight from `load_or_create_
    repo_json` to minting a lifecycle id), abandonment, the maintenance
    trace, and any container/worktree/checkpoint-ref mutation. This
    function alone does **not** mitigate T-E1: nothing here refuses a
    second concurrent run beyond the repository lock's own ordinary
    non-blocking `BUSY` refusal, which is `acquire_repository_lock`'s
    existing 3A-1 behavior, not a new guarantee this slice adds.
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

        # --- Automatic pre-run reconciliation (ADR 0004 Amendment 1
        # section 10) belongs exactly here in a later Milestone 3
        # slice: after repository-lock acquisition and repository-
        # identity validation, but before a new lifecycle_id is ever
        # minted. Not implemented in this slice. ---

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
