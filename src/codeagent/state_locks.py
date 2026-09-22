"""Generic verified nonblocking lock primitive and the repository-lock
wrapper, for Milestone 3 Slice 3A-1
(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1, section 11 `LockScope` / section 6).

Scope boundary: this module implements only the generic primitive and
`acquire_repository_lock`. `LockKind.LIFECYCLE` is fully defined so a
later slice's lifecycle-lock wrapper needs no breaking change to this
capability model, but nothing here ever constructs a `LockScope` of
that kind, and no `runs/<lifecycle-id>/` directory is created by this
module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, unique
from typing import Any

from ._lifecycle_fs import (
    LifecycleFsError,
    LifecycleFsFailure,
    _assert_cloexec,
    _cloexec_flag,
    _nofollow_flag,
    _validate_path_component,
    close_confirmed,
    validate_hex32,
)

# Lock files are strictly private, unlike a managed directory: no
# group/other bit of any kind, not even read/execute.
_UNSAFE_LOCK_FILE_MODE_MASK = 0o077

# Populated lazily by `_fcntl_module()` at first operation-time use —
# never imported at module load time, so a platform without `fcntl`
# (or a capability test that hides it) doesn't crash merely importing
# this module. Left as a module attribute (rather than a closure-local
# cache) so it stays independently patchable exactly as a real,
# eagerly-imported module would be.
fcntl: Any | None = None


def _fcntl_module() -> Any:
    """Lazily import and cache the platform's `fcntl` module, failing
    closed with `LockFailure.SUBSTRATE_UNAVAILABLE` if it is
    unavailable rather than letting a bare `ImportError` escape from an
    operation that needs it."""
    global fcntl
    if fcntl is None:
        try:
            import fcntl as _fcntl_mod
        except ImportError:
            raise LockError(
                LockFailure.SUBSTRATE_UNAVAILABLE,
                "the platform does not support the required fcntl capability",
            ) from None
        fcntl = _fcntl_mod
    return fcntl


def _flock_exclusive_nonblocking(fd: int) -> None:
    """Acquire `fd` via `flock(LOCK_EX|LOCK_NB)`. `BlockingIOError`
    means the lock is busy (another live holder); anything else —
    including a missing `fcntl` capability entirely, or a missing
    `LOCK_EX`/`LOCK_NB` flag — is `SUBSTRATE_UNAVAILABLE`, never a bare
    `ImportError`/`AttributeError`."""
    fcntl_mod = _fcntl_module()
    try:
        fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
    except BlockingIOError:
        raise LockError(LockFailure.BUSY, "the lock is held by another process") from None
    except (OSError, AttributeError):
        raise LockError(LockFailure.SUBSTRATE_UNAVAILABLE, "the lock could not be acquired") from None


def _flock_unlock_best_effort(fd: int) -> bool:
    """Best-effort `flock(LOCK_UN)`. Returns `True` if it failed to
    confirm — including a missing fcntl capability entirely — and
    never raises; existing call sites fold the boolean into their own
    dominant-failure aggregation."""
    try:
        fcntl_mod = _fcntl_module()
        fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
    except (LockError, OSError, AttributeError):
        return True
    return False


@unique
class LockKind(str, Enum):
    REPOSITORY = "repository"
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True)
class LockScope:
    """Exactly what a lock protects. `repo_key` (and, when present,
    `lifecycle_id`) are validated as exactly 32 lowercase hexadecimal
    characters at construction time — before any path component is
    derived from them."""

    kind: LockKind
    repo_key: str
    lifecycle_id: str | None = None

    def __post_init__(self) -> None:
        validate_hex32(self.repo_key, field_name="repo_key")
        if self.kind is LockKind.LIFECYCLE:
            if self.lifecycle_id is None:
                raise LifecycleFsError(
                    LifecycleFsFailure.INVALID_COMPONENT,
                    "a lifecycle-scoped lock requires a lifecycle_id",
                )
            validate_hex32(self.lifecycle_id, field_name="lifecycle_id")
        elif self.lifecycle_id is not None:
            raise LifecycleFsError(
                LifecycleFsFailure.INVALID_COMPONENT,
                "a repository-scoped lock must not carry a lifecycle_id",
            )


@unique
class LockFailure(str, Enum):
    BUSY = "busy"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    RELEASE_UNCONFIRMED = "release_unconfirmed"
    NOT_HELD = "not_held"


class LockError(Exception):
    """A lock operation failed or was refused. `reason` distinguishes
    `BUSY` (another live holder) from `SUBSTRATE_UNAVAILABLE` (anything
    else) — the two are never conflated."""

    def __init__(self, reason: LockFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class LockHandle:
    """An acquired, inode-verified advisory lock. Holds a strong
    reference to the descriptor for the lock's lifetime; the descriptor
    is non-inheritable. `diagnostic_path` is diagnostic only, never used
    as filesystem authority after acquisition. A context manager:
    `__exit__` always attempts `release()`; an unconfirmed release
    dominates and is chained from an in-flight body exception rather
    than silently discarded."""

    def __init__(self, *, fd: int, scope: LockScope, diagnostic_path: str) -> None:
        self._fd = fd
        self.scope = scope
        self.diagnostic_path = diagnostic_path
        self._held = True

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.release()
        except LockError as release_exc:
            if exc_value is not None:
                raise release_exc from exc_value
            raise
        return False

    @property
    def is_held(self) -> bool:
        return self._held

    def release(self) -> None:
        """Release the lock. Both the `flock` unlock and the descriptor
        close are always attempted — never short-circuited by the
        other's outcome — and any failure among them is reported,
        combined, as the primary error (deliberately stricter than an
        ordinary "never mask an in-flight exception" convention, since
        a stuck lock can block every future run against the
        repository)."""
        if not self._held:
            return
        self._held = False

        unlock_failed = _flock_unlock_best_effort(self._fd)

        close_failed = False
        try:
            os.close(self._fd)
        except OSError:
            close_failed = True

        if unlock_failed and close_failed:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "a lock could not be confirmed unlocked, and its descriptor could not be confirmed closed",
            )
        if unlock_failed:
            raise LockError(LockFailure.RELEASE_UNCONFIRMED, "a lock could not be confirmed released")
        if close_failed:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "a lock's descriptor could not be confirmed closed after release",
            )


def _validate_lock_file_stat(st: os.stat_result) -> None:
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        raise LockError(LockFailure.SUBSTRATE_UNAVAILABLE, "a lock file is not a regular file")
    if st.st_uid != os.getuid():
        raise LockError(LockFailure.SUBSTRATE_UNAVAILABLE, "a lock file is not owned by the current user")
    if st.st_mode & _UNSAFE_LOCK_FILE_MODE_MASK:
        raise LockError(LockFailure.SUBSTRATE_UNAVAILABLE, "a lock file has unsafe permissions")


def _open_lock_file_at(parent_fd: int, basename: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | _nofollow_flag() | _cloexec_flag()
    try:
        fd = os.open(basename, flags, 0o600, dir_fd=parent_fd)
    except OSError:
        raise LockError(
            LockFailure.SUBSTRATE_UNAVAILABLE,
            "a lock file could not be opened",
        ) from None
    try:
        _assert_cloexec(fd)
    except LifecycleFsError as cloexec_exc:
        close_failed = False
        try:
            os.close(fd)
        except OSError:
            close_failed = True
        if close_failed:
            raise LockError(
                LockFailure.SUBSTRATE_UNAVAILABLE,
                "a lock file's descriptor could not be made non-inheritable, "
                "and could not be confirmed closed",
            ) from cloexec_exc
        raise LockError(
            LockFailure.SUBSTRATE_UNAVAILABLE, "a lock file's descriptor could not be made non-inheritable"
        ) from cloexec_exc
    return fd


def acquire_lock_nonblocking_at(
    parent_fd: int,
    basename: str,
    *,
    scope: LockScope,
    diagnostic_path: str,
) -> LockHandle:
    """Acquire a nonblocking advisory lock on `basename` beneath
    `parent_fd`. Never `O_TRUNC`, never unlinks/renames/replaces the
    lock file. After `flock`, verifies via `fstat`/`lstat` that the
    locked descriptor and the current pathname identify the same
    regular inode, that it is owned by the current user, and that its
    permissions are private (no group/other bit at all) — otherwise a
    different process could lock a different, unsafe, or substituted
    inode at the same pathname undetected.

    If the lock itself is acquired but the caller-owned `parent_fd`
    cleanup later fails, the caller (not this function) is responsible
    for releasing the lock before raising — see
    `acquire_repository_lock` for that composition.
    """
    try:
        _validate_path_component(basename)
    except LifecycleFsError as exc:
        raise LockError(LockFailure.SUBSTRATE_UNAVAILABLE, "a lock file name is not a valid path component") from exc

    fd = _open_lock_file_at(parent_fd, basename)
    try:
        _flock_exclusive_nonblocking(fd)

        try:
            fstat_result = os.fstat(fd)
            lstat_result = os.lstat(basename, dir_fd=parent_fd)
        except OSError:
            raise LockError(
                LockFailure.SUBSTRATE_UNAVAILABLE, "the lock file could not be verified"
            ) from None

        same_inode = (
            fstat_result.st_dev == lstat_result.st_dev and fstat_result.st_ino == lstat_result.st_ino
        )
        if not same_inode:
            raise LockError(
                LockFailure.SUBSTRATE_UNAVAILABLE,
                "the lock file's identity could not be confirmed",
            )
        _validate_lock_file_stat(fstat_result)
        return LockHandle(fd=fd, scope=scope, diagnostic_path=diagnostic_path)
    except BaseException as acquisition_exc:
        # Both cleanup steps are always attempted, regardless of the
        # other's outcome. If either is unconfirmed, that is reported
        # as the primary error, chained from the original acquisition
        # failure (BUSY/SUBSTRATE_UNAVAILABLE) — never silently
        # swallowed by a bare `except OSError: pass`.
        unlock_failed = _flock_unlock_best_effort(fd)
        close_failed = False
        try:
            os.close(fd)
        except OSError:
            close_failed = True
        if unlock_failed and close_failed:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "a partially acquired lock could not be confirmed unlocked, "
                "and its descriptor could not be confirmed closed",
            ) from acquisition_exc
        if unlock_failed:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "a partially acquired lock could not be confirmed unlocked",
            ) from acquisition_exc
        if close_failed:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "a partially acquired lock's descriptor could not be confirmed closed",
            ) from acquisition_exc
        raise


def acquire_repository_lock(state_root, repo_key: str) -> LockHandle:
    """Acquire the repository lock `repo-locks/<repo_key>.lock` beneath
    `state_root`. If the lock itself is successfully acquired but
    cleanup of the short-lived `repo-locks/` parent-directory
    descriptor then fails, the just-acquired lock is released before
    raising; if both the parent-fd cleanup and the lock release fail,
    both are reported together in one sanitized cleanup error, chained
    from the parent-fd cleanup failure (the earliest sanitized failure
    in this composition).
    """
    validate_hex32(repo_key, field_name="repo_key")
    scope = LockScope(kind=LockKind.REPOSITORY, repo_key=repo_key)
    basename = f"{repo_key}.lock"

    parent_fd = state_root.open_repo_locks_dir()
    handle: LockHandle | None = None
    try:
        handle = acquire_lock_nonblocking_at(
            parent_fd,
            basename,
            scope=scope,
            diagnostic_path=f"<state-root>/repo-locks/{basename}",
        )
    except LockError as acquisition_exc:
        # If cleaning up the parent-fd itself fails here too, the
        # original acquisition failure (e.g. BUSY) is preserved as the
        # chained cause rather than being discarded.
        _dominant_parent_cleanup(parent_fd, primary=acquisition_exc)
        raise

    try:
        _dominant_parent_cleanup(parent_fd, primary=None)
    except LockError as parent_cleanup_error:
        try:
            handle.release()
        except LockError as release_error:
            raise LockError(
                LockFailure.RELEASE_UNCONFIRMED,
                "the repository lock's parent directory could not be confirmed closed, "
                "and releasing the acquired lock also failed",
            ) from parent_cleanup_error
        raise
    return handle


def acquire_lifecycle_lock(
    run_dir_fd: int, *, repo_key: str, lifecycle_id: str, diagnostic_path: str
) -> LockHandle:
    """Acquire the lifecycle lock `lifecycle.lock` beneath the caller's
    own already-open, long-lived `run_dir_fd` — the run directory
    (`runs/<lifecycle-id>/`, Milestone 3 Slice 3A-2) that the caller
    created and continues to own for the lifetime of the run. Unlike
    `acquire_repository_lock`, this wrapper opens no short-lived
    parent-directory descriptor of its own to clean up: `run_dir_fd` is
    owned and released by the caller (the lifecycle lease), not by this
    function, per ADR 0004 Amendment 1 section 12's 3A-1/3A-2 lock
    ordering boundary.
    """
    validate_hex32(repo_key, field_name="repo_key")
    validate_hex32(lifecycle_id, field_name="lifecycle_id")
    scope = LockScope(kind=LockKind.LIFECYCLE, repo_key=repo_key, lifecycle_id=lifecycle_id)
    return acquire_lock_nonblocking_at(
        run_dir_fd,
        "lifecycle.lock",
        scope=scope,
        diagnostic_path=diagnostic_path,
    )


def _dominant_parent_cleanup(parent_fd: int, *, primary: LockError | None) -> None:
    try:
        close_confirmed([parent_fd])
    except LifecycleFsError:
        if primary is not None:
            raise LockError(
                LockFailure.SUBSTRATE_UNAVAILABLE,
                "the repository lock's parent directory could not be confirmed closed",
            ) from primary
        raise LockError(
            LockFailure.SUBSTRATE_UNAVAILABLE,
            "the repository lock's parent directory could not be confirmed closed",
        ) from None
