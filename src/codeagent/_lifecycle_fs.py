"""Shared filesystem-safety foundation for Milestone 3 Slice 3A-1
(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1, Accepted).

Scope boundary: this module owns the primitives Amendment 1 specifies
as shared across `state_root.py`, `state_locks.py`, and
`repo_identity.py` — canonical directory identity (including the
Darwin `F_GETPATH` case-canonicalization fix), directional containment,
`dir_fd`-relative managed-directory-chain traversal, private
exclusive-create file primitives, descriptor-cleanup discipline (never
a short-circuiting `any()`, cleanup failures dominate and chain), and
canonical/strict JSON encode-decode with legitimate-filesystem-surrogate
handling. It does not decide lifecycle outcomes and never constructs an
`errors.OperationalError` — Slice 3A-1 adds no new `errors.ErrorCode`/
`ErrorDomain` (ADR 0004 Amendment 1, "Milestone boundary"); every
failure here raises `LifecycleFsError` carrying a categorical `reason`.

Not implemented here or anywhere in this slice: `lifecycle.json`,
`runs/<lifecycle-id>/` creation, a lifecycle-lock wrapper, container/
worktree/checkpoint-ref attribution, reconciliation, abandonment, the
maintenance trace, or any controller/CLI wiring.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import stat
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Any

# Fixed bounds (ADR 0004 Amendment 1 section 4).
STATE_ROOT_JSON_MAX_BYTES = 4096
REPO_JSON_MAX_BYTES = 32768
STORED_PATH_MAX_FS_BYTES = 4096
SANITIZED_DETAIL_MAX_BYTES = 512

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

# Legitimate os.fsdecode surrogateescape range for raw bytes 0x80-0xFF.
_FS_SURROGATE_LOW = 0xDC80
_FS_SURROGATE_HIGH = 0xDCFF


@unique
class LifecycleFsFailure(str, Enum):
    """Categorical reason a lifecycle-filesystem primitive failed or
    refused. Callers branch on this; `LifecycleFsError.message` is for
    humans and must never be parsed, and never contains a raw path."""

    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    INVALID_COMPONENT = "invalid_component"
    SYMLINK_REFUSED = "symlink_refused"
    UNSAFE_PERMISSIONS = "unsafe_permissions"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    OVERSIZED = "oversized"
    INVALID_UTF8 = "invalid_utf8"
    DUPLICATE_KEY = "duplicate_key"
    ILLEGITIMATE_SURROGATE = "illegitimate_surrogate"
    JSON_SYNTAX_INVALID = "json_syntax_invalid"
    SCHEMA_INVALID = "schema_invalid"
    FSYNC_FAILED = "fsync_failed"
    IO_FAILED = "io_failed"
    ENVIRONMENT_INVALID = "environment_invalid"


class LifecycleFsError(Exception):
    """A lifecycle-filesystem primitive failed or was refused.

    `reason` is the stable, matchable identifier. `message` is
    sanitized, fixed categorical text only — never a raw path, raw
    OSError text, or a formatted traceback. Raw `OSError`/`RuntimeError`/
    `UnicodeError` are always translated via `from None`, never
    `from exc` (their own attributes, e.g. `.filename`, can leak a
    path). Chaining (`from`) is used only between this module's own
    already-sanitized exceptions, per the cleanup-dominates-and-chains
    discipline.
    """

    def __init__(self, reason: LifecycleFsFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _required_platform_flag(name: str) -> int:
    """Return the numeric value of a required POSIX flag attribute
    from the `os` module, or fail closed with `SUBSTRATE_UNAVAILABLE`
    if the running platform doesn't define it — never silently
    substituting `0`, which would silently strip the safety property
    the flag exists to provide. Called at operation time inside each
    function that needs it, never at module import time, so a platform
    missing one optional capability doesn't crash the whole module on
    import."""
    value = getattr(os, name, None)
    if value is None:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            f"the platform does not support the required {name} capability",
        )
    return value


def _cloexec_flag() -> int:
    return _required_platform_flag("O_CLOEXEC")


def _nofollow_flag() -> int:
    return _required_platform_flag("O_NOFOLLOW")


def _directory_flag() -> int:
    return _required_platform_flag("O_DIRECTORY")


# Populated lazily by `_fcntl_module()` at first operation-time use —
# never imported at module load time, so a platform without `fcntl`
# (or a capability test that hides it) doesn't crash merely importing
# this module. Left as a module attribute (rather than a closure-local
# cache) so it stays independently patchable exactly as a real,
# eagerly-imported module would be.
fcntl: Any | None = None


def _fcntl_module() -> Any:
    """Lazily import and cache `fcntl`, failing closed with
    `SUBSTRATE_UNAVAILABLE` if it is unavailable, rather than a bare
    `ImportError` at module import time — this module must import
    cleanly on any platform, deferring the fcntl capability check to
    the operation that actually needs it."""
    global fcntl
    if fcntl is None:
        try:
            import fcntl as _fcntl_mod
        except ImportError:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "the platform does not support the required fcntl capability",
            ) from None
        fcntl = _fcntl_mod
    return fcntl


def _assert_cloexec(fd: int) -> None:
    """Reassert and verify `FD_CLOEXEC` on `fd`, independent of whether
    `O_CLOEXEC` was honored at open time. A missing `fcntl` module, or
    a missing `F_SETFD`/`F_GETFD`/`FD_CLOEXEC` attribute on it, is
    `SUBSTRATE_UNAVAILABLE`, raised here at operation time."""
    fcntl_mod = _fcntl_module()
    try:
        fcntl_mod.fcntl(fd, fcntl_mod.F_SETFD, fcntl_mod.FD_CLOEXEC)
        flags = fcntl_mod.fcntl(fd, fcntl_mod.F_GETFD)
    except (OSError, AttributeError):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the platform does not support the required descriptor non-inheritance controls",
        ) from None
    if not (flags & fcntl_mod.FD_CLOEXEC):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a descriptor could not be made non-inheritable",
        )


def close_confirmed(fds: list[int]) -> None:
    """Close every descriptor in `fds`, attempting every close even
    after an earlier one fails — never a short-circuiting
    `any(generator)`, which would stop attempting further closes as
    soon as the first one failed. Raises `LifecycleFsError` exactly
    once, after every attempt has been made, if any close failed."""
    any_failed = False
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            any_failed = True
    if any_failed:
        raise LifecycleFsError(
            LifecycleFsFailure.CLEANUP_UNCONFIRMED,
            "one or more descriptors could not be confirmed closed",
        )


def _dominant_cleanup(fds: list[int], primary: BaseException | None) -> None:
    """Attempt to close every fd in `fds`. If cleanup fails, it becomes
    the primary raised error, explicitly chained from `primary` (an
    already-sanitized exception, or `None` if there was none)."""
    if not fds:
        return
    try:
        close_confirmed(fds)
    except LifecycleFsError as cleanup_exc:
        if primary is not None:
            raise cleanup_exc from primary
        raise


def validate_hex32(value: str, *, field_name: str) -> str:
    """Validate `value` as exactly 32 lowercase hexadecimal characters
    (a `repo_key`, `lifecycle_id`, or `state_root_id`). Validated before
    any path component is derived from it."""
    if not isinstance(value, str) or not _HEX32_RE.match(value):
        raise LifecycleFsError(
            LifecycleFsFailure.INVALID_COMPONENT,
            f"{field_name} is not exactly 32 lowercase hexadecimal characters",
        )
    return value


def _validate_path_component(component: str) -> str:
    if (
        not isinstance(component, str)
        or component in ("", ".", "..")
        or "/" in component
        or "\x00" in component
    ):
        raise LifecycleFsError(
            LifecycleFsFailure.INVALID_COMPONENT,
            "a managed directory component is not a valid single path segment",
        )
    return component


def _current_platform_is_darwin() -> bool:
    return platform.system() == "Darwin"


def _darwin_canonical_path(fd: int) -> str:
    """Canonicalize `fd`'s path via `fcntl.F_GETPATH`, exactly as
    specified: `fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))` — passing
    `array.array`/`bytearray` instead of immutable `bytes` raises
    `TypeError` on the tested runtime. Requires a NUL terminator and a
    nonempty absolute result; decodes only via `os.fsdecode`. Any
    failure is `SUBSTRATE_UNAVAILABLE` with no fallback to the
    non-canonical path."""
    fcntl_mod = _fcntl_module()
    try:
        raw_buffer = fcntl_mod.fcntl(fd, fcntl_mod.F_GETPATH, bytes(1024))
    except (OSError, TypeError, AttributeError):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the platform's canonical-path resolution is unavailable or failed",
        ) from None
    nul_index = raw_buffer.find(b"\x00")
    if nul_index == -1:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "canonical-path resolution returned an unterminated result",
        )
    raw_path = raw_buffer[:nul_index]
    if not raw_path or not raw_path.startswith(b"/"):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "canonical-path resolution returned an empty or non-absolute result",
        )
    return os.fsdecode(raw_path)


def canonicalize_directory(path: str) -> tuple[int, str]:
    """Open `path` as a directory (`O_NOFOLLOW`) and compute its
    case-canonical absolute path: on Darwin via `F_GETPATH` (closing the
    real, reproduced case-insensitive-but-preserving APFS/HFS+ bug where
    `Path.resolve()` does not canonicalize case), on other platforms via
    the already-resolved path used to open it (case-sensitive
    filesystems already have `st_dev`/`st_ino` as authoritative
    identity). Returns the open, non-inheritable descriptor and the
    canonical path string; the caller owns the descriptor's lifetime.
    """
    try:
        resolved = str(Path(path).resolve())
    except (OSError, RuntimeError):
        # OSError: a permission failure or similar while walking
        # symlinks. RuntimeError: pathlib's own infinite-symlink-loop
        # detection. Either exception's own message/args can embed the
        # raw path, so it is never re-raised or chained (`from None`).
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a directory path could not be resolved",
        ) from None
    flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a directory could not be opened for canonicalization",
        ) from None
    try:
        _assert_cloexec(fd)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise LifecycleFsError(
                LifecycleFsFailure.NOT_A_DIRECTORY,
                "the target of canonicalization is not a directory",
            )
        if _current_platform_is_darwin():
            canonical = _darwin_canonical_path(fd)
        else:
            canonical = resolved
        return fd, canonical
    except BaseException as exc:
        _dominant_cleanup([fd], exc)
        raise


def is_within_or_equal(candidate: str, container: str) -> bool:
    """Directional, component-aware containment: true iff `candidate`
    is `container` or a descendant of it. Never a string prefix
    comparison (which would wrongly match `/state-root2` against
    `/state-root`). Both arguments must already be canonical absolute
    paths — this primitive does no resolution of its own."""
    candidate_parts = Path(candidate).parts
    container_parts = Path(container).parts
    if len(container_parts) > len(candidate_parts):
        return False
    return candidate_parts[: len(container_parts)] == container_parts


def _safe_lstat(name: str, *, dir_fd: int) -> os.stat_result:
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a managed directory component could not be inspected",
        ) from None


# Directories: owner match, no group/other WRITE bit. Read/execute bits
# (0750, 0740, 0705, ...) are safe and must not be rejected merely for
# being present — only a writable group/other bit actually admits
# tampering.
_UNSAFE_DIRECTORY_WRITE_MASK = 0o022


def validate_safe_owned_directory_stat(st: os.stat_result) -> None:
    """Shared permission rule for a managed directory (used by both the
    state-root leaf itself and `open_managed_directory_chain`): the
    current uid must own it, and neither the group nor other write bit
    may be set. Read/execute bits for group/other are permitted."""
    if st.st_uid != os.getuid():
        raise LifecycleFsError(
            LifecycleFsFailure.UNSAFE_PERMISSIONS,
            "a managed directory is not owned by the current user",
        )
    if st.st_mode & _UNSAFE_DIRECTORY_WRITE_MASK:
        raise LifecycleFsError(
            LifecycleFsFailure.UNSAFE_PERMISSIONS,
            "a managed directory is writable by a group or user other than its owner",
        )


def _validate_managed_directory_state(name: str, *, dir_fd: int) -> None:
    st = _safe_lstat(name, dir_fd=dir_fd)
    if stat.S_ISLNK(st.st_mode):
        raise LifecycleFsError(
            LifecycleFsFailure.SYMLINK_REFUSED,
            "a managed directory component is a symlink",
        )
    if not stat.S_ISDIR(st.st_mode):
        raise LifecycleFsError(
            LifecycleFsFailure.NOT_A_DIRECTORY,
            "a managed directory component is not a real directory",
        )
    validate_safe_owned_directory_stat(st)


def _finish_successful_chain(opened: list[int]) -> int:
    """On the success path of a chain-walk (`open_managed_directory_chain`
    or `open_existing_directory_chain_if_present`), close every
    intermediate descriptor and return the final one. If closing the
    intermediates fails, the final descriptor is also attempted
    (never leaked simply because it was about to be returned), and
    both failures are reported together — as a single sanitized
    error — if both occur."""
    to_close_now = opened[:-1]
    final_fd = opened[-1]
    if not to_close_now:
        return final_fd

    intermediate_failed = False
    try:
        close_confirmed(to_close_now)
    except LifecycleFsError:
        intermediate_failed = True
    if not intermediate_failed:
        return final_fd

    final_failed = False
    try:
        close_confirmed([final_fd])
    except LifecycleFsError:
        final_failed = True
    if final_failed:
        raise LifecycleFsError(
            LifecycleFsFailure.CLEANUP_UNCONFIRMED,
            "neither the intermediate nor the final managed directory descriptors "
            "could be confirmed closed",
        )
    raise LifecycleFsError(
        LifecycleFsFailure.CLEANUP_UNCONFIRMED,
        "an intermediate managed directory descriptor could not be confirmed closed",
    )


def open_managed_directory_chain(parent_fd: int, components: list[str]) -> int:
    """Walk `components` beneath `parent_fd`, creating each as mode
    `0700` if absent (never `os.makedirs` — no arbitrary recursion
    beyond the given components), refusing a symlink or unsafe existing
    directory at any step, and returning the final directory's
    non-inheritable descriptor. `parent_fd` is never closed here — it is
    owned by the caller (e.g. `StateRoot`'s long-lived root descriptor).
    Every intermediate descriptor this function itself opens is closed
    before returning (success) or before raising (failure); a cleanup
    failure dominates and chains from whatever failure was already
    active.
    """
    if not components:
        raise LifecycleFsError(
            LifecycleFsFailure.INVALID_COMPONENT,
            "a managed directory chain requires at least one component",
        )
    current_fd = parent_fd
    opened: list[int] = []
    try:
        for component in components:
            _validate_path_component(component)
            try:
                os.mkdir(component, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            except OSError:
                raise LifecycleFsError(
                    LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                    "a managed directory component could not be created",
                ) from None
            _validate_managed_directory_state(component, dir_fd=current_fd)
            flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
            try:
                fd = os.open(component, flags, dir_fd=current_fd)
            except OSError:
                raise LifecycleFsError(
                    LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                    "a managed directory component could not be opened",
                ) from None
            # Recorded immediately, before the CLOEXEC assertion: if
            # that assertion fails, this fd must still be in `opened`
            # so the exception handler below closes it rather than
            # leaking it.
            opened.append(fd)
            _assert_cloexec(fd)
            current_fd = fd
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise

    return _finish_successful_chain(opened)


class _DirectoryComponentNotPresent(Exception):
    """Internal-only signal from `open_existing_directory_chain_if_present`'s
    walk loop to its single handling site. Never escapes that function."""


def list_directory_entries(fd: int) -> list[str]:
    """List the names of every entry in the directory `fd` refers to,
    without closing it. Used to enforce "no other state exists yet"
    preconditions (e.g. `repo.json` creation) — never to derive a
    filesystem-mutation target."""
    try:
        with os.scandir(fd) as it:
            return [entry.name for entry in it]
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a managed directory could not be listed",
        ) from None


def open_existing_directory_chain_if_present(parent_fd: int, components: list[str]) -> int | None:
    """Like `open_managed_directory_chain`, but never creates anything:
    walks `components` beneath `parent_fd`, returning the final
    directory's non-inheritable descriptor if the complete chain
    already exists, or `None` if any component along the way is
    absent — closing every descriptor this function itself opened
    along the way in either case. A symlink or unsafe existing
    directory at any present component is still refused, exactly as
    `open_managed_directory_chain`."""
    if not components:
        raise LifecycleFsError(
            LifecycleFsFailure.INVALID_COMPONENT,
            "a managed directory chain requires at least one component",
        )
    current_fd = parent_fd
    opened: list[int] = []
    try:
        for component in components:
            _validate_path_component(component)
            try:
                st = os.lstat(component, dir_fd=current_fd)
            except FileNotFoundError:
                raise _DirectoryComponentNotPresent() from None
            except OSError:
                raise LifecycleFsError(
                    LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                    "a managed directory component could not be inspected",
                ) from None
            if stat.S_ISLNK(st.st_mode):
                raise LifecycleFsError(
                    LifecycleFsFailure.SYMLINK_REFUSED,
                    "a managed directory component is a symlink",
                )
            if not stat.S_ISDIR(st.st_mode):
                raise LifecycleFsError(
                    LifecycleFsFailure.NOT_A_DIRECTORY,
                    "a managed directory component is not a real directory",
                )
            validate_safe_owned_directory_stat(st)
            flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
            try:
                fd = os.open(component, flags, dir_fd=current_fd)
            except OSError:
                raise LifecycleFsError(
                    LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                    "a managed directory component could not be opened",
                ) from None
            # Recorded immediately, before the CLOEXEC assertion — see
            # open_managed_directory_chain's identical rationale.
            opened.append(fd)
            _assert_cloexec(fd)
            current_fd = fd
    except _DirectoryComponentNotPresent:
        if opened:
            close_confirmed(opened)
        return None
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise

    return _finish_successful_chain(opened)


def open_private_create_exclusive_at(parent_fd: int, basename: str, mode: int = 0o600) -> int:
    """Create-and-open `basename` beneath `parent_fd` with
    `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, then explicitly `fchmod` and
    `fstat`-verify the exact regular-file/current-uid/exact-mode result
    — independent of umask, which can silently narrow a bare
    `os.open(..., mode)` request. Raises `FileExistsError` unmodified
    (the caller distinguishes an ordinary creation race from any other
    failure) and `LifecycleFsError` for anything else."""
    _validate_path_component(basename)
    flags = os.O_CREAT | os.O_EXCL | _nofollow_flag() | os.O_WRONLY | _cloexec_flag()
    try:
        fd = os.open(basename, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        # Preserved unmodified: the caller's documented signal for the
        # one legitimate creation race. Never converted into, or
        # confused with, any other failure below.
        raise
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "a private file could not be created",
        ) from None
    try:
        _assert_cloexec(fd)
        try:
            os.fchmod(fd, mode)
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "a newly created private file's mode could not be set",
            ) from None
        try:
            st = os.fstat(fd)
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "a newly created private file could not be inspected",
            ) from None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o777) != mode:
            raise LifecycleFsError(
                LifecycleFsFailure.UNSAFE_PERMISSIONS,
                "a newly created private file did not have the expected exact mode",
            )
        return fd
    except BaseException as exc:
        _dominant_cleanup([fd], exc)
        raise


def write_all_eintr_safe(fd: int, data: bytes) -> None:
    """Write every byte of `data` to `fd`, retrying on `EINTR` and
    raising on zero forward progress (a write that returns `0` without
    an exception can never complete)."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(fd, view[offset:])
        except InterruptedError:
            continue
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.IO_FAILED,
                "a write to a lifecycle file failed",
            ) from None
        if written == 0:
            raise LifecycleFsError(
                LifecycleFsFailure.IO_FAILED,
                "a write to a lifecycle file made no forward progress",
            )
        offset += written


def read_all_eintr_safe(fd: int, max_bytes: int) -> bytes:
    """Read at most `max_bytes + 1` bytes from `fd` (the `+1` lets a
    caller detect and refuse an oversized file rather than silently
    truncating it), retrying on `EINTR`."""
    chunks: list[bytes] = []
    total = 0
    limit = max_bytes + 1
    while total < limit:
        try:
            chunk = os.read(fd, min(65536, limit - total))
        except InterruptedError:
            continue
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.IO_FAILED,
                "a read from a lifecycle file failed",
            ) from None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.FSYNC_FAILED,
            "a lifecycle file or directory could not be confirmed durable",
        ) from None


def validate_filesystem_surrogates(value: Any) -> None:
    """Recursively refuse any lone UTF-16 surrogate character outside
    `os.fsdecode`'s surrogateescape range (U+DC80-U+DCFF for raw bytes
    0x80-0xFF). CodeAgent's own encoder never produces any other lone
    surrogate, so its presence is corruption or hostile input."""
    if isinstance(value, str):
        for ch in value:
            code = ord(ch)
            if 0xD800 <= code <= 0xDFFF and not (_FS_SURROGATE_LOW <= code <= _FS_SURROGATE_HIGH):
                raise LifecycleFsError(
                    LifecycleFsFailure.ILLEGITIMATE_SURROGATE,
                    "persisted content contains an illegitimate lone surrogate character",
                )
    elif isinstance(value, dict):
        for key, sub in value.items():
            validate_filesystem_surrogates(key)
            validate_filesystem_surrogates(sub)
    elif isinstance(value, list):
        for sub in value:
            validate_filesystem_surrogates(sub)


def canonical_json_dumps(payload: Any) -> bytes:
    """Canonical, strictly-UTF-8-safe JSON serialization:
    `sort_keys=True, separators=(",", ":"), ensure_ascii=True`, then a
    strict UTF-8 encode (always succeeds, because `ensure_ascii=True`
    escapes every non-ASCII code point, including a legitimate lone
    surrogate, as a pure-ASCII `\\uXXXX` sequence)."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def canonical_json_loads_strict(data: bytes, *, max_bytes: int) -> Any:
    """Strictly decode and parse persisted canonical JSON: size bound,
    strict UTF-8 decode, duplicate-key rejection, and illegitimate-
    surrogate rejection. Raises `LifecycleFsError` with a precise
    reason — `OVERSIZED`, `INVALID_UTF8`, `DUPLICATE_KEY`,
    `JSON_SYNTAX_INVALID`, or `ILLEGITIMATE_SURROGATE` — never a generic
    catch-all."""
    if len(data) > max_bytes:
        raise LifecycleFsError(LifecycleFsFailure.OVERSIZED, "persisted content exceeds its fixed size bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LifecycleFsError(
            LifecycleFsFailure.INVALID_UTF8, "persisted content is not valid UTF-8"
        ) from None
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError:
        raise LifecycleFsError(
            LifecycleFsFailure.DUPLICATE_KEY, "persisted content contains a duplicate JSON key"
        ) from None
    except ValueError:
        raise LifecycleFsError(
            LifecycleFsFailure.JSON_SYNTAX_INVALID, "persisted content is not syntactically valid JSON"
        ) from None
    validate_filesystem_surrogates(obj)
    return obj


@unique
class StateRootOrigin(str, Enum):
    EXPLICIT = "explicit"
    MACOS_DEFAULT = "macos_default"
    XDG_DEFAULT = "xdg_default"
    LINUX_HOME_DEFAULT = "linux_home_default"


@dataclass(frozen=True)
class StateRootLocation:
    path: str
    origin: StateRootOrigin
    conventional_parent_creation_allowed: bool


def resolve_state_root_path(env: dict[str, str] | None = None) -> StateRootLocation:
    """Resolve the state-root location per ADR 0004 Amendment 1 section
    5: explicit `CODEAGENT_STATE_DIR` (absolute-only, no parent
    creation), else the platform default (macOS: `~/Library/Application
    Support/CodeAgent`; Linux: `$XDG_STATE_HOME/codeagent` when
    `XDG_STATE_HOME` is absolute, else `~/.local/state/codeagent`) with
    precisely bounded ancestor creation permitted. `env` defaults to
    `os.environ` (injectable for tests)."""
    environ = os.environ if env is None else env

    explicit = environ.get("CODEAGENT_STATE_DIR")
    if explicit is not None:
        if not os.path.isabs(explicit):
            raise LifecycleFsError(
                LifecycleFsFailure.ENVIRONMENT_INVALID,
                "CODEAGENT_STATE_DIR must be an absolute path",
            )
        parent = os.path.dirname(explicit.rstrip("/") or "/")
        if not os.path.isdir(parent):
            raise LifecycleFsError(
                LifecycleFsFailure.ENVIRONMENT_INVALID,
                "CODEAGENT_STATE_DIR's parent directory does not exist",
            )
        return StateRootLocation(
            path=explicit, origin=StateRootOrigin.EXPLICIT, conventional_parent_creation_allowed=False
        )

    home = environ.get("HOME")
    if not home or not os.path.isabs(home):
        raise LifecycleFsError(
            LifecycleFsFailure.ENVIRONMENT_INVALID,
            "HOME is not set to an absolute path",
        )

    if _current_platform_is_darwin():
        path = os.path.join(home, "Library", "Application Support", "CodeAgent")
        return StateRootLocation(
            path=path, origin=StateRootOrigin.MACOS_DEFAULT, conventional_parent_creation_allowed=True
        )

    xdg = environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(xdg):
        path = os.path.join(xdg, "codeagent")
        return StateRootLocation(
            path=path, origin=StateRootOrigin.XDG_DEFAULT, conventional_parent_creation_allowed=True
        )

    path = os.path.join(home, ".local", "state", "codeagent")
    return StateRootLocation(
        path=path, origin=StateRootOrigin.LINUX_HOME_DEFAULT, conventional_parent_creation_allowed=True
    )


def ensure_bounded_ancestor(base: str, components: list[str]) -> None:
    """Create at most the exact named `components` beneath `base`, in
    order — never a recursive `os.makedirs` over an arbitrary ancestor
    chain. Each step tolerates the directory already existing."""
    current = base
    for component in components:
        current = os.path.join(current, component)
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "a conventional state-root ancestor directory could not be created",
            ) from None
