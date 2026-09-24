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

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, unique

from ._bounded_subprocess import BoundedProcessError, run_bounded_stdout
from ._git_safety import GIT_TIMEOUT_SECONDS, GitSafetyError, run_git_bounded
from ._lifecycle_fs import (
    LifecycleFsError,
    LifecycleFsFailure,
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
from .checkpoint_ref import LIFECYCLE_ID_RE, CheckpointRef, CheckpointRefError
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
    _publish_projection_state,
    _publish_reconciler_container_transition,
    is_projection_fully_absent_shape,
    is_projection_reconciliation_eligible_shape,
    load_lifecycle_projection,
)
from .repo_identity import RepositoryIdentity, TrustedRepositoryContext
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

_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_CONTAINER_ID_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

CONTAINER_LABEL_SCHEMA = "codeagent.lifecycle.schema"
CONTAINER_LABEL_STATE_ROOT_ID = "codeagent.lifecycle.state-root-id"
CONTAINER_LABEL_ID = "codeagent.lifecycle.id"
CONTAINER_LABEL_ROLE = "codeagent.lifecycle.role"


@unique
class ReconciliationEntryOutcome(str, Enum):
    RECONCILED = "reconciled"
    SKIPPED_TERMINAL = "skipped_terminal"
    SKIPPED_ACTIVE = "skipped_active"
    REFUSED = "refused"
    FAILED = "failed"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"


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

    def __init__(self, reason: ReconciliationFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


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


class _DockerListingError(Exception):
    pass


def _docker_ps_all_id_name_pairs() -> tuple[dict[str, str], dict[str, str]]:
    """One bounded, unfiltered, timeout-controlled, no-shell
    `docker ps -a --no-trunc --format '{{.ID}}\\t{{.Names}}'` listing of
    every container (running or stopped) — never a name-filtered or
    label-filtered query (I2). Output is capped at
    `_DOCKER_OUTPUT_MAX_BYTES` via the shared `_bounded_subprocess.
    run_bounded_stdout`, which owns the complete launch/monitor/read/
    wait/kill/confirm lifecycle; a timeout or overflow is confirmed-
    terminated before this function raises.

    Strictly parsed: each nonempty line must be exactly one
    tab-separated `id`/`name` pair, `id` exactly 64 lowercase hex
    characters, `name` matching Docker's own container-name grammar; a
    duplicate id or duplicate name anywhere in the listing makes the
    whole listing untrusted (never partially trusted), mirroring
    `executor._parse_cleanup_listing`'s own discipline. Returns both
    directions (`name -> id`, `id -> name`) from the single listing so
    a caller can detect "the id appears under a different name" without
    a second query."""
    argv = ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"]
    try:
        result = run_bounded_stdout(
            argv, timeout_seconds=_DOCKER_TIMEOUT_SECONDS, stdout_limit=_DOCKER_OUTPUT_MAX_BYTES
        )
    except BoundedProcessError as exc:
        raise _DockerListingError("docker listing failed, timed out, or exceeded its output bound") from exc
    if result.returncode != 0:
        raise _DockerListingError("docker listing exited with a nonzero status")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DockerListingError("docker listing produced invalid UTF-8 output") from exc

    if text and not text.endswith("\n"):
        raise _DockerListingError("docker listing output was missing its final line terminator")

    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    # Split strictly on `\n` only -- never `str.splitlines()`, which also
    # treats `\r`, lone `\r`, and several Unicode line separators as row
    # boundaries and would silently absorb a CRLF-terminated row as if
    # it were the expected bare-LF shape. A stray `\r` that a real CRLF
    # row would leave attached to its last field is caught below by the
    # name grammar instead (`\r` is never a legal name character).
    #
    # Empty output (`text == ""`) is valid -- zero containers -- and
    # never reaches this loop at all (`"".split("\n")[:-1] == []`).
    # Once output is nonempty, however, every row must be a genuine
    # id/name record: a blank row (leading, internal, or an extra
    # trailing one beyond the single required final LF) is fail-closed
    # rejected, never silently skipped, matching ADR 0004 Amendment 5's
    # own statement that each row is exactly one id/name record.
    for line in text.split("\n")[:-1]:
        if not line:
            raise _DockerListingError("a docker listing contained a blank row")
        fields = line.split("\t")
        if len(fields) != 2:
            raise _DockerListingError("a docker listing row was not the expected two-field shape")
        raw_id, raw_name = fields
        if not _CONTAINER_ID_HEX_RE.fullmatch(raw_id):
            raise _DockerListingError("a docker listing row's id was not the expected 64-lowercase-hex shape")
        if not _CONTAINER_NAME_RE.fullmatch(raw_name):
            raise _DockerListingError("a docker listing row's name was not the expected shape")
        if raw_id in id_to_name or raw_name in name_to_id:
            raise _DockerListingError("a docker listing contained a duplicate id or name")
        name_to_id[raw_name] = raw_id
        id_to_name[raw_id] = raw_name
    return name_to_id, id_to_name


class _DockerInspectError(Exception):
    pass


@dataclass(frozen=True)
class _InspectOwnership:
    id: str
    name: str
    labels: dict[str, str]


def _docker_inspect_ownership(candidate_id: str) -> _InspectOwnership:
    """One bounded, timeout-controlled, no-shell ownership-proof
    `docker inspect` of exactly one candidate, by its immutable id —
    never by name (a name can be reused). `_INSPECT_OWNERSHIP_MAX_BYTES`
    bounds the output; a timeout, overflow, or nonzero exit (including
    the candidate having vanished in a race between the listing and
    this call) is never treated as confirmed absence — it is a genuine
    inspection failure the caller must classify as
    `SUBSTRATE_UNAVAILABLE`, forcing a retry on a later pass rather than
    guessing."""
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
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DockerInspectError("docker inspect produced invalid UTF-8 output") from exc
    if text.count("\n") != 1 or not text.endswith("\n"):
        raise _DockerInspectError("docker inspect output was not the expected single-line shape")
    if "\r" in text:
        # A CRLF row's `\r` would otherwise survive as trailing
        # whitespace the JSON decoder silently tolerates after a
        # complete value (correction pass finding 3) -- rejected
        # explicitly here rather than relying on that decoder's own
        # leniency to ever catch it.
        raise _DockerInspectError("docker inspect output contained a carriage return")
    fields = text[:-1].split("\t")
    if len(fields) != 3:
        raise _DockerInspectError("docker inspect output was not the expected three-field shape")
    raw_id, raw_name, raw_labels_json = fields
    if not _CONTAINER_ID_HEX_RE.fullmatch(raw_id):
        raise _DockerInspectError("docker inspect id was not the expected 64-lowercase-hex shape")
    # `docker inspect`'s `.Name` always carries exactly one leading `/`
    # for a container's primary name -- a missing slash or more than one
    # is malformed output, never a valid name to strip and proceed with.
    if not raw_name.startswith("/") or raw_name.startswith("//"):
        raise _DockerInspectError("docker inspect name did not have exactly one leading '/'")
    name = raw_name[1:]
    if not _CONTAINER_NAME_RE.fullmatch(name):
        raise _DockerInspectError("docker inspect name was not the expected shape")
    try:
        labels = json.loads(raw_labels_json)
    except json.JSONDecodeError as exc:
        raise _DockerInspectError("docker inspect labels were not valid JSON") from exc
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise _DockerInspectError("docker inspect labels were not a flat string-keyed object")
    return _InspectOwnership(id=raw_id, name=name, labels=labels)


def _labels_match(labels: dict[str, str], *, state_root_id: str, lifecycle_id: str, role: str) -> bool:
    """ADR 0004 section 7's exactly-four-required-labels ownership
    check. Extra, unrecognized labels are always ignored; every one of
    the four required labels must be present with the exact expected
    value."""
    return (
        labels.get(CONTAINER_LABEL_SCHEMA) == "1"
        and labels.get(CONTAINER_LABEL_STATE_ROOT_ID) == state_root_id
        and labels.get(CONTAINER_LABEL_ID) == lifecycle_id
        and labels.get(CONTAINER_LABEL_ROLE) == role
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


def _worktree_registered_paths(working_tree_root: str) -> set[str]:
    """One bounded, timeout-controlled, no-shell, no-hooks
    `git worktree list --porcelain -z` listing of every registered
    worktree path, run through the shared hardened `run_git_bounded`
    seam (sanitized `GIT_*` environment, structured argv only, byte-
    capped output with confirmed child termination on timeout or
    overflow). `-z` NUL-delimits every field instead of newline-
    delimiting them, so a registered path containing a newline or any
    other Git-quoting-sensitive character cannot be misparsed into a
    different (or missed) path — a NUL byte can never appear in a real
    POSIX pathname, so it is an unambiguous delimiter."""
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
        raise _GitWorktreeListingError("git worktree listing failed, timed out, or exceeded its output bound") from exc

    paths: set[str] = set()
    prefix = b"worktree "
    for token in result.stdout.split(b"\x00"):
        if token.startswith(prefix):
            paths.add(os.path.normpath(os.fsdecode(token[len(prefix) :])))
    return paths


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


def _check_inner_entries(run_dir_fd: int) -> _InnerEntriesCheck:
    """Enumerate and validate a validated run directory's own
    contents. Only `lifecycle.json`, `lifecycle.lock`, and the exact
    recognized temp-publication pattern are ever recognized by name;
    anything else is an unrecognized inner entry (`REFUSED`), never
    opened or trusted. Every recognized name is additionally validated
    itself (see `_validate_recognized_inner_entry`) before being
    trusted. A recognized temp-publication leftover is never opened,
    trusted, or deleted, but its presence is reported via
    `has_temp_leftover` so the caller can carry it into the
    maintenance trace."""
    try:
        names = list_directory_entries(run_dir_fd)
    except LifecycleFsError:
        return _InnerEntriesCheck(False, ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE, "run directory contents could not be listed")

    has_temp_leftover = False
    for name in names:
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


def _reconcile_locked_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Inspect-before-mutate, per entry: load and validate the
    projection, confirm the checkpoint ref then the worktree absent
    (unchanged from Slice 3B-1, still out of removal scope), then
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

    if projection.state not in _RECONCILER_ELIGIBLE_STATES or not is_projection_reconciliation_eligible_shape(
        projection
    ):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "entry is no longer the recognized nonterminal reconciliation-eligible shape",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )

    try:
        ref = CheckpointRef(context.working_tree_root, lifecycle_id)
    except (ValueError, CheckpointRefError):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "the checkpoint ref could not be constructed",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )
    if ref.object_format.value != identity.object_format:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "the checkpoint ref's discovered object format disagrees with the trusted repository identity",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )
    try:
        observation = ref.observe()
    except CheckpointRefError:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "the checkpoint ref could not be observed",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )
    if observation.present:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "the recomputed checkpoint ref is present",
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


def _process_open_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
    """Validates the run directory's own recognized contents, then
    delegates to `_process_open_entry_body` for the terminal-peek/lock/
    reconcile flow. `has_temp_leftover` is determined once, up front,
    and applied to whichever result the body returns (including its
    own early inner-entry refusal), so every code path's maintenance-
    trace entry carries the same observed evidence."""
    inner_check = _check_inner_entries(run_dir_fd)
    if inner_check.outcome is not None:
        return ReconciliationEntryResult(
            lifecycle_id, inner_check.outcome, inner_check.detail, has_temp_leftover=inner_check.has_temp_leftover
        )

    result = _process_open_entry_body(
        run_dir_fd=run_dir_fd,
        lifecycle_id=lifecycle_id,
        state_root=state_root,
        identity=identity,
        context=context,
    )
    return replace(result, has_temp_leftover=inner_check.has_temp_leftover)


def _process_open_entry_body(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
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
        return ReconciliationEntryResult(
            lifecycle_id, _classify_projection_load_failure(exc), "lifecycle.json could not be loaded"
        )

    if peek.state in (LifecycleState.COMPLETE, LifecycleState.RECONCILED):
        if is_projection_fully_absent_shape(peek):
            return ReconciliationEntryResult(
                lifecycle_id,
                ReconciliationEntryOutcome.SKIPPED_TERMINAL,
                "terminal entry recognized with fully absent attribution",
                run_id=peek.run_id,
                attempt_number=peek.reconciliation.attempts_total,
            )
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "terminal entry has non-absent attribution or a populated failure",
            run_id=peek.run_id,
            attempt_number=peek.reconciliation.attempts_total,
        )

    if peek.state not in _RECONCILER_ELIGIBLE_STATES or not is_projection_reconciliation_eligible_shape(peek):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "entry state or attribution is not recognized by this slice",
            run_id=peek.run_id,
            attempt_number=peek.reconciliation.attempts_total,
        )

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

    def __init__(self, *, fd: int, maintenance_id: str, state_root_id: str, repo_key: str) -> None:
        self._fd = fd
        self._maintenance_id = maintenance_id
        self._state_root_id = state_root_id
        self._repo_key = repo_key

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
                "trigger": "pre_run",
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
                "trigger": "pre_run",
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
                "worktree": {"confirmed_absent": result.worktree_confirmed_absent},
                "checkpoint_ref": {"ref_name": ref_name, "confirmed_absent": result.checkpoint_ref_confirmed_absent},
                "has_recognized_temp_leftover": result.has_temp_leftover,
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
                "trigger": "pre_run",
                "timestamp": _timestamp(),
                "entries_total": len(entries),
                "entries_reconciled": counts[ReconciliationEntryOutcome.RECONCILED],
                "entries_skipped_terminal": counts[ReconciliationEntryOutcome.SKIPPED_TERMINAL],
                "entries_skipped_active": counts[ReconciliationEntryOutcome.SKIPPED_ACTIVE],
                "entries_refused": counts[ReconciliationEntryOutcome.REFUSED],
                "entries_failed": counts[ReconciliationEntryOutcome.FAILED],
                "entries_substrate_unavailable": counts[ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE],
                "blocked": blocked,
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


def _open_maintenance_trace(state_root, repo_dir_fd: int, repo_key: str) -> _MaintenanceTraceWriter:
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
        except (FileExistsError, LifecycleFsError) as exc:
            raise ReconciliationError(
                ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "the maintenance-trace file could not be created"
            ) from exc
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
        fd=fd, maintenance_id=maintenance_id, state_root_id=state_root.state_root_id, repo_key=repo_key
    )


def reconcile_repository(
    *,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
    repository_lock: LockHandle,
) -> ReconciliationPassResult:
    """Automatic pre-run reconciliation (ADR 0004 Amendment 1 section
    10, Amendment 2), called by `lifecycle_store.prepare_lifecycle()`
    while `repository_lock` is already held. Never raises for a
    per-entry problem (captured as an outcome in the returned result);
    raises `ReconciliationError` only for a whole-pass infrastructure
    failure (an unusable `runs/`/`maintenance/` namespace, an
    unconfirmed cleanup, or a wrong locking precondition) — the caller
    treats that identically to `blocked=True`.
    """
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
        trace = _open_maintenance_trace(state_root, repo_dir_fd, identity.repo_key)
        maintenance_id = trace.maintenance_id
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
