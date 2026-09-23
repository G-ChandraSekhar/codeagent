"""Milestone 3 Slice 3B-1: initial-shape automatic reconciliation.

(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1 section 10, Amendment 2.)

Recognizes and reconciles only entries whose durable state is
`PREPARING` or `RECONCILING` with the complete initial absent
attribution shape (both containers `{intent: absent, id: null}`,
worktree `{intent: absent, expected_head: null}`, `checkpoint_ref`
exactly `ABSENT_TRANSITION`, `failure: null`). After freshly confirming
every recomputed external resource is absent (a real, unfiltered
`docker ps -a` listing, a real `git worktree list --porcelain` listing,
and a real `CheckpointRef.observe()`), it writes
`RECONCILING -> RECONCILED`. It never removes or mutates a Docker
container, Git worktree, or checkpoint ref, and it never writes
`LifecycleState.RECONCILIATION_FAILED` (reserved for a later slice that
has a real external mutation to give up on).

Called by `lifecycle_store.prepare_lifecycle()` while the repository
lock is already held, after `load_or_create_repo_json()` and before a
new `lifecycle_id` is minted or a new run directory is created. Every
filesystem, Docker, worktree, and checkpoint-ref target derives
exclusively from the trusted `state_root`/`identity`/`context`, a
validated directory-name lifecycle id, and fixed recomputed names.
Projection field values are compared, never used as mutation targets.

Not implemented here (later Milestone 3 work): abandonment, the
explicit `codeagent reconcile`/`--abandon` CLI and its `explicit`
maintenance-trigger, and any container/worktree/checkpoint-ref
*removal*.
"""

from __future__ import annotations

import os
import re
import secrets
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, unique

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
from .checkpoint_session import ABSENT_TRANSITION
from .lifecycle_store import (
    LIFECYCLE_JSON_FILENAME,
    RUN_ID_MAX_ENCODED_BYTES,
    RUNS_DIRNAME,
    ContainerIntent,
    LifecycleProjection,
    LifecycleState,
    LifecycleStoreError,
    LifecycleStoreFailure,
    WorktreeIntent,
    _publish_projection_state,
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
# Mirrors codeagent._git_safety's own _KILL_CONFIRM_GRACE_SECONDS: not
# additional time to make progress, only enough to let an already-
# issued SIGKILL actually be reaped.
_KILL_CONFIRM_GRACE_SECONDS = 2.0
_BOUNDED_READ_CHUNK = 65_536


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


def _is_absent_shape(projection: LifecycleProjection) -> bool:
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


class _BoundedReadFailure(Exception):
    """Internal-only signal from `_drain_bounded` to its callers: a
    monitoring-setup failure, a timeout, or an output-size overflow.
    The caller always kills and confirms termination of the child
    before converting this into its own categorical error — mirrors
    `codeagent._git_safety`'s own `_BoundedFailure`/`_drain_stdout`
    shape, generalized here for a non-Git subprocess (Docker)."""


def _drain_bounded(process: subprocess.Popen, *, deadline: float, limit: int) -> bytes:
    """Read at most `limit + 1` bytes of `process.stdout`, non-blocking,
    under `deadline`. Raises `_BoundedReadFailure` on a monitoring
    failure, a timeout, or an oversize read — the caller discards the
    buffer and kills the child in every case; a truncated read is never
    silently trusted as the complete listing (a false "absent"
    conclusion from a partial view would be worse than refusing)."""
    try:
        selector = selectors.DefaultSelector()
    except Exception as exc:  # noqa: BLE001 - any setup failure is categorical
        raise _BoundedReadFailure("subprocess output could not be monitored") from exc

    buf = bytearray()
    try:
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
        except Exception as exc:  # noqa: BLE001 - any setup failure is categorical
            raise _BoundedReadFailure("subprocess output could not be monitored") from exc

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoundedReadFailure("subprocess did not finish within its time limit")
            if not selector.select(timeout=remaining):
                continue
            try:
                chunk = os.read(process.stdout.fileno(), _BOUNDED_READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise _BoundedReadFailure("subprocess output could not be read") from exc
            if not chunk:
                return bytes(buf)
            buf += chunk
            if len(buf) > limit:
                raise _BoundedReadFailure("subprocess produced more output than its fixed safety bound allows")
    finally:
        try:
            selector.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.stdout.close()
        except Exception:  # noqa: BLE001
            pass


def _kill_and_confirm(process: subprocess.Popen, *, deadline: float) -> None:
    """The single abort path for a bounded-read failure: kill the
    child and confirm it was reaped within a bounded grace window.
    Raises `_BoundedReadFailure` if termination cannot be confirmed —
    never returns having silently left the child running."""
    try:
        process.kill()
    except Exception:  # noqa: BLE001 - still attempt to confirm exit below
        pass
    confirm_deadline = max(deadline, time.monotonic()) + _KILL_CONFIRM_GRACE_SECONDS
    try:
        process.wait(timeout=max(0.0, confirm_deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise _BoundedReadFailure("subprocess could not be confirmed terminated") from exc


def _docker_ps_all_names() -> set[str]:
    """One bounded, unfiltered, timeout-controlled, no-shell
    `docker ps -a` listing of every container name (running or
    stopped) — never a name-filtered or label-filtered query (I2).
    Output is capped at `_DOCKER_OUTPUT_MAX_BYTES`; a timeout or
    overflow kills the child and confirms it was reaped before this
    function raises."""
    argv = ["docker", "ps", "-a", "--no-trunc", "--format", "{{.Names}}"]
    deadline = time.monotonic() + _DOCKER_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        raise _DockerListingError("the docker executable could not be launched") from exc

    try:
        stdout = _drain_bounded(process, deadline=deadline, limit=_DOCKER_OUTPUT_MAX_BYTES)
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (_BoundedReadFailure, subprocess.TimeoutExpired) as exc:
        try:
            _kill_and_confirm(process, deadline=deadline)
        except _BoundedReadFailure as cleanup_exc:
            raise _DockerListingError("a docker listing child could not be confirmed terminated") from cleanup_exc
        raise _DockerListingError("docker listing failed, timed out, or exceeded its output bound") from exc

    if process.returncode != 0:
        raise _DockerListingError("docker listing exited with a nonzero status")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DockerListingError("docker listing produced invalid UTF-8 output") from exc
    return {name for name in text.splitlines() if name}


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


def _reconcile_locked_entry(
    *,
    run_dir_fd: int,
    lifecycle_id: str,
    state_root,
    identity: RepositoryIdentity,
    context: TrustedRepositoryContext,
) -> ReconciliationEntryResult:
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

    if projection.state not in (LifecycleState.PREPARING, LifecycleState.RECONCILING) or not _is_absent_shape(
        projection
    ):
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "entry is no longer the recognized nonterminal absent shape",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    try:
        live_container_names = _docker_ps_all_names()
    except _DockerListingError:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
            "container listing failed",
            run_id=projection.run_id,
            attempt_number=projection.reconciliation.attempts_total,
        )
    if baseline_name in live_container_names or verification_name in live_container_names:
        return ReconciliationEntryResult(
            lifecycle_id,
            ReconciliationEntryOutcome.REFUSED,
            "a container with a recomputed owned name is present",
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

    # Everything confirmed absent: write RECONCILING (fresh transitions
    # only increment attempts_total; a resumed RECONCILING carries its
    # already-incremented value forward unchanged), then RECONCILED.
    attempts_total = projection.reconciliation.attempts_total
    if projection.state is LifecycleState.PREPARING:
        attempts_total += 1
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
        if _is_absent_shape(peek):
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

    if peek.state not in (LifecycleState.PREPARING, LifecycleState.RECONCILING) or not _is_absent_shape(peek):
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
                    "baseline": {"id": None, "confirmed_absent": result.baseline_confirmed_absent},
                    "verification": {"id": None, "confirmed_absent": result.verification_confirmed_absent},
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
