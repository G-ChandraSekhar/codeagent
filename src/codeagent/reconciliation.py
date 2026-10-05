"""Milestone 3 Slices 3B-1 and 3B-5: automatic reconciliation, including
safe reconciliation and removal of ADR-attributable Docker containers.

(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1 section 10, Amendment 2, Amendment 5.)

Recognizes and reconciles entries whose durable state is `PREPARING`,
`ACTIVE`, `CLEANING`, or `RECONCILING` (Amendment 5 widened this from
3B-1's original `PREPARING`/`RECONCILING`-only eligibility, since a
crash can leave a dead owner in any of the three owner-writable states)
whose worktree and checkpoint ref are both at their initial absent
shape and whose `failure` is null --
`lifecycle_store.is_projection_reconciliation_eligible_shape` -- with
either container's own shape otherwise unconstrained. Worktree and
checkpoint-ref removal remain entirely out of scope (still 3B-1's own
narrowing); only container reconciliation and removal are new in 3B-5.

After freshly confirming every recomputed external resource (a real,
unfiltered `docker ps -a` listing plus, for every candidate that
matches a recomputed name, a real ownership-proof `docker inspect` by
that candidate's immutable id; a real `git worktree list --porcelain`
listing; and a real `CheckpointRef.observe()`), it writes each
container's own reconciler-owned write-ahead transition
(`lifecycle_store._publish_reconciler_container_transition`,
distinct from the live-owner's own `record_container_transition`, since
a reconciliation pass runs precisely in the states that writer
categorically refuses), removes an attributable present/creating
container by its immutable id, and, once every container plus the
worktree and checkpoint ref are all durably absent, writes the final
`RECONCILING -> RECONCILED` collapse via the unchanged
`_publish_projection_state`. It never writes
`LifecycleState.RECONCILIATION_FAILED` (still reserved for a later
slice that gives up on a genuinely irrecoverable external mutation).

ADR 0004 Amendments 12, 13 and 16 later added worktree rows and a
checkpoint-ref row: a dead entry whose checkpoint ref is non-absent while
its worktree and both containers are confirmed absent has its exact owned
ref removed by one compare-and-swap delete against the observed value
(`_reconcile_checkpoint_ref_entry`). A ref alongside a worktree or a live
container is still refused.

Called by `lifecycle_store.prepare_lifecycle()` while the repository
lock is already held, after `load_or_create_repo_json()` and before a
new `lifecycle_id` is minted or a new run directory is created. Every
filesystem, Docker, worktree, and checkpoint-ref target derives
exclusively from the trusted `state_root`/`identity`/`context`, a
validated directory-name lifecycle id, and fixed recomputed names.
Ownership is proven by inspecting each candidate's own labels, never
assumed from its name alone.

Not implemented here (later Milestone 3 work): abandonment, the
explicit `codeagent reconcile`/`--abandon` CLI and its `explicit`
maintenance-trigger, and any worktree or checkpoint-ref *removal*.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, unique

from ._bounded_subprocess import BoundedProcessError, BoundedProcessFailure, run_bounded_stdout
from ._docker_ownership import (
    CONTAINER_ID_HEX_RE as _CONTAINER_ID_HEX_RE,
)
from ._docker_ownership import (
    CONTAINER_NAME_RE as _CONTAINER_NAME_RE,
)
from ._docker_ownership import DockerInspectError as _DockerInspectError
from ._docker_ownership import DockerListingError as _DockerListingError
from ._docker_ownership import InspectOwnership as _InspectOwnership
from ._docker_ownership import parse_inspect_output as _parse_inspect_output
from ._docker_ownership import parse_ps_all_output as _parse_ps_all_output
from ._git_safety import GIT_TIMEOUT_SECONDS, GitSafetyError, GitSafetyFailure, ObjectFormat, run_git_bounded
from ._lifecycle_fs import (
    LifecycleFsError,
    LifecycleFsFailure,
    _assert_cloexec,
    _cloexec_flag,
    _directory_flag,
    _dominant_cleanup,
    _nofollow_flag,
    canonical_json_dumps,
    close_confirmed,
    fsync_fd,
    list_directory_entries,
    open_existing_directory_chain_if_present,
    open_managed_directory_chain,
    open_private_create_exclusive_at,
    validate_hex32,
    validate_safe_owned_directory_stat,
    write_all_eintr_safe,
)
from .abandonment import (
    ABANDONMENT_TEMP_MAX,
    MARKER_FILENAME,
    MARKER_TEMP_RE,
    RESOURCE_FIELDS,
    AbandonmentDisposition,
    AbandonmentMarkerError,
    AbandonmentMarkerFailure,
    count_stale_marker_temps,
    load_abandonment_marker,
)
from .checkpoint_ref import LIFECYCLE_ID_RE, CheckpointRef, CheckpointRefError, CheckpointRefFailure, MutationOutcome
from .checkpoint_session import ABSENT_TRANSITION, CheckpointIntent, CheckpointTransition
from .container_lifecycle import (
    CONTAINER_LABEL_ID,
    CONTAINER_LABEL_ROLE,
    CONTAINER_LABEL_SCHEMA,
    CONTAINER_LABEL_STATE_ROOT_ID,
    ContainerRole,
)
from .container_lifecycle import labels_match as _shared_labels_match
from .lifecycle_store import (
    LIFECYCLE_JSON_FILENAME,
    RUN_ID_MAX_ENCODED_BYTES,
    RUNS_DIRNAME,
    ContainerAttribution,
    ContainerIntent,
    LifecycleProjection,
    LifecycleState,
    LifecycleStoreError,
    LifecycleStoreFailure,
    WorktreeIntent,
    WorktreeTransition,
    _publish_projection_state,
    _publish_reconciler_checkpoint_ref_transition,
    _publish_reconciler_container_transition,
    _publish_reconciler_worktree_transition,
    checkpoint_ref_deletion_candidates,
    is_projection_checkpoint_ref_reconciliation_shape,
    is_projection_materialized_worktree_reconciliation_shape,
    is_projection_worktree_then_checkpoint_ref_reconciliation_shape,
    is_projection_fully_absent_shape,
    is_projection_reconciliation_eligible_shape,
    load_lifecycle_projection,
)
from .repo_identity import RepositoryIdentity, TrustedRepositoryContext
from .state_root import AbandonedLeafObservation, LeafRemovalKind, MaterializedLeafObservation, RmdirReport
from .state_locks import LockError, LockFailure, LockHandle, LockKind, LockScope, acquire_lifecycle_lock

MAINTENANCE_DIRNAME = "maintenance"

# ADR 0004 Amendment 2 section 4's fixed bounds. `run_id` and the
# categorical `detail` string reuse existing bounds
# (`RUN_ID_MAX_ENCODED_BYTES`, the ADR's own 512-byte sanitized-detail
# bound) rather than inventing new ones for the same kind of value.
_DETAIL_MAX_BYTES = 512
_CONTAINER_ID_MAX_BYTES = 128
_REF_NAME_MAX_BYTES = 128
MAINTENANCE_EVENT_MAX_BYTES = 4096

_TEMP_LEFTOVER_RE = re.compile(r"^\.lifecycle\.json\.tmp-[0-9a-f]{16}$")
_RUN_ENTRY_NAME_RE = LIFECYCLE_ID_RE

_DOCKER_TIMEOUT_SECONDS = 30.0
# Generous but bounded: a huge or hostile listing must never be
# unboundedly captured into memory, but never silently truncated
# either — overflow is a confirmed-termination failure, not a partial
# read a caller might mistake for the complete picture.
_DOCKER_OUTPUT_MAX_BYTES = 1_048_576
_WORKTREE_LISTING_MAX_BYTES = 1_048_576
# One ownership-proof `docker inspect` result (id, name, and a JSON
# labels object) is small; 16 KiB is generous but still a real bound --
# a hostile or malformed `Config.Labels` payload is confirmed-terminated
# on overflow rather than unboundedly captured.
_INSPECT_OWNERSHIP_MAX_BYTES = 16 * 1024

# ADR 0004 Amendment 12: the bounded, read-only Git admin-directory scan.
# Only entry names are read (never stat'd, opened, or followed). Git names
# a linked worktree's admin directory after the worktree directory's own
# name, appending decimal digits on a collision.
_ADMIN_SCAN_MAX_ENTRIES = 4096
_ADMIN_SCAN_MAX_NAME_BYTES = 262_144
_ADMIN_NAME_RE = re.compile(rb"^(?P<id>[0-9a-f]{32})(?P<suffix>[0-9]+)?$")

# `_CONTAINER_NAME_RE`/`_CONTAINER_ID_HEX_RE`/`CONTAINER_LABEL_*` are no
# longer defined here (Slice 3B-6): they are now `_docker_ownership.py`'s
# and `container_lifecycle.py`'s own canonical definitions, imported
# above under these exact same names for source compatibility with this
# module's own call sites and with existing tests that reference them
# via `reconciliation.CONTAINER_LABEL_*`.


@unique
class MaintenanceTrigger(str, Enum):
    """ADR 0004 section 12 / Amendment 18: why a maintenance trace exists."""

    PRE_RUN = "pre_run"
    EXPLICIT = "explicit"


@unique
class ReconciliationEntryOutcome(str, Enum):
    RECONCILED = "reconciled"
    SKIPPED_TERMINAL = "skipped_terminal"
    SKIPPED_ACTIVE = "skipped_active"
    REFUSED = "refused"
    FAILED = "failed"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    # ADR 0004 section 10 / Amendment 18: a valid final abandonment
    # marker; zero lock, Docker, or Git calls, and never blocking.
    SKIPPED_ABANDONED = "skipped_abandoned"
    SKIPPED_ABANDONED_UNRESOLVED = "skipped_abandoned_unresolved"


@dataclass(frozen=True)
class ReconciliationEntryResult:
    lifecycle_id: str
    outcome: ReconciliationEntryOutcome
    detail: str
    run_id: str | None = None
    attempt_number: int | None = None
    baseline_confirmed_absent: bool = False
    verification_confirmed_absent: bool = False
    worktree_confirmed_absent: bool = False
    checkpoint_ref_confirmed_absent: bool = False
    has_temp_leftover: bool = False
    # The live container id this pass positively observed for each role
    # (Slice 3B-5) — retained even after a successful removal (the
    # maintenance trace is retrospective, never write-ahead: ADR 0004
    # Amendment 2 section 4), `None` when the role was already absent or
    # no well-formed candidate was ever positively observed.
    baseline_id: str | None = None
    verification_id: str | None = None
    # ADR 0004 Amendment 12 (maintenance-trace `worktree` fields).
    # `initial_persisted_intent` comes only from the locked authoritative
    # re-read -- `None` when the pass stopped before it (never the pre-lock
    # peek). `absent_transition_confirmed_this_pass` is true only when this
    # pass itself got confirmed success from the reconciler-owned
    # `creating -> absent` publication.
    worktree_initial_persisted_intent: str | None = None
    worktree_leaf_outcome: str = "not_inspected"
    worktree_removal_observation: str | None = None
    worktree_absent_transition_confirmed_this_pass: bool = False
    # ADR 0004 Amendment 13: pre-/post-command evidence for a materialized
    # worktree. `None` means "not observed in this pass" -- never invented.
    worktree_registration_pre: dict | None = None
    worktree_registration_post: dict | None = None
    worktree_admin_matches_pre: int | str | None = None
    worktree_admin_matches_post: int | str | None = None
    worktree_leaf_pre: str | None = None
    worktree_leaf_post: str | None = None
    worktree_removal_attempt: str = "not_attempted"
    worktree_disposing_transition_confirmed_this_pass: bool = False
    # ADR 0004 Amendment 16 (maintenance-trace `checkpoint_ref` fields):
    # categorical only -- never a SHA. `None` means "not observed in this
    # pass", never invented.
    checkpoint_ref_initial_persisted_intent: str | None = None
    checkpoint_ref_observation_pre: str | None = None
    checkpoint_ref_candidate_role: str | None = None
    checkpoint_ref_removal_attempt: str = "not_attempted"
    checkpoint_ref_removing_transition_confirmed_this_pass: bool = False
    checkpoint_ref_absent_transition_confirmed_this_pass: bool = False
    # ADR 0004 Amendment 17 (categorical only). `gate_observation` is the
    # chained row's read-only pre-worktree ref gate (`None` elsewhere);
    # `container_gate` is the listing that immediately gates the ref phase
    # (the chained row's second listing, or Amendment 16's single listing).
    checkpoint_ref_gate_observation: str | None = None
    checkpoint_ref_container_gate: str = "not_attempted"
    # ADR 0004 Amendment 18. The disposition and categorical summary of a
    # valid final marker -- never the operator's reason, which is persisted
    # only in abandonment.json. `abandonment_temp_leftovers` counts
    # recognized stale temporary marker files (stops at 17; 0 whenever a
    # final marker decided the entry).
    abandonment_disposition: str | None = None
    abandonment_remaining: dict | None = None
    abandonment_temp_leftovers: int = 0


@dataclass(frozen=True)
class ReconciliationPassResult:
    maintenance_id: str
    entries: tuple[ReconciliationEntryResult, ...]
    blocked: bool


@unique
class ReconciliationFailure(str, Enum):
    # A positively observed inconsistency (malformed name, symlink,
    # wrong type/owner/permissions at the shared `runs/` level, or the
    # maintenance directory/file itself in an inconsistent state) — I7.
    REFUSED = "refused"
    # A genuine inability to inspect or durably record the pass itself
    # (a syscall failure, not an observed wrong state) — I6.
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    # The caller's repository_lock does not match identity.repo_key, or
    # is not actually held.
    WRONG_LOCK_SCOPE = "wrong_lock_scope"


class ReconciliationError(Exception):
    """A whole-pass failure: `runs/` or `maintenance/` themselves are
    unusable, or the caller's locking precondition does not hold.
    `reason` is the stable, matchable identifier; `message` is fixed,
    sanitized categorical text only."""

    def __init__(self, reason: ReconciliationFailure, message: str, *, maintenance_id: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        # ADR 0004 Amendment 18: set when a maintenance-trace file for this
        # pass already exists on disk (so callers report its presence
        # truthfully); None means no trace file was created.
        self.maintenance_id = maintenance_id


def _require_repository_lock_scope(repository_lock: LockHandle, repo_key: str) -> None:
    expected = LockScope(kind=LockKind.REPOSITORY, repo_key=repo_key)
    if not repository_lock.is_held or repository_lock.scope != expected:
        raise ReconciliationError(
            ReconciliationFailure.WRONG_LOCK_SCOPE,
            "reconciliation requires the exact matching repository lock to already be held",
        )


# `LifecycleFsFailure` reasons that indicate a genuine inability to
# inspect or open something (a syscall failure), as distinct from a
# positively observed wrong state (a symlink, wrong type, or unsafe
# permissions) — I6 vs I7. Shared by every fd-relative open/list call
# site in this module so the same distinction is never redrawn
# ad hoc at each site.
_SUBSTRATE_FAILURE_REASONS = (LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, LifecycleFsFailure.CLEANUP_UNCONFIRMED)


def _classify_pass_level_fs_failure(exc: LifecycleFsError) -> ReconciliationFailure:
    if exc.reason in _SUBSTRATE_FAILURE_REASONS:
        return ReconciliationFailure.SUBSTRATE_UNAVAILABLE
    return ReconciliationFailure.REFUSED


def _classify_entry_fs_failure(exc: LifecycleFsError) -> ReconciliationEntryOutcome:
    if exc.reason in _SUBSTRATE_FAILURE_REASONS:
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    return ReconciliationEntryOutcome.REFUSED


# `_DockerListingError`, `_DockerInspectError`, and `_InspectOwnership`
# are no longer defined here (Slice 3B-6) -- they are `_docker_ownership.
# py`'s own canonical types, imported above under these exact same names
# so this module's own call sites, and existing tests that construct or
# monkeypatch them via `reconciliation._InspectOwnership`/
# `reconciliation._DockerListingError`/`reconciliation._DockerInspectError`,
# keep working unchanged.


def _docker_ps_all_id_name_pairs() -> tuple[dict[str, str], dict[str, str]]:
    """Still issues its own `run_bounded_stdout` call, bound to this
    module's own existing timeout/bound constants, so existing tests
    that monkeypatch `reconciliation.run_bounded_stdout` directly
    continue to intercept every Docker call this module makes
    unchanged; only the strict output parsing is now delegated to the
    shared `_docker_ownership.parse_ps_all_output` (Slice 3B-6 —
    generalizes what was previously this module's own private parser,
    a byte-for-byte identical duplicate of `executor.py`'s and
    `reconciliation.py`'s own prior copies)."""
    argv = ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"]
    try:
        result = run_bounded_stdout(
            argv, timeout_seconds=_DOCKER_TIMEOUT_SECONDS, stdout_limit=_DOCKER_OUTPUT_MAX_BYTES
        )
    except BoundedProcessError as exc:
        raise _DockerListingError("docker listing failed, timed out, or exceeded its output bound") from exc
    if result.returncode != 0:
        raise _DockerListingError("docker listing exited with a nonzero status")
    return _parse_ps_all_output(result.stdout)


def _docker_inspect_ownership(candidate_id: str) -> _InspectOwnership:
    """Still issues its own `run_bounded_stdout` call (same rationale as
    `_docker_ps_all_id_name_pairs` above); only the strict output
    parsing is delegated to the shared `_docker_ownership.
    parse_inspect_output` (Slice 3B-6)."""
    argv = [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        '{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{json .Config.Labels}}',
        candidate_id,
    ]
    try:
        result = run_bounded_stdout(
            argv, timeout_seconds=_DOCKER_TIMEOUT_SECONDS, stdout_limit=_INSPECT_OWNERSHIP_MAX_BYTES
        )
    except BoundedProcessError as exc:
        raise _DockerInspectError("docker inspect failed, timed out, or exceeded its output bound") from exc
    if result.returncode != 0:
        raise _DockerInspectError("docker inspect could not confirm the candidate container")
    return _parse_inspect_output(result.stdout)


def _labels_match(labels: dict[str, str], *, state_root_id: str, lifecycle_id: str, role: str) -> bool:
    """Thin delegator to `container_lifecycle.labels_match` (Slice
    3B-6): ADR 0004 section 7's exactly-four-required-labels ownership
    check. Extra, unrecognized labels are always ignored; every one of
    the four required labels must be present with the exact expected
    value."""
    return _shared_labels_match(
        labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=ContainerRole(role)
    )


@unique
class _ContainerDecisionOutcome(str, Enum):
    NOOP = "noop"
    CONFIRMED_ABSENT = "confirmed_absent"
    OWNED_REMOVE = "owned_remove"
    REFUSED = "refused"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"


@dataclass(frozen=True)
class _ContainerDecision:
    """`removal_id` and `observed_id` are deliberately separate fields
    (correction pass finding 1) -- they are not always the same value,
    and conflating them let an unobserved persisted id leak into the
    maintenance trace as if it were positively confirmed evidence.

    `removal_id` is set only when a write targeting that id is
    authorized: `OWNED_REMOVE` (removal is authorized) or
    `CONFIRMED_ABSENT` with `direct_to_absent=False` (the persisted id
    must still be carried through the `PRESENT/REMOVING -> removing ->
    absent` write-ahead path even though nothing is live, since no
    direct `present -> absent` edge exists). It is never set on
    `REFUSED`/`SUBSTRATE_UNAVAILABLE`/`NOOP`, and the write-ahead loop
    never removes or writes using anything but this field.

    `observed_id` is the safely parsed candidate actually, positively
    observed live for this role during classification -- for
    retrospective trace-evidence purposes only, never for a write. It
    is `None` whenever nothing was confirmed live for this role
    (already absent, or persisted-but-now-confirmed-absent)."""

    outcome: _ContainerDecisionOutcome
    detail: str
    removal_id: str | None = None
    observed_id: str | None = None
    direct_to_absent: bool = False
    live_present: bool = False


def _classify_container(
    *,
    attribution: ContainerAttribution,
    role: str,
    expected_name: str,
    name_to_id: dict[str, str],
    id_to_name: dict[str, str],
    state_root_id: str,
    lifecycle_id: str,
) -> _ContainerDecision:
    """ADR 0004 section 7's persisted-combination table, per role.
    Performs an ownership-proof `docker inspect` only for a candidate
    the listing itself says occupies the recomputed name or the
    persisted id — never trusts the listing's own name/id pairing as
    ownership proof by itself (I2)."""
    live_id_at_name = name_to_id.get(expected_name)

    if attribution.intent is ContainerIntent.ABSENT:
        if live_id_at_name is None:
            return _ContainerDecision(_ContainerDecisionOutcome.NOOP, "already absent and confirmed absent")
        return _ContainerDecision(
            _ContainerDecisionOutcome.REFUSED,
            "a container exists at the recomputed name for a persisted-absent role",
            observed_id=live_id_at_name,
        )

    if attribution.intent is ContainerIntent.CREATING:
        if live_id_at_name is None:
            return _ContainerDecision(
                _ContainerDecisionOutcome.CONFIRMED_ABSENT,
                "no container exists at the recomputed name",
                direct_to_absent=True,
            )
        try:
            proof = _docker_inspect_ownership(live_id_at_name)
        except _DockerInspectError:
            return _ContainerDecision(
                _ContainerDecisionOutcome.SUBSTRATE_UNAVAILABLE,
                "the candidate container could not be inspected",
                observed_id=live_id_at_name,
            )
        if proof.id != live_id_at_name or proof.name != expected_name:
            return _ContainerDecision(
                _ContainerDecisionOutcome.REFUSED,
                "the candidate's own inspected identity disagrees with the listing",
                observed_id=live_id_at_name,
            )
        if not _labels_match(proof.labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role):
            return _ContainerDecision(
                _ContainerDecisionOutcome.REFUSED,
                "a container exists at the recomputed name but its labels do not prove ownership",
                observed_id=live_id_at_name,
            )
        return _ContainerDecision(
            _ContainerDecisionOutcome.OWNED_REMOVE,
            "an owned container was observed for a creating role",
            removal_id=live_id_at_name,
            observed_id=live_id_at_name,
            live_present=True,
        )

    # PRESENT or REMOVING: a persisted id always exists for these intents.
    persisted_id = attribution.id
    assert persisted_id is not None
    live_name_of_persisted_id = id_to_name.get(persisted_id)

    if live_id_at_name is None and live_name_of_persisted_id is None:
        return _ContainerDecision(
            _ContainerDecisionOutcome.CONFIRMED_ABSENT,
            "neither the recomputed name nor the persisted id appears live",
            removal_id=persisted_id,  # write-ahead absent-recovery target, not positively observed
            live_present=False,
        )

    if live_id_at_name == persisted_id and live_name_of_persisted_id == expected_name:
        try:
            proof = _docker_inspect_ownership(persisted_id)
        except _DockerInspectError:
            return _ContainerDecision(
                _ContainerDecisionOutcome.SUBSTRATE_UNAVAILABLE,
                "the owned candidate container could not be inspected",
                observed_id=persisted_id,
            )
        if proof.id != persisted_id or proof.name != expected_name:
            return _ContainerDecision(
                _ContainerDecisionOutcome.REFUSED,
                "the candidate's own inspected identity disagrees with the listing",
                observed_id=persisted_id,
            )
        if not _labels_match(proof.labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role):
            return _ContainerDecision(
                _ContainerDecisionOutcome.REFUSED,
                "the owned candidate's labels do not prove ownership",
                observed_id=persisted_id,
            )
        return _ContainerDecision(
            _ContainerDecisionOutcome.OWNED_REMOVE,
            "an owned container was observed for a present/removing role",
            removal_id=persisted_id,
            observed_id=persisted_id,
            live_present=True,
        )

    # Neither the matched-pair nor the fully-absent shape: some kind of
    # ambiguity. At least one of the two positively exists live here
    # (the fully-absent case was already returned above), so exactly
    # one of the three sub-cases below always applies. Reported
    # evidence prefers whatever occupies the recomputed name itself
    # (this role's own identity is the more directly relevant conflict
    # evidence); the persisted id is reported only when it is live
    # exclusively under a different name. Never fabricated, never the
    # unobserved `persisted_id` alone.
    if live_id_at_name is not None:
        observed_id = live_id_at_name
    else:
        observed_id = persisted_id  # live_name_of_persisted_id is not None here
    return _ContainerDecision(
        _ContainerDecisionOutcome.REFUSED,
        "the persisted id and the recomputed name disagree about which container currently exists",
        observed_id=observed_id,
    )


@unique
class _DockerRemovalOutcome(str, Enum):
    CONFIRMED_ABSENT = "confirmed_absent"
    STILL_PRESENT = "still_present"
    CONFLICT = "conflict"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"


def _remove_and_confirm_absent(
    *,
    removal_id: str,
    expected_name: str,
    role: str,
    state_root_id: str,
    lifecycle_id: str,
    attempt_rm: bool,
) -> tuple[_DockerRemovalOutcome, str]:
    """Remove-by-immutable-id, then always attempt a fresh, independent,
    strict listing regardless of the removal attempt's own outcome
    (launch failure, timeout, overflow, nonzero exit, or unconfirmed
    termination included) — `docker rm`'s own result is never
    authoritative for removal; only this fresh observation is. When
    `attempt_rm` is false (the container was already confirmed absent
    by an earlier observation this same pass; nothing needs removing),
    the fresh listing is still performed, both for genuine defense in
    depth and because it is this function's sole source of truth.

    A listing that still shows the exact owned id/name pair live is not
    by itself enough to conclude `STILL_PRESENT`: the listing alone
    cannot prove ownership (I2), so the surviving candidate is
    re-inspected by immutable id, exactly like the original
    classification did, before this function will report anything
    beyond absence. A listing-level pairing disagreement (the id
    appears under a different name than expected, or a different id
    occupies the expected name) is `CONFLICT` without an inspect — the
    listing itself already disproves ownership continuity. A
    well-formed re-inspect that still proves ownership is
    `STILL_PRESENT`; a well-formed re-inspect whose identity or labels
    now disagree is `CONFLICT`; a failed or malformed re-inspect is
    `SUBSTRATE_UNAVAILABLE`."""
    if attempt_rm:
        try:
            run_bounded_stdout(
                ["docker", "rm", "--force", removal_id],
                timeout_seconds=_DOCKER_TIMEOUT_SECONDS,
                stdout_limit=_DOCKER_OUTPUT_MAX_BYTES,
            )
        except BoundedProcessError:
            pass

    try:
        name_to_id, id_to_name = _docker_ps_all_id_name_pairs()
    except _DockerListingError:
        return _DockerRemovalOutcome.SUBSTRATE_UNAVAILABLE, "the post-removal listing failed"

    live_id_at_name = name_to_id.get(expected_name)
    live_name_of_id = id_to_name.get(removal_id)
    if live_id_at_name is None and live_name_of_id is None:
        return _DockerRemovalOutcome.CONFIRMED_ABSENT, "confirmed absent by a fresh independent listing"
    if not (live_id_at_name == removal_id and live_name_of_id == expected_name):
        return (
            _DockerRemovalOutcome.CONFLICT,
            "the post-removal listing disagrees about which container currently exists",
        )

    try:
        proof = _docker_inspect_ownership(removal_id)
    except _DockerInspectError:
        return _DockerRemovalOutcome.SUBSTRATE_UNAVAILABLE, "the still-present candidate could not be re-inspected"
    if proof.id != removal_id or proof.name != expected_name:
        return _DockerRemovalOutcome.CONFLICT, "the re-inspected candidate's own identity disagrees with the listing"
    if not _labels_match(proof.labels, state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role):
        return _DockerRemovalOutcome.CONFLICT, "the re-inspected candidate's labels no longer prove ownership"
    return _DockerRemovalOutcome.STILL_PRESENT, "the owned container is still present after removal"


class _GitWorktreeListingError(Exception):
    pass


class _WorktreeListingError(_GitWorktreeListingError):
    """The bounded listing failed or timed out, or its output is malformed.
    Both are inspection failures (`SUBSTRATE_UNAVAILABLE`)."""


_HEX_RE = re.compile(rb"^[0-9a-f]+$")


@dataclass(frozen=True)
class _WorktreeRecord:
    path: str
    head: str | None  # None only for a bare record
    kind: str  # "branch" | "detached" | "bare"
    branch: str | None
    locked: bool
    prunable: bool


def _run_worktree_listing(working_tree_root: str) -> bytes:
    """One bounded, timeout-controlled, no-shell, no-hooks
    `git worktree list --porcelain -z` through the shared hardened
    `run_git_bounded` seam. `-z` NUL-delimits every field, so a registered
    path containing a newline cannot be misparsed."""
    try:
        result = run_git_bounded(
            working_tree_root,
            "worktree",
            "list",
            "--porcelain",
            "-z",
            limit=_WORKTREE_LISTING_MAX_BYTES,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except GitSafetyError as exc:
        raise _WorktreeListingError("git worktree listing failed, timed out, or exceeded its output bound") from exc
    return result.stdout


def _parse_worktree_listing(stdout: bytes, *, oid_hex_len: int | None) -> tuple[_WorktreeRecord, ...]:
    """Structural parser for `git worktree list --porcelain -z` (ADR 0004
    Amendment 13). Each field is `key` or `key SP value`, NUL-terminated; a
    record ends with an empty field. Validates every record and field but
    keeps structurally valid duplicate paths: target-aware duplicate
    analysis belongs to `_analyze_worktree_listing`. `oid_hex_len=None`
    accepts either object-format length (legacy callers). Any violation
    raises `_WorktreeListingError` -- the listing is never partially
    trusted."""

    def malformed() -> _WorktreeListingError:
        return _WorktreeListingError("git worktree listing is malformed")

    if not stdout or not stdout.endswith(b"\x00"):
        raise malformed()
    fields = stdout[:-1].split(b"\x00")
    if not fields or fields[-1] != b"":
        raise malformed()

    records: list[_WorktreeRecord] = []
    current: list[bytes] = []
    for field in fields:
        if field != b"":
            current.append(field)
            continue
        if not current:
            raise malformed()  # a stray empty field
        records.append(_parse_worktree_record(current, oid_hex_len=oid_hex_len, malformed=malformed))
        current = []
    if current or not records:
        raise malformed()
    return tuple(records)


def _parse_worktree_record(fields: list[bytes], *, oid_hex_len: int | None, malformed) -> _WorktreeRecord:
    seen: dict[bytes, bytes | None] = {}
    for index, field in enumerate(fields):
        key, sep, value = field.partition(b" ")
        if key in seen:
            raise malformed()
        if key == b"worktree":
            if index != 0 or not sep or not value.startswith(b"/"):
                raise malformed()
        elif key in (b"HEAD", b"branch"):
            if not sep or not value:
                raise malformed()
        elif key in (b"detached", b"bare"):
            if sep:
                raise malformed()
        elif key in (b"locked", b"prunable"):
            pass  # with or without a reason; the reason is never retained
        else:
            raise malformed()  # unknown key
        seen[key] = value if sep else None
    if b"worktree" not in seen:
        raise malformed()
    kinds = [k for k in (b"branch", b"detached", b"bare") if k in seen]
    if len(kinds) != 1:
        raise malformed()
    kind = kinds[0].decode()
    head = seen.get(b"HEAD")
    if kind == "bare":
        if head is not None:
            raise malformed()
    else:
        if head is None or not _HEX_RE.fullmatch(head):
            raise malformed()
        if oid_hex_len is None:
            if len(head) not in (40, 64):
                raise malformed()
        elif len(head) != oid_hex_len:
            raise malformed()
    branch = seen.get(b"branch")
    return _WorktreeRecord(
        path=os.path.normpath(os.fsdecode(seen[b"worktree"])),
        head=head.decode() if head is not None else None,
        kind=kind,
        branch=os.fsdecode(branch) if branch is not None else None,
        locked=b"locked" in seen,
        prunable=b"prunable" in seen,
    )


@dataclass(frozen=True)
class _TargetRegistration:
    """`state` is "absent" (0 target records), "present" (1 or "many"), or
    "unknown" (listing failure; every other field None). `target_bare` is
    set iff exactly one target record exists; `locked`/`prunable` iff
    exactly one NON-bare target record exists."""

    state: str
    target_records: int | str | None
    target_bare: bool | None
    locked: bool | None
    prunable: bool | None

    def to_trace(self) -> dict:
        return {
            "state": self.state,
            "target_records": self.target_records,
            "target_bare": self.target_bare,
            "locked": self.locked,
            "prunable": self.prunable,
        }


_UNKNOWN_REGISTRATION = _TargetRegistration("unknown", None, None, None, None)


@dataclass(frozen=True)
class _WorktreeListingAnalysis:
    registration: _TargetRegistration


def _analyze_worktree_listing(records: tuple[_WorktreeRecord, ...], *, target_path: str) -> _WorktreeListingAnalysis:
    """Target-aware analysis: duplicate normalized NON-target paths make the
    listing malformed (`_WorktreeListingError`); records matching the
    deterministic target are counted 0 / 1 / "many" and returned as data
    -- "many" is positive ambiguity the caller refuses."""
    target = os.path.normpath(target_path)
    seen: set[str] = set()
    matches = [r for r in records if r.path == target]
    for record in records:
        if record.path == target:
            continue
        if record.path in seen:
            raise _WorktreeListingError("git worktree listing repeats a path")
        seen.add(record.path)
    if not matches:
        return _WorktreeListingAnalysis(_TargetRegistration("absent", 0, None, None, None))
    if len(matches) > 1:
        return _WorktreeListingAnalysis(_TargetRegistration("present", "many", None, None, None))
    only = matches[0]
    if only.kind == "bare":
        return _WorktreeListingAnalysis(_TargetRegistration("present", 1, True, None, None))
    return _WorktreeListingAnalysis(_TargetRegistration("present", 1, False, only.locked, only.prunable))


def _observe_target_registration(
    working_tree_root: str, *, oid_hex_len: int, target_path: str, cleanup_failures: list[str] | None = None
) -> _TargetRegistration:
    """`cleanup_failures` (ADR 0004 Amendment 18, read-only inspection only):
    when given, a listing failure caused by CodeAgent's own unconfirmed
    process or descriptor cleanup is also recorded there, so it is never
    reduced to an ordinary "unknown" observation. Existing callers omit it."""
    try:
        records = _parse_worktree_listing(_run_worktree_listing(working_tree_root), oid_hex_len=oid_hex_len)
        return _analyze_worktree_listing(records, target_path=target_path).registration
    except _WorktreeListingError as exc:
        if cleanup_failures is not None and _is_own_cleanup_failure(exc):
            cleanup_failures.append("worktree_listing")
        return _UNKNOWN_REGISTRATION


def _worktree_registered_paths(working_tree_root: str) -> set[str]:
    """Legacy set of every registered path, built by the same structural
    parser. With no authorized target exception, ANY duplicate normalized
    path makes the listing malformed."""
    records = _parse_worktree_listing(_run_worktree_listing(working_tree_root), oid_hex_len=None)
    paths: set[str] = set()
    for record in records:
        if record.path in paths:
            raise _WorktreeListingError("git worktree listing repeats a path")
        paths.add(record.path)
    return paths


@unique
class _AdminScanResult(str, Enum):
    NONE_FOUND = "none_found"
    MATCH_FOUND = "match_found"
    UNEXPECTED_SUBSTRATE = "unexpected_substrate"
    LIMIT_EXCEEDED = "limit_exceeded"
    INSPECTION_FAILED = "inspection_failed"


class _AdminScanDiagnostic(Exception):
    """Private, categorical-only cause for an admin-scan cleanup failure:
    carries the scan result already decided, never names or paths."""

    def __init__(self, result: _AdminScanResult) -> None:
        super().__init__(result.value)
        self.result = result


def _scan_worktree_admin_entries(canonical_common_dir: str, lifecycle_id: str) -> _AdminScanResult:
    """Bounded, read-only scan of `<common-dir>/worktrees/` for an admin
    entry Git would have created for this lifecycle's worktree (ADR 0004
    Amendment 12). `git worktree list` does not report an admin directory
    whose `gitdir` file was never written, so registration absence alone
    does not prove Git holds nothing for this path.

    Every locally opened descriptor is closed exactly once; a close
    failure raises `LifecycleFsError(CLEANUP_UNCONFIRMED)` (chained from a
    categorical diagnostic of the result already decided) and dominates
    every result. Never reads admin-file contents, never stats or opens an
    entry, never follows a symlink, never deletes anything."""
    opened: list[int] = []
    try:
        result = _scan_admin_entries_into(opened, canonical_common_dir, lifecycle_id)
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise
    _dominant_cleanup(opened, _AdminScanDiagnostic(result))
    return result


def _count_worktree_admin_entries(canonical_common_dir: str, lifecycle_id: str) -> int | str:
    """ADR 0004 Amendment 13: the same bounded, names-only, close-once scan,
    counting every admin name for this lifecycle instead of stopping at
    the first. Returns 0, 1, "many", or "unknown" (limit exceeded, an
    inspection failure, or a `worktrees/` that is not a real directory).
    A descriptor-close failure raises `LifecycleFsError(CLEANUP_UNCONFIRMED)`
    and dominates, exactly as `_scan_worktree_admin_entries`."""
    opened: list[int] = []
    counter = [0]
    try:
        result = _scan_admin_entries_into(opened, canonical_common_dir, lifecycle_id, counter=counter)
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise
    _dominant_cleanup(opened, _AdminScanDiagnostic(result))
    if result in (_AdminScanResult.NONE_FOUND, _AdminScanResult.MATCH_FOUND):
        return counter[0] if counter[0] < 2 else "many"
    return "unknown"


def _scan_admin_entries_into(
    opened: list[int], canonical_common_dir: str, lifecycle_id: str, *, counter: list[int] | None = None
) -> _AdminScanResult:
    flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
    try:
        common_fd = os.open(canonical_common_dir, flags)
    except OSError:
        return _AdminScanResult.INSPECTION_FAILED
    opened.append(common_fd)
    # A failed CLOEXEC check propagates (SUBSTRATE_UNAVAILABLE); the caller
    # closes every opened descriptor once, chained from that exact error.
    _assert_cloexec(common_fd)
    try:
        worktrees_fd = os.open("worktrees", flags, dir_fd=common_fd)
    except FileNotFoundError:
        return _AdminScanResult.NONE_FOUND  # Git creates worktrees/ lazily
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return _AdminScanResult.UNEXPECTED_SUBSTRATE
        return _AdminScanResult.INSPECTION_FAILED
    opened.append(worktrees_fd)
    _assert_cloexec(worktrees_fd)

    target = lifecycle_id.encode("ascii")
    entries = 0
    name_bytes = 0
    try:
        with os.scandir(worktrees_fd) as it:
            for entry in it:
                name = os.fsencode(entry.name)
                # Bounds are enforced before any name is interpreted, so no
                # entry -- matching or not -- is ever considered past them.
                entries += 1
                name_bytes += len(name)
                if entries > _ADMIN_SCAN_MAX_ENTRIES or name_bytes > _ADMIN_SCAN_MAX_NAME_BYTES:
                    return _AdminScanResult.LIMIT_EXCEEDED
                match = _ADMIN_NAME_RE.fullmatch(name)
                if match is not None and match["id"] == target:
                    if counter is None:
                        return _AdminScanResult.MATCH_FOUND
                    counter[0] += 1
    except OSError:
        return _AdminScanResult.INSPECTION_FAILED
    if counter is not None and counter[0]:
        return _AdminScanResult.MATCH_FOUND
    return _AdminScanResult.NONE_FOUND


_ADMIN_SCAN_OUTCOMES = {
    _AdminScanResult.MATCH_FOUND: (ReconciliationEntryOutcome.REFUSED, "a git worktree admin entry exists for this lifecycle"),
    _AdminScanResult.UNEXPECTED_SUBSTRATE: (
        ReconciliationEntryOutcome.REFUSED,
        "the git worktree admin directory is not a real directory",
    ),
    _AdminScanResult.LIMIT_EXCEEDED: (
        ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
        "the git worktree admin directory exceeded its scan bound",
    ),
    _AdminScanResult.INSPECTION_FAILED: (
        ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
        "the git worktree admin directory could not be inspected",
    ),
}


def _check_worktree_unregistered(
    *, lifecycle_id: str, expected_path: str, identity: RepositoryIdentity, context: TrustedRepositoryContext
) -> tuple[ReconciliationEntryOutcome | None, str | None]:
    """Fresh bounded Git listing plus bounded admin scan: `(None, None)`
    only when the exact path is unregistered and no admin entry exists."""
    try:
        registered_paths = _worktree_registered_paths(context.working_tree_root)
    except _GitWorktreeListingError:
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "worktree listing failed"
    if expected_path in registered_paths:
        return ReconciliationEntryOutcome.REFUSED, "the recomputed worktree is registered"
    try:
        scan = _scan_worktree_admin_entries(identity.canonical_common_dir, lifecycle_id)
    except LifecycleFsError as exc:
        return _classify_entry_fs_failure(exc), "the git worktree admin scan could not be completed"
    if scan is _AdminScanResult.NONE_FOUND:
        return None, None
    return _ADMIN_SCAN_OUTCOMES[scan]


def _enumerate_runs(runs_fd: int) -> list[str]:
    """Prevalidate the complete `runs/` namespace, sorted, before any
    legitimate entry is touched. A positively observed malformed name,
    symlink, wrong type, wrong owner, or unsafe permissions is
    `REFUSED` and aborts the whole pass; a genuine inspection failure
    is `SUBSTRATE_UNAVAILABLE` and also aborts the whole pass — both
    before any legitimate entry is opened, locked, or mutated."""
    try:
        names = list_directory_entries(runs_fd)
    except LifecycleFsError as exc:
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the runs/ namespace could not be listed"
        ) from exc

    sorted_names = sorted(names)
    for name in sorted_names:
        if not _RUN_ENTRY_NAME_RE.fullmatch(name):
            raise ReconciliationError(
                ReconciliationFailure.REFUSED, "an unrecognized entry exists directly beneath runs/"
            )
        try:
            st = os.lstat(name, dir_fd=runs_fd)
        except OSError as exc:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "a runs/ entry could not be inspected"
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise ReconciliationError(ReconciliationFailure.REFUSED, "a runs/ entry is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ReconciliationError(ReconciliationFailure.REFUSED, "a runs/ entry is not a directory")
        try:
            validate_safe_owned_directory_stat(st)
        except LifecycleFsError as exc:
            raise ReconciliationError(
                ReconciliationFailure.REFUSED, "a runs/ entry has unsafe ownership or permissions"
            ) from exc
    return sorted_names


@dataclass(frozen=True)
class _InnerEntriesCheck:
    has_temp_leftover: bool
    outcome: ReconciliationEntryOutcome | None
    detail: str | None


def _validate_recognized_inner_entry(run_dir_fd: int, name: str) -> tuple[ReconciliationEntryOutcome | None, str | None]:
    """fd-relative, no-follow inspection of one recognized inner-entry
    name (`lifecycle.json`, `lifecycle.lock`, or a recognized temp-
    publication leftover): every one of these is created as a private
    regular file (mode 0600, no group/other bits), so a symlink, a
    non-regular type, a foreign owner, or a widened permission bit at
    that name is a positively observed inconsistency (`REFUSED`),
    never trusted merely because its name matched. A genuine inability
    to inspect the entry is `SUBSTRATE_UNAVAILABLE`. `lifecycle.json`
    and `lifecycle.lock` are still fully, independently validated by
    their own consumers afterward (`load_lifecycle_projection`,
    `acquire_lifecycle_lock`) — this check exists because a recognized
    clean-final entry is classified `SKIPPED_TERMINAL` before either of
    those consumers ever runs, so nothing else would otherwise inspect
    a hostile `lifecycle.lock` sitting next to it."""
    try:
        st = os.lstat(name, dir_fd=run_dir_fd)
    except OSError:
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, f"{name} could not be inspected"
    if stat.S_ISLNK(st.st_mode):
        return ReconciliationEntryOutcome.REFUSED, f"{name} is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return ReconciliationEntryOutcome.REFUSED, f"{name} is not a regular file"
    if st.st_uid != os.getuid():
        return ReconciliationEntryOutcome.REFUSED, f"{name} is not owned by the current user"
    if st.st_mode & 0o077:
        return ReconciliationEntryOutcome.REFUSED, f"{name} has unsafe permissions"
    return None, None


def _check_inner_entries(run_dir_fd: int, names: list[str] | None = None) -> _InnerEntriesCheck:
    """Enumerate and validate a validated run directory's own
    contents. Only `lifecycle.json`, `lifecycle.lock`, and the exact
    recognized temp-publication pattern are ever recognized by name;
    anything else is an unrecognized inner entry (`REFUSED`), never
    opened or trusted. Every recognized name is additionally validated
    itself (see `_validate_recognized_inner_entry`) before being
    trusted. A recognized temp-publication leftover is never opened,
    trusted, or deleted, but its presence is reported via
    `has_temp_leftover` so the caller can carry it into the
    maintenance trace.

    `names` is the caller's single listing (ADR 0004 Amendment 18); when
    omitted the directory is listed here. Recognized abandonment temporary
    files are validated separately (`count_stale_marker_temps`) and are
    skipped here, never treated as unrecognized."""
    if names is None:
        try:
            names = list_directory_entries(run_dir_fd)
        except LifecycleFsError:
            return _InnerEntriesCheck(False, ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "run directory contents could not be listed")

    has_temp_leftover = False
    for name in names:
        if MARKER_TEMP_RE.fullmatch(name):
            continue
        is_temp_leftover = bool(_TEMP_LEFTOVER_RE.fullmatch(name))
        if name not in ("lifecycle.json", "lifecycle.lock") and not is_temp_leftover:
            return _InnerEntriesCheck(has_temp_leftover, ReconciliationEntryOutcome.REFUSED, "run directory contains an unrecognized inner entry")
        outcome, detail = _validate_recognized_inner_entry(run_dir_fd, name)
        if outcome is not None:
            return _InnerEntriesCheck(has_temp_leftover, outcome, detail)
        if is_temp_leftover:
            has_temp_leftover = True
    return _InnerEntriesCheck(has_temp_leftover, None, None)


def _classify_projection_load_failure(exc: LifecycleStoreError) -> ReconciliationEntryOutcome:
    if exc.reason is LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE:
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    return ReconciliationEntryOutcome.REFUSED


# The states a dead run's entry may legitimately be found in (Slice
# 3B-5, ADR 0004 Amendment 5) — every owner-writable state plus
# RECONCILING itself (a resumed pass). Must match
# `lifecycle_store._RECONCILER_ELIGIBLE_STATES` (an independent
# constant in that module, not imported, since the two modules'
# eligibility checks are each responsible for their own boundary).
_RECONCILER_ELIGIBLE_STATES = (
    LifecycleState.PREPARING,
    LifecycleState.ACTIVE,
    LifecycleState.CLEANING,
    LifecycleState.RECONCILING,
)


def _is_reconciliation_eligible(projection: LifecycleProjection) -> bool:
    """The one eligibility rule used by BOTH the pre-lock peek and the
    locked authoritative re-read (ADR 0004 Amendment 12): an owner-writable
    or RECONCILING state, with either the absent-worktree shape (existing;
    includes a resumed RECONCILING whose worktree was already collapsed) or
    the narrow `creating`-worktree shape. `present`/`disposing` worktrees
    are admitted by neither predicate."""
    return projection.state in _RECONCILER_ELIGIBLE_STATES and (
        is_projection_reconciliation_eligible_shape(projection)
        or is_projection_materialized_worktree_reconciliation_shape(projection)
        or is_projection_checkpoint_ref_reconciliation_shape(projection)
        or is_projection_worktree_then_checkpoint_ref_reconciliation_shape(projection)
    )


def _observe_checkpoint_ref_absent(
    *, lifecycle_id: str, identity: RepositoryIdentity, context: TrustedRepositoryContext
) -> tuple[ReconciliationEntryOutcome | None, str | None]:
    """The recomputed checkpoint ref must be confirmed absent. Returns
    `(None, None)` on confirmed absence, otherwise the categorical
    outcome and its sanitized detail. Shared by both worktree paths."""
    try:
        ref = CheckpointRef(context.working_tree_root, lifecycle_id)
    except (ValueError, CheckpointRefError):
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the checkpoint ref could not be constructed"
    if ref.object_format.value != identity.object_format:
        return (
            ReconciliationEntryOutcome.REFUSED,
            "the checkpoint ref's discovered object format disagrees with the trusted repository identity",
        )
    try:
        observation = ref.observe()
    except CheckpointRefError as exc:
        # ADR 0004 section 5 / Amendment 16: a symbolic ref is a positively
        # observed wrong state (REFUSED); every other observation failure
        # stays SUBSTRATE_UNAVAILABLE.
        if exc.reason is CheckpointRefFailure.SYMBOLIC_REF:
            return ReconciliationEntryOutcome.REFUSED, "the recomputed checkpoint ref is symbolic"
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the checkpoint ref could not be observed"
    if observation.present:
        return ReconciliationEntryOutcome.REFUSED, "the recomputed checkpoint ref is present"
    return None, None


def _reconcile_locked_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Load and validate the projection under the lifecycle lock, then
    route by its worktree intent: the narrow `creating` row (ADR 0004
    Amendment 12) or the existing absent-worktree path. Every result after
    the locked re-read carries the worktree intent that re-read found."""
    try:
        projection = load_lifecycle_projection(
            run_dir_fd,
            object_format=identity.object_format,
            expected_lifecycle_id=lifecycle_id,
            expected_repo_key=identity.repo_key,
            expected_state_root_id=state_root.state_root_id,
        )
    except LifecycleStoreError as exc:
        return ReconciliationEntryResult(
            lifecycle_id,
            _classify_projection_load_failure(exc),
            "lifecycle.json could not be loaded after acquiring the lifecycle lock",
        )

    initial_intent = projection.worktree.intent.value
    if not _is_reconciliation_eligible(projection):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "entry is no longer the recognized nonterminal reconciliation-eligible shape",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
            worktree_initial_persisted_intent=initial_intent,
        )

    if projection.checkpoint_ref.intent is not CheckpointIntent.ABSENT and (
        projection.worktree.intent is not WorktreeIntent.ABSENT
    ):
        # ADR 0004 Amendment 17: eligibility guarantees a materialized
        # worktree record and both container records absent.
        result = _reconcile_worktree_then_checkpoint_ref_entry(
            run_dir_fd=run_dir_fd,
            lifecycle_id=lifecycle_id,
            projection=projection,
            state_root=state_root,
            identity=identity,
            context=context,
        )
    elif projection.checkpoint_ref.intent is not CheckpointIntent.ABSENT:
        # ADR 0004 Amendment 16: eligibility already guarantees the worktree
        # and both container records are absent for this shape.
        result = replace(
            _reconcile_checkpoint_ref_entry(
                run_dir_fd=run_dir_fd,
                lifecycle_id=lifecycle_id,
                projection=projection,
                state_root=state_root,
                identity=identity,
                context=context,
            ),
            worktree_leaf_outcome="not_applicable",
        )
    elif projection.worktree.intent is not WorktreeIntent.ABSENT:
        result = _route_worktree_entry(
            run_dir_fd=run_dir_fd,
            lifecycle_id=lifecycle_id,
            projection=projection,
            state_root=state_root,
            identity=identity,
            context=context,
        )
    else:
        result = replace(
            _reconcile_absent_worktree_entry(
                run_dir_fd=run_dir_fd,
                lifecycle_id=lifecycle_id,
                projection=projection,
                state_root=state_root,
                identity=identity,
                context=context,
            ),
            worktree_leaf_outcome="not_applicable",
        )
    return replace(result, worktree_initial_persisted_intent=initial_intent)


# ADR 0004 Amendment 16: a failed compare-and-swap delete's `MutationOutcome`
# -> (entry outcome, maintenance-trace removal category). A normal return
# from `CheckpointRef.delete()` is the only applied result.
_CHECKPOINT_REF_DELETE_DISPOSITION = {
    MutationOutcome.UNCHANGED: (ReconciliationEntryOutcome.FAILED, "unchanged"),
    MutationOutcome.UNEXPECTED: (ReconciliationEntryOutcome.REFUSED, "unexpected"),
    MutationOutcome.SYMBOLIC: (ReconciliationEntryOutcome.REFUSED, "symbolic"),
    MutationOutcome.UNKNOWN: (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "unknown"),
}


def _classify_checkpoint_ref_delete_failure(exc: CheckpointRefError) -> tuple[ReconciliationEntryOutcome, str]:
    """`TRANSACTION_CLEANUP_UNCONFIRMED` is always unknown (an unconfirmed
    Git child may still hold the ref lock), and an error claiming `APPLIED`
    is a collaborator bug degraded to unknown -- `CheckpointSession`'s own
    two rules, re-applied here."""
    if (
        exc.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED
        or exc.outcome not in _CHECKPOINT_REF_DELETE_DISPOSITION
    ):
        return _CHECKPOINT_REF_DELETE_DISPOSITION[MutationOutcome.UNKNOWN]
    return _CHECKPOINT_REF_DELETE_DISPOSITION[exc.outcome]


_RECONCILED_FLAGS = dict(
    baseline_confirmed_absent=True,
    verification_confirmed_absent=True,
    worktree_confirmed_absent=True,
    checkpoint_ref_confirmed_absent=True,
)

# ADR 0004 Amendment 17: a container gate's categorical result -> the stop
# every row already used for it (unchanged details).
_CONTAINER_GATE_STOPS = {
    "present": (ReconciliationEntryOutcome.REFUSED, "a deterministic container name is present"),
    "unknown": (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "container listing failed"),
}


def _container_gate(lifecycle_id: str) -> str:
    """One complete, fresh Docker listing: `confirmed_absent` when both
    deterministic names are absent, `present` when either is listed,
    `unknown` when the listing failed. Categorical only."""
    try:
        name_to_id, _ = _docker_ps_all_id_name_pairs()
    except _DockerListingError:
        return "unknown"
    if f"codeagent-baseline-{lifecycle_id}" in name_to_id or f"codeagent-verification-{lifecycle_id}" in name_to_id:
        return "present"
    return "confirmed_absent"


def _enter_reconciling(run_dir_fd: int, projection: LifecycleProjection):
    """Enter RECONCILING before any mutation -- the only increment, and only
    on a fresh cycle. Returns `(stop, projection, attempts)`."""
    attempts = projection.reconciliation.attempts_total
    if projection.state is LifecycleState.RECONCILING:
        return None, projection, attempts
    attempts += 1
    try:
        projection = _publish_projection_state(
            run_dir_fd, projection, state=LifecycleState.RECONCILING, attempts_total=attempts
        )
    except LifecycleStoreError as exc:
        return (
            (ReconciliationEntryOutcome.FAILED, f"the RECONCILING projection write failed ({exc.reason.value})"),
            projection,
            attempts,
        )
    return None, projection, attempts


def _publish_reconciled(run_dir_fd: int, projection: LifecycleProjection, attempts: int):
    """RECONCILED, last. Returns a stop on failure, otherwise `None`."""
    try:
        _publish_projection_state(run_dir_fd, projection, state=LifecycleState.RECONCILED, attempts_total=attempts)
    except LifecycleStoreError as exc:
        return ReconciliationEntryOutcome.FAILED, f"the RECONCILED projection write failed ({exc.reason.value})"
    return None


def _inspect_owned_checkpoint_ref(
    *,
    lifecycle_id: str,
    transition: CheckpointTransition,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    trace: dict,
    key: str,
    record_role: bool,
):
    """The exact owned ref, against the trusted repository and object format,
    observed without following a symbolic ref and classified against the
    record's section 8 deletion candidates. Mutates nothing. Records the
    category in `trace[key]` (and the candidate role when `record_role`).
    Returns `(stop, ref, removal_sha)`; `removal_sha` is `None` when the ref
    is absent."""
    try:
        ref = CheckpointRef(context.working_tree_root, lifecycle_id)
    except (ValueError, CheckpointRefError):
        return (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the checkpoint ref could not be constructed"), None, None
    if ref.object_format.value != identity.object_format:
        return (
            (
                ReconciliationEntryOutcome.REFUSED,
                "the checkpoint ref's discovered object format disagrees with the trusted repository identity",
            ),
            None,
            None,
        )
    try:
        observation = ref.observe()
    except CheckpointRefError as exc:
        if exc.reason is CheckpointRefFailure.SYMBOLIC_REF:
            trace[key] = "symbolic"
            return (ReconciliationEntryOutcome.REFUSED, "the recomputed checkpoint ref is symbolic"), None, None
        trace[key] = "ambiguous" if exc.reason is CheckpointRefFailure.AMBIGUOUS_OBSERVATION else "unknown"
        return (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the checkpoint ref could not be observed"), None, None
    if not observation.present:
        trace[key] = "absent"
        return None, ref, None
    for role, sha in checkpoint_ref_deletion_candidates(transition):
        if observation.oid == sha:
            if record_role:
                trace["role"] = role
            trace[key] = f"candidate_{role}"
            return None, ref, sha
    trace[key] = "unexpected"
    return (
        (
            ReconciliationEntryOutcome.REFUSED,
            "the checkpoint ref holds a value that is not a deletion candidate for its record",
        ),
        None,
        None,
    )


def _mutate_checkpoint_ref(
    *,
    run_dir_fd: int,
    projection: LifecycleProjection,
    attempts: int,
    ref,
    removal_sha: str | None,
    trace: dict,
):
    """Amendment 16's M2-M4: write-ahead `removing(observed)` (a no-op when
    already removing), one `CheckpointRef.delete(expected_oid=observed)`,
    then `absent` only after a normal return. A ref already absent goes
    straight to `absent` with no Git mutation. Returns `(stop, projection)`."""
    if removal_sha is not None:
        already_removing = projection.checkpoint_ref.intent is CheckpointIntent.REMOVING
        try:
            projection = _publish_reconciler_checkpoint_ref_transition(
                run_dir_fd,
                projection,
                attempts_total=attempts,
                target=CheckpointTransition(
                    intent=CheckpointIntent.REMOVING, accepted_sha=removal_sha, expected_old_sha=removal_sha
                ),
            )
        except LifecycleStoreError as exc:
            return (
                (ReconciliationEntryOutcome.FAILED, f"the checkpoint-ref removing write failed ({exc.reason.value})"),
                projection,
            )
        trace["removing"] = not already_removing
        try:
            ref.delete(expected_oid=removal_sha)
        except CheckpointRefError as exc:
            outcome, category = _classify_checkpoint_ref_delete_failure(exc)
            trace["attempt"] = category
            return (outcome, f"checkpoint-ref removal not confirmed ({category})"), projection
        trace["attempt"] = "applied"
    try:
        projection = _publish_reconciler_checkpoint_ref_transition(
            run_dir_fd, projection, attempts_total=attempts, target=ABSENT_TRANSITION
        )
    except LifecycleStoreError as exc:
        return (
            (ReconciliationEntryOutcome.FAILED, f"the checkpoint-ref absent collapse write failed ({exc.reason.value})"),
            projection,
        )
    trace["absent"] = True
    return None, projection


def _checkpoint_ref_trace() -> dict:
    return {
        "pre": None,
        "role": None,
        "attempt": "not_attempted",
        "removing": False,
        "absent": False,
        "gate": None,
        "container_gate": "not_attempted",
    }


def _checkpoint_ref_result_fields(transition: CheckpointTransition, trace: dict) -> dict:
    return dict(
        checkpoint_ref_initial_persisted_intent=transition.intent.value,
        checkpoint_ref_observation_pre=trace["pre"],
        checkpoint_ref_candidate_role=trace["role"],
        checkpoint_ref_removal_attempt=trace["attempt"],
        checkpoint_ref_removing_transition_confirmed_this_pass=trace["removing"],
        checkpoint_ref_absent_transition_confirmed_this_pass=trace["absent"],
        checkpoint_ref_gate_observation=trace["gate"],
        checkpoint_ref_container_gate=trace["container_gate"],
    )


def _reconcile_checkpoint_ref_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """ADR 0004 Amendment 16: a dead entry whose checkpoint-ref record is
    `creating`/`present`/`advancing`/`removing` and whose worktree and both
    container records are absent.

    Every inspection precedes every mutation, in section 9's order: both
    deterministic container names absent (one fresh listing, recorded as
    `container_gate`); the worktree unregistered, with zero Git admin
    entries and no leaf entry (I5); the repository object format; then the
    exact owned ref, observed without following a symbolic ref. Then:
    RECONCILING (+1 only on a fresh cycle) -> write-ahead
    `removing(observed)` (a no-op when already `removing`) -> one
    `CheckpointRef.delete(expected_oid=observed)` compare-and-swap ->
    `absent` only after a normal return -> RECONCILED. A ref already absent
    collapses without any Git mutation. Messages and trace fields are
    categorical; no SHA is ever recorded."""
    transition = projection.checkpoint_ref
    attempts = projection.reconciliation.attempts_total
    trace = _checkpoint_ref_trace()

    def result(outcome, detail, **extra) -> ReconciliationEntryResult:
        return ReconciliationEntryResult(
            lifecycle_id,
            outcome,
            detail,
            run_id=projection.run_id,
            attempt_number=attempts,
            **_checkpoint_ref_result_fields(transition, trace),
            **extra,
        )

    # Section 9 / I4: both deterministic container names absent.
    trace["container_gate"] = _container_gate(lifecycle_id)
    if trace["container_gate"] != "confirmed_absent":
        return result(*_CONTAINER_GATE_STOPS[trace["container_gate"]])

    # I5: the worktree confirmed absent -- unregistered, zero admin entries,
    # and no entry at the deterministic leaf path (not followed).
    expected_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))
    outcome, detail = _check_worktree_unregistered(
        lifecycle_id=lifecycle_id, expected_path=expected_path, identity=identity, context=context
    )
    if outcome is not None:
        return result(outcome, detail)
    # The leaf name, observed descriptor-relative and no-follow (Amendment 13's
    # observer): only a genuinely missing name is absence. `os.path.lexists`
    # is not used -- it reports *any* `OSError` (e.g. an unreadable parent) as
    # absence, which would mistake an inspection failure for I5's proof.
    try:
        leaf = state_root.observe_materialized_worktree_leaf(identity.repo_key, lifecycle_id)
    except LifecycleFsError as exc:
        return result(_classify_entry_fs_failure(exc), "the worktree leaf could not be inspected")
    if leaf is MaterializedLeafObservation.UNKNOWN:
        return result(ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the worktree leaf could not be inspected")
    if leaf is not MaterializedLeafObservation.ABSENT:
        return result(ReconciliationEntryOutcome.REFUSED, "the recomputed worktree leaf is present")

    stop, ref, removal_sha = _inspect_owned_checkpoint_ref(
        lifecycle_id=lifecycle_id, transition=transition, identity=identity, context=context,
        trace=trace, key="pre", record_role=True,
    )
    if stop is not None:
        return result(*stop)

    stop, projection, attempts = _enter_reconciling(run_dir_fd, projection)
    if stop is not None:
        return result(*stop)
    stop, projection = _mutate_checkpoint_ref(
        run_dir_fd=run_dir_fd, projection=projection, attempts=attempts, ref=ref, removal_sha=removal_sha, trace=trace
    )
    if stop is not None:
        return result(*stop)
    stop = _publish_reconciled(run_dir_fd, projection, attempts)
    if stop is not None:
        return result(*stop)
    return result(ReconciliationEntryOutcome.RECONCILED, "confirmed absent and durably reconciled", **_RECONCILED_FLAGS)


def _reconcile_absent_worktree_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Inspect-before-mutate, per entry: confirm the checkpoint ref then
    the worktree absent (unchanged from Slice 3B-1, still out of removal
    scope), then
    inspect both container roles completely (one Docker listing plus
    every ownership-proof inspect it requires) and compute both roles'
    complete decisions *before* any mutation of either — a conflict on
    either role aborts the whole entry with zero mutation attempted.
    Only once both decisions are known does this function publish any
    write-ahead transition, and only once both roles' write-ahead
    writes are durably confirmed does it ever issue a `docker rm`,
    baseline before verification, stopping before verification's
    removal if baseline's own resolution did not reach absent this
    pass."""
    ref_outcome, ref_detail = _observe_checkpoint_ref_absent(
        lifecycle_id=lifecycle_id, identity=identity, context=context
    )
    if ref_outcome is not None:
        return ReconciliationEntryResult(
            lifecycle_id,
            ref_outcome,
            ref_detail,
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )

    expected_worktree_path = os.path.normpath(
        os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id)
    )
    try:
        registered_paths = _worktree_registered_paths(context.working_tree_root)
    except _GitWorktreeListingError:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "worktree listing failed",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )
    if expected_worktree_path in registered_paths or os.path.lexists(expected_worktree_path):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "the recomputed worktree is registered or present",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    try:
        name_to_id, id_to_name = _docker_ps_all_id_name_pairs()
    except _DockerListingError:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "container listing failed",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )

    decisions: dict[str, _ContainerDecision] = {}
    for role, attribution, expected_name in (
        ("baseline", projection.baseline, baseline_name),
        ("verification", projection.verification, verification_name),
    ):
        decisions[role] = _classify_container(
            attribution=attribution,
            role=role,
            expected_name=expected_name,
            name_to_id=name_to_id,
            id_to_name=id_to_name,
            state_root_id=state_root.state_root_id,
            lifecycle_id=lifecycle_id,
        )

    for role in ("baseline", "verification"):
        decision = decisions[role]
        if decision.outcome in (_ContainerDecisionOutcome.REFUSED, _ContainerDecisionOutcome.SUBSTRATE_UNAVAILABLE):
            final_outcome = (
                ReconciliationEntryOutcome.REFUSED
                if decision.outcome is _ContainerDecisionOutcome.REFUSED
                else ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
            )
            return ReconciliationEntryResult(
                lifecycle_id,
                final_outcome,
                f"{role} container: {decision.detail}",
                run_id=projection.run_id,
                attempt_number=projection.reconciliation.attempts_total,
                baseline_id=decisions["baseline"].observed_id,
                verification_id=decisions["verification"].observed_id,
            )

    # Both roles' decisions are conflict-free. Compute the one
    # attempt-count increment this pass may perform (fresh cycle only;
    # a resumed RECONCILING keeps its already-incremented value).
    fresh_cycle = projection.state is not LifecycleState.RECONCILING
    attempts_total = projection.reconciliation.attempts_total + 1 if fresh_cycle else projection.reconciliation.attempts_total

    # Correction pass finding 2: both retrospective trace ids are
    # derived immediately from both already-completed decisions, before
    # any publication whatsoever -- every return from this point on,
    # success or failure, retains these exact values unchanged. This is
    # deliberately independent of `removal_id` (finding 1): a role that
    # was never positively observed live (e.g. `CONFIRMED_ABSENT` while
    # persisted `present`) still correctly reports `None` here, even
    # though its `removal_id` is set for the write-ahead path.
    observed_ids: dict[str, str | None] = {
        "baseline": decisions["baseline"].observed_id,
        "verification": decisions["verification"].observed_id,
    }

    # Write-ahead phase: baseline then verification, entirely before
    # any `docker rm` is issued for either role.
    pending_removal: dict[str, str] = {}
    resolved_absent: dict[str, bool] = {"baseline": False, "verification": False}
    for role in ("baseline", "verification"):
        decision = decisions[role]
        if decision.outcome is _ContainerDecisionOutcome.NOOP:
            resolved_absent[role] = True
            continue
        if decision.outcome is _ContainerDecisionOutcome.CONFIRMED_ABSENT and decision.direct_to_absent:
            try:
                projection = _publish_reconciler_container_transition(
                    run_dir_fd,
                    projection,
                    role=role,
                    intent=ContainerIntent.ABSENT,
                    id=None,
                    attempts_total=attempts_total,
                )
            except LifecycleStoreError as exc:
                return ReconciliationEntryResult(
                    lifecycle_id,
                    ReconciliationEntryOutcome.FAILED,
                    f"the {role} confirmed-absent write failed ({exc.reason.value})",
                    run_id=projection.run_id,
                    attempt_number=attempts_total,
                    baseline_id=observed_ids["baseline"],
                    verification_id=observed_ids["verification"],
                )
            resolved_absent[role] = True
            continue

        # OWNED_REMOVE, or CONFIRMED_ABSENT-while-persisted-present:
        # both require a REMOVING write-ahead write before absence can
        # be declared (no direct PRESENT/CREATING->ABSENT edge exists).
        # `observed_ids` was already fully derived above and is never
        # touched here -- only `decision.removal_id` (the authorized
        # write target) is used for the actual write.
        assert decision.removal_id is not None
        try:
            projection = _publish_reconciler_container_transition(
                run_dir_fd,
                projection,
                role=role,
                intent=ContainerIntent.REMOVING,
                id=decision.removal_id,
                attempts_total=attempts_total,
            )
        except LifecycleStoreError as exc:
            return ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.FAILED,
                f"the {role} removing write-ahead write failed ({exc.reason.value})",
                run_id=projection.run_id,
                attempt_number=attempts_total,
                baseline_id=observed_ids["baseline"],
                verification_id=observed_ids["verification"],
            )
        pending_removal[role] = decision.removal_id

    # Removal phase: baseline first, then verification — only after
    # baseline reaches durable absence.
    for role in ("baseline", "verification"):
        if role not in pending_removal:
            continue
        removal_id = pending_removal[role]
        expected_name = baseline_name if role == "baseline" else verification_name
        outcome, detail = _remove_and_confirm_absent(
            removal_id=removal_id,
            expected_name=expected_name,
            role=role,
            state_root_id=state_root.state_root_id,
            lifecycle_id=lifecycle_id,
            attempt_rm=decisions[role].live_present,
        )
        if outcome is _DockerRemovalOutcome.CONFIRMED_ABSENT:
            try:
                projection = _publish_reconciler_container_transition(
                    run_dir_fd,
                    projection,
                    role=role,
                    intent=ContainerIntent.ABSENT,
                    id=None,
                    attempts_total=attempts_total,
                )
            except LifecycleStoreError as exc:
                return ReconciliationEntryResult(
                    lifecycle_id,
                    ReconciliationEntryOutcome.FAILED,
                    f"the {role} absent collapse write failed ({exc.reason.value})",
                    run_id=projection.run_id,
                    attempt_number=attempts_total,
                    baseline_id=observed_ids["baseline"],
                    verification_id=observed_ids["verification"],
                )
            resolved_absent[role] = True
            continue

        final_outcome = {
            _DockerRemovalOutcome.STILL_PRESENT: ReconciliationEntryOutcome.FAILED,
            _DockerRemovalOutcome.CONFLICT: ReconciliationEntryOutcome.REFUSED,
            _DockerRemovalOutcome.SUBSTRATE_UNAVAILABLE: ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
        }[outcome]
        return ReconciliationEntryResult(
            lifecycle_id,
            final_outcome,
            f"{role} container: {detail}",
            run_id=projection.run_id,
            attempt_number=attempts_total,
            baseline_id=observed_ids["baseline"],
            verification_id=observed_ids["verification"],
        )

    # Both containers (and the worktree/checkpoint ref, already
    # confirmed above) are now durably absent: enter RECONCILING if a
    # container write did not already do so, then collapse to
    # RECONCILED.
    assert resolved_absent["baseline"] and resolved_absent["verification"]
    if projection.state is not LifecycleState.RECONCILING:
        try:
            projection = _publish_projection_state(
                run_dir_fd, projection, state=LifecycleState.RECONCILING, attempts_total=attempts_total
            )
        except LifecycleStoreError as exc:
            return ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.FAILED,
                f"the RECONCILING projection write failed ({exc.reason.value})",
                run_id=projection.run_id,
                attempt_number=attempts_total,
                baseline_id=observed_ids["baseline"],
                verification_id=observed_ids["verification"],
            )

    try:
        _publish_projection_state(
            run_dir_fd, projection, state=LifecycleState.RECONCILED, attempts_total=attempts_total
        )
    except LifecycleStoreError as exc:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.FAILED,
            f"the RECONCILED projection write failed ({exc.reason.value})",
            run_id=projection.run_id,
            attempt_number=attempts_total,
            baseline_id=observed_ids["baseline"],
            verification_id=observed_ids["verification"],
        )

    return ReconciliationEntryResult(
        lifecycle_id,
        ReconciliationEntryOutcome.RECONCILED,
        "confirmed absent and durably reconciled",
        run_id=projection.run_id,
        attempt_number=attempts_total,
        baseline_confirmed_absent=True,
        verification_confirmed_absent=True,
        worktree_confirmed_absent=True,
        checkpoint_ref_confirmed_absent=True,
        baseline_id=observed_ids["baseline"],
        verification_id=observed_ids["verification"],
    )


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 13: materialized `creating`/`present`/`disposing`
# worktrees, removed only by one bounded, hardened, single-force
# `git worktree remove --force <exact path>`.
# ---------------------------------------------------------------------------

_WORKTREE_REMOVE_STDOUT_MAX_BYTES = 4096

# Every reason `run_git_bounded` can emit for this command -> trace value.
# An unexpected reason falls back to "cleanup_unconfirmed": nothing about the
# child is asserted, and no observation or transition follows it.
_REMOVAL_ATTEMPT_BY_FAILURE = {
    GitSafetyFailure.BOUNDED_COMMAND_FAILED: "exited_nonzero",
    GitSafetyFailure.GIT_COMMAND_TIMEOUT: "stopped_after_failure",
    GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE: "stopped_after_failure",
    GitSafetyFailure.PROCESS_SETUP_FAILED: "stopped_after_failure",
    GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED: "cleanup_unconfirmed",
    GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE: "launch_failed",
}


def _attempt_worktree_remove(working_tree_root: str, target_path: str) -> str:
    """Run the one sanctioned worktree mutation. Its exit status is never
    evidence of success; the caller always re-observes (unless the child
    was never launched, or was not confirmed stopped)."""
    try:
        run_git_bounded(
            working_tree_root,
            "worktree",
            "remove",
            "--force",
            target_path,
            limit=_WORKTREE_REMOVE_STDOUT_MAX_BYTES,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except GitSafetyError as exc:
        return _REMOVAL_ATTEMPT_BY_FAILURE.get(exc.reason, "cleanup_unconfirmed")
    return "exited_zero"


@dataclass(frozen=True)
class _WorktreeObservation:
    registration: _TargetRegistration
    admin: int | str  # 0 / 1 / "many" / "unknown"
    leaf: str  # MaterializedLeafObservation value


def _observe_worktree(
    *, state_root, identity: RepositoryIdentity, context: TrustedRepositoryContext, lifecycle_id: str, target_path: str
) -> _WorktreeObservation:
    """Three fresh, independent observations. A descriptor-close or
    CLOEXEC failure propagates as `LifecycleFsError`."""
    registration = _observe_target_registration(
        context.working_tree_root,
        oid_hex_len=ObjectFormat(identity.object_format).hex_length,
        target_path=target_path,
    )
    admin = _count_worktree_admin_entries(identity.canonical_common_dir, lifecycle_id)
    leaf = state_root.observe_materialized_worktree_leaf(identity.repo_key, lifecycle_id).value
    return _WorktreeObservation(registration, admin, leaf)


def _pre_removal_gate(intent: WorktreeIntent, registration: _TargetRegistration, admin):
    """Decide every row that registration and admin evidence settle on their
    own, before any leaf observation. Returns `(outcome, detail)` to stop,
    or `None` when the leaf observer's result is genuinely needed: one
    eligible registration with one admin entry, or `disposing` with neither
    (where an absent leaf is the collapse case). A `creating` record with no
    registration never reaches here (Amendment 12 routing)."""
    refused = ReconciliationEntryOutcome.REFUSED
    if registration.state == "unknown" or admin == "unknown":
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the worktree could not be fully inspected"
    if registration.target_records == "many":
        return refused, "the worktree path is registered more than once"
    if registration.target_records == 1:
        if registration.target_bare:
            return refused, "the worktree registration is bare"
        if registration.locked:
            return refused, "the worktree registration is locked"
        if admin != 1:
            return refused, "the worktree registration has no single matching admin entry"
        return None
    if intent is not WorktreeIntent.DISPOSING:
        return refused, "the worktree is not registered"
    if admin != 0:
        return refused, "a git worktree admin entry exists without a registration"
    return None


def _classify_pre_removal(intent: WorktreeIntent, obs: _WorktreeObservation):
    """Before-command table. Returns ("remove" | "collapse", None, None) or
    ("stop", outcome, detail). Nothing has been changed yet."""
    r, admin, leaf = obs.registration, obs.admin, obs.leaf
    refused = ReconciliationEntryOutcome.REFUSED
    if r.state == "unknown" or admin == "unknown" or leaf == "unknown":
        return "stop", ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the worktree could not be fully inspected"
    if leaf == "conflict":
        return "stop", refused, "the worktree leaf is not a private materialized directory"
    if r.target_records == "many":
        return "stop", refused, "the worktree path is registered more than once"
    if r.target_records == 1:
        if r.target_bare:
            return "stop", refused, "the worktree registration is bare"
        if r.locked:
            return "stop", refused, "the worktree registration is locked"
        if admin != 1:
            return "stop", refused, "the worktree registration has no single matching admin entry"
        if leaf == "materialized":
            if r.prunable:
                return "stop", refused, "git reports the registered worktree as prunable"
            return "remove", None, None
        if intent is WorktreeIntent.DISPOSING:
            return "remove", None, None  # D2: disposing, registered, directory confirmed absent
        return "stop", refused, "the registered worktree directory is missing without disposal begun"
    if admin != 0:
        return "stop", refused, "a git worktree admin entry exists without a registration"
    if leaf == "absent" and intent is WorktreeIntent.DISPOSING:
        return "collapse", None, None
    return "stop", refused, "the worktree is not registered"


def _classify_post_removal(obs: _WorktreeObservation):
    """After-command table. Returns (outcome or None if confirmed absent,
    leaf_outcome)."""
    r, admin, leaf = obs.registration, obs.admin, obs.leaf
    refused = ReconciliationEntryOutcome.REFUSED
    failed = ReconciliationEntryOutcome.FAILED
    if r.state == "unknown" or admin == "unknown" or leaf == "unknown":
        return ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "removal_unconfirmed"
    if leaf == "conflict" or r.target_records == "many":
        return refused, "removal_unconfirmed"
    if r.target_records == 1:
        if r.target_bare or r.locked or admin != 1:
            return refused, "removal_unconfirmed"
        if leaf == "materialized":
            return failed, "removal_unconfirmed"
        return failed, "registered_directory_missing"
    if admin != 0:
        return refused, "removal_unconfirmed"
    if leaf == "materialized":
        return failed, "partial_removal_leftover"
    return None, "removed_by_git"


def _route_worktree_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Dispatch a non-absent worktree record (ADR 0004 Amendments 12/13):
    `creating` with no registration goes to Amendment 12's empty-leaf row
    (whose own admin scan refuses any admin entry); everything else goes
    to the Amendment 13 materialized row."""
    target_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))

    def stop(outcome, detail, registration=None) -> ReconciliationEntryResult:
        return ReconciliationEntryResult(
            lifecycle_id,
            outcome,
            detail,
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
            worktree_registration_pre=registration.to_trace() if registration is not None else None,
        )

    outcome, detail = _observe_checkpoint_ref_absent(lifecycle_id=lifecycle_id, identity=identity, context=context)
    if outcome is not None:
        return stop(outcome, detail)
    registration = _observe_target_registration(
        context.working_tree_root,
        oid_hex_len=ObjectFormat(identity.object_format).hex_length,
        target_path=target_path,
    )
    if registration.state == "unknown":
        return stop(ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "worktree listing failed", registration)
    if projection.worktree.intent is WorktreeIntent.CREATING and registration.state == "absent":
        result = _reconcile_creating_worktree_entry(
            run_dir_fd=run_dir_fd,
            lifecycle_id=lifecycle_id,
            projection=projection,
            state_root=state_root,
            identity=identity,
            context=context,
        )
        return replace(result, worktree_registration_pre=registration.to_trace())
    return _reconcile_materialized_worktree_entry(
        run_dir_fd=run_dir_fd,
        lifecycle_id=lifecycle_id,
        projection=projection,
        state_root=state_root,
        identity=identity,
        context=context,
        registration=registration,
        target_path=target_path,
    )


def _worktree_trace(registration: _TargetRegistration) -> dict:
    return {
        "leaf_outcome": "removal_not_attempted",
        "registration_pre": registration.to_trace(),
        "registration_post": None,
        "admin_pre": None,
        "admin_post": None,
        "leaf_pre": None,
        "leaf_post": None,
        "attempt": "not_attempted",
        "disposing": False,
        "absent": False,
    }


def _worktree_result_fields(trace: dict) -> dict:
    return dict(
        worktree_leaf_outcome=trace["leaf_outcome"],
        worktree_registration_pre=trace["registration_pre"],
        worktree_registration_post=trace["registration_post"],
        worktree_admin_matches_pre=trace["admin_pre"],
        worktree_admin_matches_post=trace["admin_post"],
        worktree_leaf_pre=trace["leaf_pre"],
        worktree_leaf_post=trace["leaf_post"],
        worktree_removal_attempt=trace["attempt"],
        worktree_disposing_transition_confirmed_this_pass=trace["disposing"],
        worktree_absent_transition_confirmed_this_pass=trace["absent"],
    )


def _inspect_materialized_worktree(
    *,
    lifecycle_id: str,
    intent: WorktreeIntent,
    registration: _TargetRegistration,
    state_root,
    identity: RepositoryIdentity,
    trace: dict,
):
    """Amendment 13's worktree inspection; mutates nothing. I4: admin
    entries. I5 (the leaf) only for rows that need its result:
    registration/admin evidence that already decides the outcome is never
    overridden by a leaf observation or its cleanup failure. Returns
    `(stop, action)`."""
    try:
        admin = _count_worktree_admin_entries(identity.canonical_common_dir, lifecycle_id)
        trace["admin_pre"] = admin
        gate = _pre_removal_gate(intent, registration, admin)
        if gate is not None:
            return gate, None
        leaf = state_root.observe_materialized_worktree_leaf(identity.repo_key, lifecycle_id).value
        trace["leaf_pre"] = leaf
    except LifecycleFsError as exc:
        trace["leaf_outcome"] = (
            "close_failed" if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED else "not_inspected"
        )
        return (_classify_entry_fs_failure(exc), "the worktree could not be inspected"), None
    action, outcome, detail = _classify_pre_removal(intent, _WorktreeObservation(registration, admin, leaf))
    if action == "stop":
        return (outcome, detail), None
    return None, action


def _mutate_materialized_worktree(
    *,
    run_dir_fd: int,
    projection: LifecycleProjection,
    attempts: int,
    intent: WorktreeIntent,
    action: str,
    lifecycle_id: str,
    target_path: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    trace: dict,
):
    """Amendment 13's M2-M5: reconciler `-> disposing` (skipped when already
    disposing) -> one `git worktree remove --force` (or the collapse case)
    -> three fresh observations -> `disposing -> absent`, published only
    when the registration, the admin entry, and the leaf are all
    independently confirmed absent. Returns `(stop, projection)`."""
    if intent is not WorktreeIntent.DISPOSING:
        try:
            projection = _publish_reconciler_worktree_transition(
                run_dir_fd,
                projection,
                attempts_total=attempts,
                target=WorktreeTransition(
                    intent=WorktreeIntent.DISPOSING, expected_head=projection.worktree.expected_head
                ),
            )
        except LifecycleStoreError as exc:
            return (
                (ReconciliationEntryOutcome.FAILED, f"the worktree disposing write failed ({exc.reason.value})"),
                projection,
            )
        trace["disposing"] = True

    if action == "collapse":
        trace["leaf_outcome"] = "already_absent"
    else:
        # M3: the one bounded, hardened, single-force removal.
        attempt = _attempt_worktree_remove(context.working_tree_root, target_path)
        trace["attempt"] = attempt
        if attempt == "launch_failed":
            return (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "git could not be launched"), projection
        if attempt == "cleanup_unconfirmed":
            trace["leaf_outcome"] = "removal_unconfirmed"
            return (
                (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the git worktree removal could not be confirmed stopped"),
                projection,
            )
        # M4: three fresh observations, whatever the command reported.
        try:
            post = _observe_worktree(
                state_root=state_root,
                identity=identity,
                context=context,
                lifecycle_id=lifecycle_id,
                target_path=target_path,
            )
        except LifecycleFsError as exc:
            trace["leaf_outcome"] = (
                "close_failed" if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED else "removal_unconfirmed"
            )
            return (_classify_entry_fs_failure(exc), "the worktree could not be re-observed"), projection
        trace["registration_post"] = post.registration.to_trace()
        trace["admin_post"] = post.admin
        trace["leaf_post"] = post.leaf
        outcome, leaf_outcome = _classify_post_removal(post)
        trace["leaf_outcome"] = leaf_outcome
        if outcome is not None:
            return (outcome, f"worktree removal not confirmed ({leaf_outcome})"), projection

    # M5: reconciler-owned `disposing -> absent`.
    try:
        projection = _publish_reconciler_worktree_transition(run_dir_fd, projection, attempts_total=attempts)
    except LifecycleStoreError as exc:
        return (
            (ReconciliationEntryOutcome.FAILED, f"the worktree absent collapse write failed ({exc.reason.value})"),
            projection,
        )
    trace["absent"] = True
    return None, projection


def _reconcile_materialized_worktree_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    registration: _TargetRegistration,
    target_path: str,
) -> ReconciliationEntryResult:
    """ADR 0004 Amendment 13. Inspection (admin scan, leaf, containers)
    mutates nothing. Then: RECONCILING (+1 only on a fresh cycle) ->
    reconciler `-> disposing` (skipped when already disposing) -> one
    `git worktree remove --force` -> three fresh observations ->
    `disposing -> absent` -> RECONCILED. `absent` is published only when
    the registration, the admin entry, and the leaf are all independently
    confirmed absent."""
    intent = projection.worktree.intent
    attempts = projection.reconciliation.attempts_total
    trace = _worktree_trace(registration)

    def result(outcome, detail, **extra) -> ReconciliationEntryResult:
        return ReconciliationEntryResult(
            lifecycle_id,
            outcome,
            detail,
            run_id=projection.run_id,
            attempt_number=attempts,
            **_worktree_result_fields(trace),
            **extra,
        )

    stop, action = _inspect_materialized_worktree(
        lifecycle_id=lifecycle_id, intent=intent, registration=registration,
        state_root=state_root, identity=identity, trace=trace,
    )
    if stop is not None:
        return result(*stop)

    # I6: both deterministic container names absent (invariant I4).
    gate = _container_gate(lifecycle_id)
    if gate != "confirmed_absent":
        return result(*_CONTAINER_GATE_STOPS[gate])

    stop, projection, attempts = _enter_reconciling(run_dir_fd, projection)
    if stop is not None:
        return result(*stop)
    stop, projection = _mutate_materialized_worktree(
        run_dir_fd=run_dir_fd, projection=projection, attempts=attempts, intent=intent, action=action,
        lifecycle_id=lifecycle_id, target_path=target_path, state_root=state_root, identity=identity,
        context=context, trace=trace,
    )
    if stop is not None:
        return result(*stop)
    stop = _publish_reconciled(run_dir_fd, projection, attempts)
    if stop is not None:
        return result(*stop)
    return result(ReconciliationEntryOutcome.RECONCILED, "confirmed absent and durably reconciled", **_RECONCILED_FLAGS)


def _reconcile_worktree_then_checkpoint_ref_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """ADR 0004 Amendment 17: a materialized worktree (Amendment 13) and a
    non-absent checkpoint ref (Amendment 16), both containers absent, in one
    locked pass and one reconciliation cycle.

    Each destructive phase has its complete inspection gate immediately
    before it; this does not claim every inspection precedes every mutation:

    - Gate A (zero mutation if it stops): container gate 1; Amendment 13's
      full worktree inspection; a read-only checkpoint-ref gate
      (`gate_observation`), which never supplies the deletion SHA.
    - Phase W: RECONCILING (+1 only on a fresh cycle), then Amendment 13's
      M2-M5 through a durably confirmed worktree `absent`. Any failure stops
      the pass before every later step.
    - Gate B (zero ref mutation if it stops): container gate 2, a second
      complete fresh listing (`container_gate`), then a fresh authoritative
      ref observation (`observation_pre`), the only source of the SHA.
    - Phase R: Amendment 16's M2-M4. Then RECONCILED, once and last.

    Not cross-resource atomicity: a same-user actor (A4) can still act
    between phases. A refusal or failure after Phase W leaves Amendment 16's
    resumable shape. A never-registered `creating` worktree (Amendment 12's
    shape) with a ref is refused: the owner cannot produce it."""
    intent = projection.worktree.intent
    transition = projection.checkpoint_ref
    attempts = projection.reconciliation.attempts_total
    target_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))
    wt_trace: dict | None = None
    ref_trace = _checkpoint_ref_trace()

    def result(outcome, detail, **extra) -> ReconciliationEntryResult:
        fields = _worktree_result_fields(wt_trace) if wt_trace is not None else {}
        return ReconciliationEntryResult(
            lifecycle_id,
            outcome,
            detail,
            run_id=projection.run_id,
            attempt_number=attempts,
            **fields,
            **_checkpoint_ref_result_fields(transition, ref_trace),
            **extra,
        )

    # Gate A, before any worktree mutation.
    gate = _container_gate(lifecycle_id)
    if gate != "confirmed_absent":
        return result(*_CONTAINER_GATE_STOPS[gate])
    registration = _observe_target_registration(
        context.working_tree_root,
        oid_hex_len=ObjectFormat(identity.object_format).hex_length,
        target_path=target_path,
    )
    wt_trace = _worktree_trace(registration)
    if registration.state == "unknown":
        return result(ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "worktree listing failed")
    if intent is WorktreeIntent.CREATING and registration.state == "absent":
        return result(
            ReconciliationEntryOutcome.REFUSED,
            "a never-registered creating worktree with a checkpoint ref is not reconciled",
        )
    stop, action = _inspect_materialized_worktree(
        lifecycle_id=lifecycle_id, intent=intent, registration=registration,
        state_root=state_root, identity=identity, trace=wt_trace,
    )
    if stop is not None:
        return result(*stop)
    stop, _, _ = _inspect_owned_checkpoint_ref(
        lifecycle_id=lifecycle_id, transition=transition, identity=identity, context=context,
        trace=ref_trace, key="gate", record_role=False,
    )
    if stop is not None:
        return result(*stop)

    # Phase W.
    stop, projection, attempts = _enter_reconciling(run_dir_fd, projection)
    if stop is not None:
        return result(*stop)
    stop, projection = _mutate_materialized_worktree(
        run_dir_fd=run_dir_fd, projection=projection, attempts=attempts, intent=intent, action=action,
        lifecycle_id=lifecycle_id, target_path=target_path, state_root=state_root, identity=identity,
        context=context, trace=wt_trace,
    )
    if stop is not None:
        return result(*stop)

    # Gate B, after the worktree is durably absent, before any ref mutation.
    ref_trace["container_gate"] = _container_gate(lifecycle_id)
    if ref_trace["container_gate"] != "confirmed_absent":
        return result(*_CONTAINER_GATE_STOPS[ref_trace["container_gate"]])
    stop, ref, removal_sha = _inspect_owned_checkpoint_ref(
        lifecycle_id=lifecycle_id, transition=transition, identity=identity, context=context,
        trace=ref_trace, key="pre", record_role=True,
    )
    if stop is not None:
        return result(*stop)

    # Phase R.
    stop, projection = _mutate_checkpoint_ref(
        run_dir_fd=run_dir_fd, projection=projection, attempts=attempts, ref=ref,
        removal_sha=removal_sha, trace=ref_trace,
    )
    if stop is not None:
        return result(*stop)
    stop = _publish_reconciled(run_dir_fd, projection, attempts)
    if stop is not None:
        return result(*stop)
    return result(ReconciliationEntryOutcome.RECONCILED, "confirmed absent and durably reconciled", **_RECONCILED_FLAGS)


# ADR 0004 Amendment 12: removal-primitive result -> (entry outcome or
# None to continue, maintenance-trace leaf_outcome).
_LEAF_REMOVAL_DISPOSITION = {
    LeafRemovalKind.ALREADY_ABSENT: (None, "already_absent"),
    LeafRemovalKind.PRE_CONFLICT: (ReconciliationEntryOutcome.REFUSED, "conflict_not_removed"),
    LeafRemovalKind.NOT_EMPTY_AT_RMDIR: (ReconciliationEntryOutcome.REFUSED, "conflict_not_removed"),
    LeafRemovalKind.PRE_INSPECTION_FAILED: (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "removal_not_attempted"),
    LeafRemovalKind.ORIGINAL_STILL_PRESENT: (ReconciliationEntryOutcome.FAILED, "original_still_present"),
    LeafRemovalKind.REPLACEMENT_CONFLICT: (ReconciliationEntryOutcome.REFUSED, "replacement_conflict"),
    LeafRemovalKind.POST_INSPECTION_FAILED: (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "post_inspection_failed"),
}


def _reconcile_creating_worktree_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    projection: LifecycleProjection,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """ADR 0004 Amendment 12: a dead lifecycle whose worktree record is
    `creating` and whose deterministic leaf is an empty, private,
    unregistered directory with no Git admin entry.

    Inspection (I2-I6) mutates nothing. Mutation: enter RECONCILING
    (attempts +1 only on a fresh cycle) -> remove the empty leaf if one is
    present -> close the leaf descriptors (a close failure dominates and
    stops everything after it) -> fresh Git listing and admin scan ->
    reconciler-owned `creating -> absent` -> RECONCILED. Once mutation has
    begun, `REFUSED`/`SUBSTRATE_UNAVAILABLE` may follow a real removal (the
    container reconciler's own convention); `leaf_outcome` records what
    actually happened."""
    attempts = projection.reconciliation.attempts_total
    trace = {"leaf": "not_inspected", "removal": None}

    def result(outcome, detail, **extra) -> ReconciliationEntryResult:
        return ReconciliationEntryResult(
            lifecycle_id,
            outcome,
            detail,
            run_id=projection.run_id,
            attempt_number=attempts,
            worktree_leaf_outcome=trace["leaf"],
            worktree_removal_observation=trace["removal"],
            **extra,
        )

    # I2: checkpoint ref absent.
    outcome, detail = _observe_checkpoint_ref_absent(lifecycle_id=lifecycle_id, identity=identity, context=context)
    if outcome is not None:
        return result(outcome, detail)

    # I3 + I4: exact path unregistered, no admin entry.
    expected_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))
    outcome, detail = _check_worktree_unregistered(
        lifecycle_id=lifecycle_id, expected_path=expected_path, identity=identity, context=context
    )
    if outcome is not None:
        return result(outcome, detail)

    # I5: the leaf itself.
    try:
        opened = state_root.open_abandoned_worktree_leaf(identity.repo_key, lifecycle_id)
    except LifecycleFsError as exc:
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            trace["leaf"] = "close_failed"
        return result(_classify_entry_fs_failure(exc), "the worktree leaf could not be inspected")
    if opened.observation is AbandonedLeafObservation.CONFLICT:
        trace["leaf"] = "conflict_not_removed"
        return result(ReconciliationEntryOutcome.REFUSED, "the worktree leaf is not an empty private directory")
    if opened.observation is AbandonedLeafObservation.UNKNOWN:
        return result(ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "the worktree leaf could not be inspected")
    leaf = opened.leaf
    trace["leaf"] = "already_absent" if leaf is None else "removal_not_attempted"

    try:
        early = _creating_row_mutate(
            run_dir_fd=run_dir_fd, projection=projection, leaf=leaf, lifecycle_id=lifecycle_id, trace=trace
        )
    except BaseException as exc:
        if leaf is not None:
            leaf.__exit__(type(exc), exc, exc.__traceback__)  # close failure dominates, chained
        raise
    projection, attempts = early.projection, early.attempts

    # M3: close before anything else; a close failure dominates and stops
    # every later step.
    if leaf is not None:
        try:
            leaf.close()
        except LifecycleFsError:
            trace["leaf"] = "close_failed"
            return result(
                ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
                "the worktree leaf descriptors could not be confirmed closed",
            )
    if early.result is not None:
        return result(*early.result)

    # M4: fresh Git listing and admin scan after any removal.
    outcome, detail = _check_worktree_unregistered(
        lifecycle_id=lifecycle_id, expected_path=expected_path, identity=identity, context=context
    )
    if outcome is not None:
        return result(outcome, detail)

    # M5: reconciler-owned creating -> absent.
    try:
        projection = _publish_reconciler_worktree_transition(run_dir_fd, projection, attempts_total=attempts)
    except LifecycleStoreError as exc:
        return result(ReconciliationEntryOutcome.FAILED, f"the worktree absent collapse write failed ({exc.reason.value})")

    # M6.
    try:
        _publish_projection_state(run_dir_fd, projection, state=LifecycleState.RECONCILED, attempts_total=attempts)
    except LifecycleStoreError as exc:
        return result(
            ReconciliationEntryOutcome.FAILED,
            f"the RECONCILED projection write failed ({exc.reason.value})",
            worktree_absent_transition_confirmed_this_pass=True,
        )
    return result(
        ReconciliationEntryOutcome.RECONCILED,
        "confirmed absent and durably reconciled",
        baseline_confirmed_absent=True,
        verification_confirmed_absent=True,
        worktree_confirmed_absent=True,
        checkpoint_ref_confirmed_absent=True,
        worktree_absent_transition_confirmed_this_pass=True,
    )


@dataclass(frozen=True)
class _CreatingRowPhase:
    projection: LifecycleProjection
    attempts: int
    result: tuple[ReconciliationEntryOutcome, str] | None  # early exit, after the leaf is closed


def _creating_row_mutate(*, run_dir_fd, projection, leaf, lifecycle_id, trace) -> _CreatingRowPhase:
    """I6 (containers), M1 (enter RECONCILING), M2 (remove the leaf).
    Never closes the leaf -- the caller does, before acting on any
    early-exit result."""
    attempts = projection.reconciliation.attempts_total

    # I6: both deterministic container names absent (I4 invariant).
    try:
        name_to_id, _ = _docker_ps_all_id_name_pairs()
    except _DockerListingError:
        return _CreatingRowPhase(
            projection, attempts, (ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "container listing failed")
        )
    if f"codeagent-baseline-{lifecycle_id}" in name_to_id or f"codeagent-verification-{lifecycle_id}" in name_to_id:
        return _CreatingRowPhase(
            projection,
            attempts,
            (ReconciliationEntryOutcome.REFUSED, "a deterministic container name is present"),
        )

    # M1: enter RECONCILING before any mutation; the only increment.
    if projection.state is not LifecycleState.RECONCILING:
        attempts += 1
        try:
            projection = _publish_projection_state(
                run_dir_fd, projection, state=LifecycleState.RECONCILING, attempts_total=attempts
            )
        except LifecycleStoreError as exc:
            return _CreatingRowPhase(
                projection,
                attempts,
                (ReconciliationEntryOutcome.FAILED, f"the RECONCILING projection write failed ({exc.reason.value})"),
            )

    # M2: remove the empty leaf, if one was found.
    if leaf is None:
        return _CreatingRowPhase(projection, attempts, None)
    removal = leaf.remove_if_still_empty()
    trace["removal"] = removal.kind.value
    if removal.kind is LeafRemovalKind.POST_ABSENT:
        trace["leaf"] = "removed" if removal.rmdir is RmdirReport.SUCCEEDED else "absent_after_failed_rmdir"
        return _CreatingRowPhase(projection, attempts, None)
    outcome, leaf_outcome = _LEAF_REMOVAL_DISPOSITION[removal.kind]
    trace["leaf"] = leaf_outcome
    if outcome is None:
        return _CreatingRowPhase(projection, attempts, None)
    return _CreatingRowPhase(projection, attempts, (outcome, f"worktree leaf removal: {removal.kind.value}"))


@dataclass(frozen=True)
class _MarkerCheck:
    """ADR 0004 Amendment 18: the outcome of the marker-first check over a
    run directory's single listing. `result` is set when the entry is
    already decided (a final marker, valid or not, or an unsafe/over-bound
    temporary file); otherwise classification continues with `names`."""

    result: ReconciliationEntryResult | None
    names: list[str]
    temp_count: int


def _check_abandonment(
    run_dir_fd: int, *, lifecycle_id: str, state_root, identity: RepositoryIdentity
) -> _MarkerCheck:
    try:
        names = list_directory_entries(run_dir_fd)
    except LifecycleFsError:
        return _MarkerCheck(
            ReconciliationEntryResult(
                lifecycle_id, ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "run directory contents could not be listed"
            ),
            [],
            0,
        )
    if MARKER_FILENAME in names:
        # Only a final marker decides the marker path -- and it decides it
        # before the inner-entry check or the projection (an entry abandoned
        # because those are corrupt must stay abandoned).
        try:
            marker = load_abandonment_marker(
                run_dir_fd,
                names=names,
                expected_lifecycle_id=lifecycle_id,
                expected_repo_key=identity.repo_key,
                expected_state_root_id=state_root.state_root_id,
            )
        except AbandonmentMarkerError as exc:
            outcome = (
                ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
                if exc.reason is AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE
                else ReconciliationEntryOutcome.REFUSED
            )
            return _MarkerCheck(ReconciliationEntryResult(lifecycle_id, outcome, "abandonment.json is invalid or unreadable"), names, 0)
        if marker is None:
            return _MarkerCheck(
                ReconciliationEntryResult(
                    lifecycle_id,
                    ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
                    "abandonment.json vanished between listing and inspection",
                ),
                names,
                0,
            )
        if marker.disposition is AbandonmentDisposition.ABANDONED:
            result = ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.SKIPPED_ABANDONED,
                "abandoned entry: no attributable resource remained when it was abandoned",
                abandonment_disposition=marker.disposition.value,
            )
        else:
            result = ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.SKIPPED_ABANDONED_UNRESOLVED,
                "abandoned entry with acknowledged unresolved resources; never clean",
                abandonment_disposition=marker.disposition.value,
                abandonment_remaining=dict(marker.remaining),
            )
        return _MarkerCheck(result, names, 0)
    try:
        temp_count = count_stale_marker_temps(run_dir_fd, names)
    except AbandonmentMarkerError as exc:
        outcome = (
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
            if exc.reason is AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE
            else ReconciliationEntryOutcome.REFUSED
        )
        return _MarkerCheck(ReconciliationEntryResult(lifecycle_id, outcome, "an abandonment temporary file is unsafe"), names, 0)
    if temp_count > ABANDONMENT_TEMP_MAX:
        return _MarkerCheck(
            ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.REFUSED,
                "more abandonment temporary files exist than CodeAgent can create",
                abandonment_temp_leftovers=temp_count,
            ),
            names,
            temp_count,
        )
    return _MarkerCheck(None, names, temp_count)


def _process_open_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Marker-first (ADR 0004 Amendment 18), then validates the run
    directory's own recognized contents, then delegates to
    `_process_open_entry_body` for the terminal-peek/lock/reconcile flow.
    `has_temp_leftover` and `abandonment_temp_leftovers` are determined
    once, up front, and applied to whichever result the body returns."""
    marker_check = _check_abandonment(run_dir_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity)
    if marker_check.result is not None:
        return marker_check.result
    inner_check = _check_inner_entries(run_dir_fd, marker_check.names)
    if inner_check.outcome is not None:
        return ReconciliationEntryResult(
            lifecycle_id,
            inner_check.outcome,
            inner_check.detail,
            has_temp_leftover=inner_check.has_temp_leftover,
            abandonment_temp_leftovers=marker_check.temp_count,
        )

    result = _process_open_entry_body(
        run_dir_fd=run_dir_fd,
        lifecycle_id=lifecycle_id,
        state_root=state_root,
        identity=identity,
        context=context,
    )
    return replace(
        result, has_temp_leftover=inner_check.has_temp_leftover, abandonment_temp_leftovers=marker_check.temp_count
    )


@dataclass(frozen=True)
class _Classification:
    result: ReconciliationEntryResult | None
    peek: LifecycleProjection | None


def _classify_open_entry(
    *, run_dir_fd: int, lifecycle_id: str, state_root, identity: RepositoryIdentity
) -> _Classification:
    """The pre-lock part of entry processing, shared by the real pass and
    the dry-run planner (ADR 0004 Amendment 18): returns a decided result
    for a terminal, refused, or unloadable entry, or `result=None` with the
    peeked projection for a reconciliation-eligible one."""
    # Pre-lock terminal peek: zero lock/inspection calls for a
    # recognized clean-final entry (ADR 0004 section 10's own
    # requirement). Never trusted for anything beyond this decision —
    # the nonterminal path below re-reads fresh, after the lock.
    try:
        peek = load_lifecycle_projection(
            run_dir_fd,
            object_format=identity.object_format,
            expected_lifecycle_id=lifecycle_id,
            expected_repo_key=identity.repo_key,
            expected_state_root_id=state_root.state_root_id,
        )
    except LifecycleStoreError as exc:
        return _Classification(
            ReconciliationEntryResult(lifecycle_id, _classify_projection_load_failure(exc), "lifecycle.json could not be loaded"),
            None,
        )

    if peek.state in (LifecycleState.COMPLETE, LifecycleState.RECONCILED):
        if is_projection_fully_absent_shape(peek):
            return _Classification(
                ReconciliationEntryResult(
                    lifecycle_id,
                    ReconciliationEntryOutcome.SKIPPED_TERMINAL,
                    "terminal entry recognized with fully absent attribution",
                    run_id=peek.run_id,
                    attempt_number=peek.reconciliation.attempts_total,
                ),
                peek,
            )
        return _Classification(
            ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.REFUSED,
                "terminal entry has non-absent attribution or a populated failure",
                run_id=peek.run_id,
                attempt_number=peek.reconciliation.attempts_total,
            ),
            peek,
        )

    if not _is_reconciliation_eligible(peek):
        return _Classification(
            ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.REFUSED,
                "entry state or attribution is not recognized by this slice",
                run_id=peek.run_id,
                attempt_number=peek.reconciliation.attempts_total,
            ),
            peek,
        )
    return _Classification(None, peek)


def _process_open_entry_body(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    classification = _classify_open_entry(
        run_dir_fd=run_dir_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity
    )
    if classification.result is not None:
        return classification.result
    peek = classification.peek

    try:
        lock = acquire_lifecycle_lock(
            run_dir_fd,
            repo_key=identity.repo_key,
            lifecycle_id=lifecycle_id,
            diagnostic_path=f"<state-root>/repos/{identity.repo_key}/{RUNS_DIRNAME}/{lifecycle_id}/lifecycle.lock",
        )
    except LockError as exc:
        if exc.reason is LockFailure.BUSY:
            return ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.SKIPPED_ACTIVE,
                "the lifecycle lock is held by a live process",
                run_id=peek.run_id,
                attempt_number=peek.reconciliation.attempts_total,
            )
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "the lifecycle lock could not be acquired",
            run_id=peek.run_id,
            attempt_number=peek.reconciliation.attempts_total,
        )

    lock_exc: BaseException | None = None
    result: ReconciliationEntryResult | None = None
    try:
        result = _reconcile_locked_entry(
            run_dir_fd=run_dir_fd,
            lifecycle_id=lifecycle_id,
            state_root=state_root,
            identity=identity,
            context=context,
        )
    except BaseException as exc:  # noqa: BLE001 - every cleanup stage is still attempted below
        lock_exc = exc
    try:
        lock.release()
    except LockError as release_exc:
        if lock_exc is not None:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the lifecycle lock could not be confirmed released"
            ) from lock_exc
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the lifecycle lock could not be confirmed released"
        ) from release_exc
    if lock_exc is not None:
        raise lock_exc
    assert result is not None
    return result


def _process_entry(
    *,
    runs_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    try:
        run_dir_fd = open_existing_directory_chain_if_present(runs_fd, [lifecycle_id])
    except LifecycleFsError as exc:
        return ReconciliationEntryResult(
            lifecycle_id, _classify_entry_fs_failure(exc), "the run directory could not be safely opened"
        )
    if run_dir_fd is None:
        # v1 never deletes a run directory once created (ADR 0004
        # section 6), so this means the directory vanished between
        # `_enumerate_runs`'s listing and this open — a genuine
        # inconsistency, not an observed wrong state.
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "the run directory vanished between enumeration and opening",
        )

    process_exc: BaseException | None = None
    result: ReconciliationEntryResult | None = None
    try:
        result = _process_open_entry(
            run_dir_fd=run_dir_fd,
            lifecycle_id=lifecycle_id,
            state_root=state_root,
            identity=identity,
            context=context,
        )
    except BaseException as exc:  # noqa: BLE001 - the descriptor is still closed below
        process_exc = exc
    try:
        close_confirmed([run_dir_fd])
    except LifecycleFsError as close_exc:
        if process_exc is not None:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                "a run-directory descriptor could not be confirmed closed",
            ) from process_exc
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "a run-directory descriptor could not be confirmed closed"
        ) from close_exc
    if process_exc is not None:
        raise process_exc
    assert result is not None
    return result


@unique
class _MaintenanceEventType(str, Enum):
    STARTED = "ReconciliationStarted"
    ENTRY_RECORDED = "ReconciliationEntryRecorded"
    FINISHED = "ReconciliationFinished"
    ABANDONMENT_RECORDED = "AbandonmentRecorded"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded(value: str | None, max_bytes: int) -> str | None:
    """Defensive truncation for a value this module does not itself
    control the length of (a `run_id`/ref name already bounded
    upstream) — never silently corrupts a fixed categorical `detail`
    literal, which is always written short by construction."""
    if value is None:
        return None
    encoded = value.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class _MaintenanceTraceWriter:
    """One exclusive private file per pass
    (`repos/<repo_key>/maintenance/<maintenance_id>.jsonl`), never
    reopened or resumed by a later pass. Every event is written and
    individually `fsync`ed; the containing directory is `fsync`ed once,
    at file-creation time."""

    def __init__(
        self,
        *,
        fd: int,
        maintenance_id: str,
        state_root_id: str,
        repo_key: str,
        trigger: MaintenanceTrigger = MaintenanceTrigger.PRE_RUN,
    ) -> None:
        self._fd = fd
        self._maintenance_id = maintenance_id
        self._state_root_id = state_root_id
        self._repo_key = repo_key
        self._trigger = MaintenanceTrigger(trigger).value

    def _write_event(self, event: dict) -> None:
        line = canonical_json_dumps(event) + b"\n"
        if len(line) > MAINTENANCE_EVENT_MAX_BYTES:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "a maintenance-trace event exceeded its fixed size bound"
            )
        try:
            write_all_eintr_safe(self._fd, line)
            fsync_fd(self._fd)
        except LifecycleFsError as exc:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                "a maintenance-trace event could not be durably written",
            ) from exc

    def started(self) -> None:
        self._write_event(
            {
                "schema_version": 1,
                "event_type": _MaintenanceEventType.STARTED.value,
                "maintenance_id": self._maintenance_id,
                "state_root_id": self._state_root_id,
                "repo_key": self._repo_key,
                "trigger": self._trigger,
                "timestamp": _timestamp(),
            }
        )

    def entry_recorded(self, result: ReconciliationEntryResult) -> None:
        ref_name = _bounded(f"refs/codeagent/runs/{result.lifecycle_id}/checkpoint", _REF_NAME_MAX_BYTES)
        self._write_event(
            {
                "schema_version": 1,
                "event_type": _MaintenanceEventType.ENTRY_RECORDED.value,
                "maintenance_id": self._maintenance_id,
                "state_root_id": self._state_root_id,
                "repo_key": self._repo_key,
                "trigger": self._trigger,
                "timestamp": _timestamp(),
                "lifecycle_id": result.lifecycle_id,
                "run_id": _bounded(result.run_id, RUN_ID_MAX_ENCODED_BYTES),
                "outcome": result.outcome.value,
                "attempt_number": result.attempt_number,
                "containers": {
                    "baseline": {
                        "id": _bounded(result.baseline_id, _CONTAINER_ID_MAX_BYTES),
                        "confirmed_absent": result.baseline_confirmed_absent,
                    },
                    "verification": {
                        "id": _bounded(result.verification_id, _CONTAINER_ID_MAX_BYTES),
                        "confirmed_absent": result.verification_confirmed_absent,
                    },
                },
                "worktree": {
                    "confirmed_absent": result.worktree_confirmed_absent,
                    "initial_persisted_intent": result.worktree_initial_persisted_intent,
                    "leaf_outcome": result.worktree_leaf_outcome,
                    "removal_observation": result.worktree_removal_observation,
                    "absent_transition_confirmed_this_pass": (
                        result.worktree_absent_transition_confirmed_this_pass
                    ),
                    "registration_pre": result.worktree_registration_pre,
                    "registration_post": result.worktree_registration_post,
                    "admin_matches_pre": result.worktree_admin_matches_pre,
                    "admin_matches_post": result.worktree_admin_matches_post,
                    "leaf_pre": result.worktree_leaf_pre,
                    "leaf_post": result.worktree_leaf_post,
                    "removal_attempt": result.worktree_removal_attempt,
                    "disposing_transition_confirmed_this_pass": (
                        result.worktree_disposing_transition_confirmed_this_pass
                    ),
                },
                "checkpoint_ref": {
                    "ref_name": ref_name,
                    "confirmed_absent": result.checkpoint_ref_confirmed_absent,
                    "initial_persisted_intent": result.checkpoint_ref_initial_persisted_intent,
                    "observation_pre": result.checkpoint_ref_observation_pre,
                    "candidate_role": result.checkpoint_ref_candidate_role,
                    "removal_attempt": result.checkpoint_ref_removal_attempt,
                    "removing_transition_confirmed_this_pass": (
                        result.checkpoint_ref_removing_transition_confirmed_this_pass
                    ),
                    "absent_transition_confirmed_this_pass": (
                        result.checkpoint_ref_absent_transition_confirmed_this_pass
                    ),
                    "gate_observation": result.checkpoint_ref_gate_observation,
                    "container_gate": result.checkpoint_ref_container_gate,
                },
                "has_recognized_temp_leftover": result.has_temp_leftover,
                "abandonment": (
                    None
                    if result.abandonment_disposition is None
                    else {"disposition": result.abandonment_disposition, "remaining": result.abandonment_remaining}
                ),
                "abandonment_temp_leftovers": min(result.abandonment_temp_leftovers, ABANDONMENT_TEMP_MAX + 1),
                "detail": _bounded(result.detail, _DETAIL_MAX_BYTES),
            }
        )

    def finished(self, *, entries: tuple[ReconciliationEntryResult, ...], blocked: bool) -> None:
        counts = {outcome: 0 for outcome in ReconciliationEntryOutcome}
        for entry in entries:
            counts[entry.outcome] += 1
        self._write_event(
            {
                "schema_version": 1,
                "event_type": _MaintenanceEventType.FINISHED.value,
                "maintenance_id": self._maintenance_id,
                "state_root_id": self._state_root_id,
                "repo_key": self._repo_key,
                "trigger": self._trigger,
                "timestamp": _timestamp(),
                "entries_total": len(entries),
                "entries_reconciled": counts[ReconciliationEntryOutcome.RECONCILED],
                "entries_skipped_terminal": counts[ReconciliationEntryOutcome.SKIPPED_TERMINAL],
                "entries_skipped_active": counts[ReconciliationEntryOutcome.SKIPPED_ACTIVE],
                "entries_refused": counts[ReconciliationEntryOutcome.REFUSED],
                "entries_failed": counts[ReconciliationEntryOutcome.FAILED],
                "entries_substrate_unavailable": counts[ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE],
                "entries_skipped_abandoned": counts[ReconciliationEntryOutcome.SKIPPED_ABANDONED],
                "entries_skipped_abandoned_unresolved": counts[ReconciliationEntryOutcome.SKIPPED_ABANDONED_UNRESOLVED],
                "blocked": blocked,
            }
        )

    def abandonment_recorded(
        self,
        *,
        lifecycle_id: str,
        disposition: AbandonmentDisposition,
        run_id: str | None,
        projection_status: str,
        remaining: dict,
        reason_recorded: bool,
        marker_publication: str,
        abandonment_temp_leftovers: int,
    ) -> None:
        """ADR 0004 section 12 / Amendment 18. Retrospective: written only
        after the marker's hard link succeeded. Deliberately takes no reason
        argument -- the operator's free-form reason is persisted only in
        abandonment.json; this event records `reason_recorded` alone."""
        self._write_event(
            {
                "schema_version": 1,
                "event_type": _MaintenanceEventType.ABANDONMENT_RECORDED.value,
                "maintenance_id": self._maintenance_id,
                "state_root_id": self._state_root_id,
                "repo_key": self._repo_key,
                "trigger": self._trigger,
                "timestamp": _timestamp(),
                "lifecycle_id": lifecycle_id,
                "run_id": _bounded(run_id, RUN_ID_MAX_ENCODED_BYTES),
                "disposition": AbandonmentDisposition(disposition).value,
                "projection_status": projection_status,
                "remaining": {key: remaining[key] for key in RESOURCE_FIELDS},
                "reason_recorded": bool(reason_recorded),
                "marker_publication": marker_publication,
                "abandonment_temp_leftovers": min(abandonment_temp_leftovers, ABANDONMENT_TEMP_MAX + 1),
            }
        )

    @property
    def maintenance_id(self) -> str:
        return self._maintenance_id

    def close(self) -> None:
        try:
            close_confirmed([self._fd])
        except LifecycleFsError as exc:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the maintenance-trace file could not be confirmed closed"
            ) from exc


def _open_maintenance_trace(
    state_root, repo_dir_fd: int, repo_key: str, *, trigger: MaintenanceTrigger = MaintenanceTrigger.PRE_RUN
) -> _MaintenanceTraceWriter:
    """Create this pass's exclusive trace file (see `_open_maintenance_trace_file`).
    Any `ReconciliationError` raised once the file exists carries its
    `maintenance_id` (Amendment 18), so "a trace file exists but its setup
    was not confirmed" is never reported as "no trace was created"."""
    created: list[str] = []
    try:
        return _open_maintenance_trace_file(state_root, repo_dir_fd, repo_key, trigger=trigger, created=created)
    except ReconciliationError as exc:
        if created and exc.maintenance_id is None:
            exc.maintenance_id = created[0]
        raise


def _open_maintenance_trace_file(
    state_root, repo_dir_fd: int, repo_key: str, *, trigger: MaintenanceTrigger, created: list[str]
) -> _MaintenanceTraceWriter:
    """Open (create) this pass's exclusive maintenance-trace file.

    Descriptor ownership is explicit: `maintenance_dir_fd` is never
    transferred anywhere and is always closed by this function, on
    every path, before it returns or raises. `fd` (the trace file's
    own descriptor) is transferred to the returned `_MaintenanceTraceWriter`
    only on the final successful return; every failure path that
    reaches a point where `fd` is open closes it too, so it can never
    become unreachable. A cleanup failure at any stage dominates and is
    chained from whatever failure was already active (this module's
    own copy of the established close-confirmed-or-chain convention),
    and no raw `LifecycleFsError` is ever allowed to escape this
    function — every site converts it to `ReconciliationError` first.
    """
    maintenance_id = secrets.token_hex(16)
    try:
        maintenance_dir_fd = open_managed_directory_chain(repo_dir_fd, [MAINTENANCE_DIRNAME])
    except LifecycleFsError as exc:
        raise ReconciliationError(
            _classify_pass_level_fs_failure(exc), "the maintenance/ directory could not be opened or created"
        ) from exc

    fd: int | None = None
    try:
        try:
            fd = open_private_create_exclusive_at(maintenance_dir_fd, f"{maintenance_id}.jsonl", 0o600)
        except FileExistsError as exc:
            # An entry with this fresh random name already existed: not ours.
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the maintenance-trace file could not be created"
            ) from exc
        except LifecycleFsError as exc:
            # The exclusive create may have succeeded before a later step
            # (mode/ownership verification) failed; report what is on disk.
            try:
                os.lstat(f"{maintenance_id}.jsonl", dir_fd=maintenance_dir_fd)
            except OSError:
                pass
            else:
                created.append(maintenance_id)
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the maintenance-trace file could not be created"
            ) from exc
        created.append(maintenance_id)
        try:
            fsync_fd(maintenance_dir_fd)
        except LifecycleFsError as exc:
            # The directory-fsync failure is the primary cause; if
            # closing the just-created trace file also fails, that
            # cleanup failure dominates the report but is still
            # chained from this original cause (never from itself).
            try:
                close_confirmed([fd])
            except LifecycleFsError:
                raise ReconciliationError(
                    ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                    "the maintenance-trace file could not be confirmed closed after a directory-fsync failure",
                ) from exc
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                "the maintenance/ directory could not be confirmed durable after file creation",
            ) from exc
    except BaseException as exc:
        # `fd` (if it was ever opened) has already been closed by the
        # inner handler above on this path; only `maintenance_dir_fd`
        # remains to be attempted here.
        try:
            close_confirmed([maintenance_dir_fd])
        except LifecycleFsError:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                "the maintenance/ directory descriptor could not be confirmed closed",
            ) from exc
        raise
    else:
        try:
            close_confirmed([maintenance_dir_fd])
        except LifecycleFsError as exc:
            # Success so far, but maintenance_dir_fd's own close
            # failed: `fd` has not been closed anywhere on this path
            # and must not be leaked or left unreachable.
            try:
                close_confirmed([fd])
            except LifecycleFsError:
                raise ReconciliationError(
                    ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                    "neither the maintenance/ directory descriptor nor the trace-file descriptor "
                    "could be confirmed closed",
                ) from exc
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                "the maintenance/ directory descriptor could not be confirmed closed",
            ) from exc

    return _MaintenanceTraceWriter(
        fd=fd,
        maintenance_id=maintenance_id,
        state_root_id=state_root.state_root_id,
        repo_key=repo_key,
        trigger=trigger,
    )


def open_maintenance_trace(
    state_root, repo_dir_fd: int, repo_key: str, *, trigger: MaintenanceTrigger
) -> _MaintenanceTraceWriter:
    """Public entry to the exclusive maintenance-trace opener, for the
    explicit maintenance commands (ADR 0004 Amendment 18)."""
    return _open_maintenance_trace(state_root, repo_dir_fd, repo_key, trigger=trigger)


def reconcile_repository(
    *,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    repository_lock: LockHandle,
    trigger: MaintenanceTrigger = MaintenanceTrigger.PRE_RUN,
) -> ReconciliationPassResult:
    """Automatic pre-run reconciliation (ADR 0004 Amendment 1 section
    10, Amendment 2), called by `lifecycle_store.prepare_lifecycle()`
    while `repository_lock` is already held. Never raises for a
    per-entry problem (captured as an outcome in the returned result);
    raises `ReconciliationError` only for a whole-pass infrastructure
    failure (an unusable `runs/`/`maintenance/` namespace, an
    unconfirmed cleanup, or a wrong locking precondition) — the caller
    treats that identically to `blocked=True`.

    `trigger` is `PRE_RUN` for admission and `EXPLICIT` for
    `codeagent reconcile` (ADR 0004 section 11 / Amendment 18); the pass
    itself is identical.
    """
    trigger = MaintenanceTrigger(trigger)
    trace_ids: list[str] = []
    try:
        return _reconcile_repository_pass(
            state_root=state_root,
            identity=identity,
            context=context,
            repository_lock=repository_lock,
            trigger=trigger,
            trace_ids=trace_ids,
        )
    except ReconciliationError as exc:
        # A pass that fails after its trace file exists reports that file.
        if trace_ids and exc.maintenance_id is None:
            exc.maintenance_id = trace_ids[0]
        raise


def _reconcile_repository_pass(
    *,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    repository_lock: LockHandle,
    trigger: MaintenanceTrigger,
    trace_ids: list[str],
) -> ReconciliationPassResult:
    _require_repository_lock_scope(repository_lock, identity.repo_key)
    validate_hex32(identity.repo_key, field_name="repo_key")

    try:
        repo_dir_fd = state_root.open_repo_dir(identity.repo_key)
    except LifecycleFsError as exc:
        raise ReconciliationError(
            _classify_pass_level_fs_failure(exc), "the repository directory could not be opened"
        ) from exc
    body_exc: BaseException | None = None
    entries: tuple[ReconciliationEntryResult, ...] = ()
    blocked = True
    maintenance_id = ""
    try:
        trace = _open_maintenance_trace(state_root, repo_dir_fd, identity.repo_key, trigger=trigger)
        maintenance_id = trace.maintenance_id
        trace_ids.append(maintenance_id)
        try:
            trace.started()

            try:
                runs_fd = open_managed_directory_chain(repo_dir_fd, [RUNS_DIRNAME])
            except LifecycleFsError as exc:
                raise ReconciliationError(
                    _classify_pass_level_fs_failure(exc), "the runs/ directory could not be opened or created"
                ) from exc
            runs_exc: BaseException | None = None
            collected: list[ReconciliationEntryResult] = []
            try:
                lifecycle_ids = _enumerate_runs(runs_fd)
                for lifecycle_id in lifecycle_ids:
                    result = _process_entry(
                        runs_fd=runs_fd,
                        lifecycle_id=lifecycle_id,
                        state_root=state_root,
                        identity=identity,
                        context=context,
                    )
                    collected.append(result)
                    trace.entry_recorded(result)
            except BaseException as exc:  # noqa: BLE001 - runs_fd is still closed below
                runs_exc = exc
            try:
                close_confirmed([runs_fd])
            except LifecycleFsError as close_exc:
                raise ReconciliationError(
                    ReconciliationFailure.SUBSTRATE_UNAVAILABLE,
                    "the runs/ directory descriptor could not be confirmed closed",
                ) from (runs_exc if runs_exc is not None else close_exc)
            if runs_exc is not None:
                raise runs_exc

            entries = tuple(collected)
            blocked = any(
                entry.outcome
                in (
                    ReconciliationEntryOutcome.REFUSED,
                    ReconciliationEntryOutcome.FAILED,
                    ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
                    ReconciliationEntryOutcome.SKIPPED_ACTIVE,
                )
                for entry in entries
            )
            trace.finished(entries=entries, blocked=blocked)
        except BaseException as exc:  # noqa: BLE001 - the trace is still closed below
            body_exc = exc
        try:
            trace.close()
        except ReconciliationError as close_exc:
            if body_exc is not None:
                raise close_exc from body_exc
            raise
        if body_exc is not None:
            raise body_exc
    except BaseException as exc:  # noqa: BLE001 - repo_dir_fd is still closed below
        body_exc = exc
    try:
        close_confirmed([repo_dir_fd])
    except LifecycleFsError as close_exc:
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the repository directory descriptor could not be confirmed closed"
        ) from (body_exc if body_exc is not None else close_exc)
    if body_exc is not None:
        raise body_exc

    return ReconciliationPassResult(maintenance_id=maintenance_id, entries=entries, blocked=blocked)


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 18: read-only resource inspection and dry-run planning.
# Nothing below writes, creates, removes, or publishes anything.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemainingResources:
    """A fresh, read-only observation of every attributable resource of one
    lifecycle, derived only from trusted identities and deterministic names.
    Each field is "absent", "present", or "unknown" (inspection failed);
    "unknown" is never absence.

    `cleanup_unconfirmed` is separate from resource presence: it names each
    observer ("docker_listing", "worktree_listing", "admin_scan",
    "leaf_observation") whose failure was CodeAgent's *own* unconfirmed
    process or descriptor cleanup. Forced abandonment may acknowledge an
    ordinary "unknown"; it never acknowledges this (Amendment 18)."""

    baseline_container: str
    verification_container: str
    worktree_registration: str
    worktree_admin_entry: str
    worktree_directory: str
    checkpoint_ref: str
    cleanup_unconfirmed: tuple[str, ...] = ()

    @property
    def all_absent(self) -> bool:
        return all(value == "absent" for value in self.to_summary().values())

    def to_summary(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in RESOURCE_FIELDS}


_CLEANUP_CHAIN_MAX_OBJECTS = 32


def _is_own_cleanup_failure(exc: BaseException) -> bool:
    """True when `exc`, or any exception reachable from it through
    `__cause__` *or* `__context__`, reports that CodeAgent's own process or
    descriptor cleanup could not be confirmed -- as distinct from an
    ordinary failed observation.

    Both links are followed (an explicit cause does not hide the context),
    including a context suppressed by `raise ... from ...`, because it may
    hold the same real cleanup failure. The traversal is iterative, protects
    against cycles by object identity, and inspects at most
    `_CLEANUP_CHAIN_MAX_OBJECTS` distinct exceptions."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(seen) < _CLEANUP_CHAIN_MAX_OBJECTS:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LifecycleFsError) and current.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            return True
        if isinstance(current, BoundedProcessError) and current.reason in (
            BoundedProcessFailure.TERMINATION_UNCONFIRMED,
            BoundedProcessFailure.CLEANUP_UNCONFIRMED,
        ):
            return True
        if isinstance(current, GitSafetyError) and current.reason is GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED:
            return True
        for link in (current.__context__, current.__cause__):
            if link is not None and id(link) not in seen:
                pending.append(link)
    return False


_ADMIN_SCAN_SUMMARY = {
    _AdminScanResult.NONE_FOUND: "absent",
    _AdminScanResult.MATCH_FOUND: "present",
}

_LEAF_SUMMARY = {
    MaterializedLeafObservation.ABSENT: "absent",
    MaterializedLeafObservation.MATERIALIZED: "present",
    MaterializedLeafObservation.CONFLICT: "present",
}


def inspect_remaining_resources(
    *,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    lifecycle_id: str,
    projection: LifecycleProjection | None,
) -> RemainingResources:
    """ADR 0004 section 11's fresh exact inspection for abandonment and the
    dry run. Uses only read-only observers: one Docker listing, one Git
    worktree listing, one bounded admin-name scan, one descriptor-relative
    leaf observation, and one checkpoint-ref observation. With an
    unreadable projection (`None`) any container at a role name counts as
    remaining; with a readable one, a recorded container id listed under any
    name also counts. Never raises for an inspection failure: it reports
    "unknown"."""
    validate_hex32(lifecycle_id, field_name="lifecycle_id")
    cleanup: list[str] = []
    baseline = verification = "unknown"
    try:
        name_to_id, id_to_name = _docker_ps_all_id_name_pairs()
    except _DockerListingError as exc:
        if _is_own_cleanup_failure(exc):
            cleanup.append("docker_listing")
    else:

        def _container(role: str, recorded_id: str | None) -> str:
            if f"codeagent-{role}-{lifecycle_id}" in name_to_id:
                return "present"
            if recorded_id is not None and recorded_id in id_to_name:
                return "present"
            return "absent"

        baseline = _container("baseline", projection.baseline.id if projection is not None else None)
        verification = _container("verification", projection.verification.id if projection is not None else None)

    target_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))
    registration = _observe_target_registration(
        context.working_tree_root,
        oid_hex_len=ObjectFormat(identity.object_format).hex_length,
        target_path=target_path,
        cleanup_failures=cleanup,
    ).state

    try:
        admin = _ADMIN_SCAN_SUMMARY.get(_scan_worktree_admin_entries(identity.canonical_common_dir, lifecycle_id), "unknown")
    except LifecycleFsError as exc:
        admin = "unknown"
        if _is_own_cleanup_failure(exc):
            cleanup.append("admin_scan")

    try:
        directory = _LEAF_SUMMARY.get(state_root.observe_materialized_worktree_leaf(identity.repo_key, lifecycle_id), "unknown")
    except LifecycleFsError as exc:
        directory = "unknown"
        if _is_own_cleanup_failure(exc):
            cleanup.append("leaf_observation")

    ref = "unknown"
    try:
        checkpoint_ref = CheckpointRef(context.working_tree_root, lifecycle_id)
    except (ValueError, CheckpointRefError):
        checkpoint_ref = None
    if checkpoint_ref is not None and checkpoint_ref.object_format.value == identity.object_format:
        try:
            ref = "present" if checkpoint_ref.observe().present else "absent"
        except CheckpointRefError as exc:
            ref = "present" if exc.reason is CheckpointRefFailure.SYMBOLIC_REF else "unknown"

    return RemainingResources(
        baseline_container=baseline,
        verification_container=verification,
        worktree_registration=registration,
        worktree_admin_entry=admin,
        worktree_directory=directory,
        checkpoint_ref=ref,
        cleanup_unconfirmed=tuple(cleanup),
    )


@unique
class PlanEntryOutcome(str, Enum):
    """Dry-run vocabulary (ADR 0004 Amendment 18). Never persisted and never
    part of a `ReconciliationPassResult`. `PENDING` means "eligible for a
    real reconciliation pass, whose outcome is not predicted"; it blocks."""

    SKIPPED_TERMINAL = "skipped_terminal"
    SKIPPED_ABANDONED = "skipped_abandoned"
    SKIPPED_ABANDONED_UNRESOLVED = "skipped_abandoned_unresolved"
    SKIPPED_ACTIVE = "skipped_active"
    REFUSED = "refused"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    PENDING = "pending"


_PLAN_BLOCKING = frozenset(
    {
        PlanEntryOutcome.SKIPPED_ACTIVE,
        PlanEntryOutcome.REFUSED,
        PlanEntryOutcome.SUBSTRATE_UNAVAILABLE,
        PlanEntryOutcome.PENDING,
    }
)


@dataclass(frozen=True)
class PlanEntry:
    lifecycle_id: str
    outcome: PlanEntryOutcome
    detail: str
    run_id: str | None = None
    remaining: RemainingResources | None = None
    abandonment_disposition: str | None = None
    abandonment_remaining: dict | None = None
    abandonment_temp_leftovers: int = 0


@dataclass(frozen=True)
class ReconciliationPlan:
    """A dry-run result. Deliberately has no `maintenance_id`: a dry run
    creates no maintenance trace and therefore has no maintenance identity."""

    entries: tuple[PlanEntry, ...]
    blocked: bool


def _plan_entry_from(result: ReconciliationEntryResult, *, outcome: PlanEntryOutcome | None = None, temps: int = 0) -> PlanEntry:
    return PlanEntry(
        lifecycle_id=result.lifecycle_id,
        outcome=outcome or PlanEntryOutcome(result.outcome.value),
        detail=result.detail,
        run_id=result.run_id,
        abandonment_disposition=result.abandonment_disposition,
        abandonment_remaining=result.abandonment_remaining,
        abandonment_temp_leftovers=temps or result.abandonment_temp_leftovers,
    )


def _plan_open_entry(
    *, run_dir_fd: int, lifecycle_id: str, state_root, identity: RepositoryIdentity, context: TrustedRepositoryContext
) -> PlanEntry:
    marker_check = _check_abandonment(run_dir_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity)
    if marker_check.result is not None:
        entry = _plan_entry_from(marker_check.result)
        if entry.outcome in (PlanEntryOutcome.SKIPPED_ABANDONED, PlanEntryOutcome.SKIPPED_ABANDONED_UNRESOLVED):
            return entry  # zero further calls, exactly as the real pass
        return replace(entry, remaining=_inspect_for_plan(state_root, identity, context, lifecycle_id, None))
    temps = marker_check.temp_count
    inner = _check_inner_entries(run_dir_fd, marker_check.names)
    if inner.outcome is not None:
        return PlanEntry(
            lifecycle_id,
            PlanEntryOutcome(inner.outcome.value),
            inner.detail,
            remaining=_inspect_for_plan(state_root, identity, context, lifecycle_id, None),
            abandonment_temp_leftovers=temps,
        )
    classification = _classify_open_entry(
        run_dir_fd=run_dir_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity
    )
    if classification.result is not None:
        entry = _plan_entry_from(classification.result, temps=temps)
        if entry.outcome is PlanEntryOutcome.SKIPPED_TERMINAL:
            return entry
        return replace(entry, remaining=_inspect_for_plan(state_root, identity, context, lifecycle_id, classification.peek))
    peek = classification.peek
    try:
        lock = acquire_lifecycle_lock(
            run_dir_fd,
            repo_key=identity.repo_key,
            lifecycle_id=lifecycle_id,
            diagnostic_path=f"<state-root>/repos/{identity.repo_key}/{RUNS_DIRNAME}/{lifecycle_id}/lifecycle.lock",
            create=False,  # a dry run never creates a lock file
        )
    except LockError as exc:
        if exc.reason is LockFailure.BUSY:
            return PlanEntry(
                lifecycle_id,
                PlanEntryOutcome.SKIPPED_ACTIVE,
                "the lifecycle lock is held by a live process",
                run_id=peek.run_id,
                abandonment_temp_leftovers=temps,
            )
        outcome, detail = (
            (PlanEntryOutcome.REFUSED, "lifecycle.lock is missing beside a valid projection")
            if exc.reason is LockFailure.ABSENT
            else (PlanEntryOutcome.SUBSTRATE_UNAVAILABLE, "the lifecycle lock could not be probed")
        )
        return PlanEntry(
            lifecycle_id,
            outcome,
            detail,
            run_id=peek.run_id,
            remaining=_inspect_for_plan(state_root, identity, context, lifecycle_id, peek),
            abandonment_temp_leftovers=temps,
        )
    try:
        lock.release()
    except LockError as exc:
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the lifecycle lock probe could not be confirmed released"
        ) from exc
    return PlanEntry(
        lifecycle_id,
        PlanEntryOutcome.PENDING,
        "eligible for reconciliation; run codeagent reconcile (outcome not predicted)",
        run_id=peek.run_id,
        remaining=_inspect_for_plan(state_root, identity, context, lifecycle_id, peek),
        abandonment_temp_leftovers=temps,
    )


def _inspect_for_plan(state_root, identity, context, lifecycle_id, projection) -> RemainingResources:
    return inspect_remaining_resources(
        state_root=state_root, identity=identity, context=context, lifecycle_id=lifecycle_id, projection=projection
    )


def plan_repository(
    *,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    repository_lock: LockHandle,
) -> ReconciliationPlan:
    """`codeagent reconcile --dry-run` (ADR 0004 section 11, Amendment 18):
    the real pass's classification plus read-only resource observation,
    without any write. Never opens or creates `maintenance/` or `runs/`,
    never creates a lock file (lifecycle locks are probed with
    `create=False` and released at once), never writes a projection, a
    marker, or a trace, and never predicts a row's outcome: an eligible
    entry is `PENDING` and blocks. Raises `ReconciliationError` for a
    whole-namespace problem, exactly as the real pass."""
    _require_repository_lock_scope(repository_lock, identity.repo_key)
    validate_hex32(identity.repo_key, field_name="repo_key")
    try:
        runs_fd = open_existing_directory_chain_if_present(state_root.root_fd, ["repos", identity.repo_key, RUNS_DIRNAME])
    except LifecycleFsError as exc:
        raise ReconciliationError(_classify_pass_level_fs_failure(exc), "the runs/ directory could not be opened") from exc
    if runs_fd is None:
        return ReconciliationPlan(entries=(), blocked=False)

    body_exc: BaseException | None = None
    entries: list[PlanEntry] = []
    try:
        for lifecycle_id in _enumerate_runs(runs_fd):
            entries.append(
                _plan_entry(runs_fd=runs_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity, context=context)
            )
    except BaseException as exc:  # noqa: BLE001 - runs_fd is still closed below
        body_exc = exc
    try:
        close_confirmed([runs_fd])
    except LifecycleFsError as close_exc:
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the runs/ directory descriptor could not be confirmed closed"
        ) from (body_exc if body_exc is not None else close_exc)
    if body_exc is not None:
        raise body_exc
    return ReconciliationPlan(entries=tuple(entries), blocked=any(e.outcome in _PLAN_BLOCKING for e in entries))


def _plan_entry(
    *, runs_fd: int, lifecycle_id: str, state_root, identity: RepositoryIdentity, context: TrustedRepositoryContext
) -> PlanEntry:
    try:
        run_dir_fd = open_existing_directory_chain_if_present(runs_fd, [lifecycle_id])
    except LifecycleFsError as exc:
        return PlanEntry(
            lifecycle_id, PlanEntryOutcome(_classify_entry_fs_failure(exc).value), "the run directory could not be safely opened"
        )
    if run_dir_fd is None:
        return PlanEntry(
            lifecycle_id, PlanEntryOutcome.SUBSTRATE_UNAVAILABLE, "the run directory vanished between enumeration and opening"
        )
    body_exc: BaseException | None = None
    entry: PlanEntry | None = None
    try:
        entry = _plan_open_entry(
            run_dir_fd=run_dir_fd, lifecycle_id=lifecycle_id, state_root=state_root, identity=identity, context=context
        )
    except BaseException as exc:  # noqa: BLE001 - the descriptor is still closed below
        body_exc = exc
    try:
        close_confirmed([run_dir_fd])
    except LifecycleFsError as close_exc:
        raise ReconciliationError(
            ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "a run-directory descriptor could not be confirmed closed"
        ) from (body_exc if body_exc is not None else close_exc)
    if body_exc is not None:
        raise body_exc
    assert entry is not None
    return entry
