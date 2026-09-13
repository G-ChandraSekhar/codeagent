"""Milestone 1: a narrow, bounded worktree file reader — the read half
of the read-plan-approve-patch-verify-report vertical slice the
implementation guide requires ("Using a deterministic fake model and a
real fixture repository, perform a full read-plan-approve-patch-verify-
report flow").

NOT the Milestone 2 repository-read toolkit: this supports exactly one
operation — read one UTF-8 text file, up to a small fixed byte limit —
and nothing else. No directory listing, no pagination, no binary
content, no search. A future RepositoryReader implementation with that
broader surface is separate, later work; this module should not be
extended toward it.

Path handling mirrors codeagent.patch.GitPatchApplier's discipline
(deliberately duplicated rather than shared, since sharing would widen
patch.py's private surface for a few dozen lines): reject absolute
paths and '..' before doing anything else, resolve and verify
containment inside the worktree, and reject a symlink anywhere along
the path — all before any file is opened. Every failure is reported as
a structured, sanitized ReadResult; no filesystem exception
(FileNotFoundError, PermissionError, UnicodeDecodeError, OSError) ever
propagates out of read_file.

Known limitation, not fixed by this module: `_validate_path` and the
actual open in `read_file` are two separate filesystem operations, so a
concurrent replacement of a path component (e.g. swapping a regular
file for a symlink after validation but before the open) is a real
TOCTOU race this module does not close. Robust, race-free handling
needs descriptor-relative resolution (openat-style, opening each path
component with O_NOFOLLOW relative to an already-open directory
descriptor) rather than path-string validation followed by a plain
open — that is real design work belonging to Milestone 2's repository-
read toolkit, not a narrow addition here. This module's validation is
a real, meaningful check against the ordinary cases (a hostile or
buggy plan-proposed path, a stray symlink already present in the
worktree) — it is not a claim of TOCTOU-safety against an adversary who
can mutate the worktree's filesystem concurrently with the read.
"""

from __future__ import annotations

from pathlib import Path

from codeagent.controller import ReadResult
from codeagent.errors import ErrorCode, OperationalError

# A small fixed cap for this narrow slice's one supported operation —
# not a general size policy (compare codeagent.executor's 64 KiB
# bounded *output* collection, a different concern: streamed process
# output vs. a single file read).
MAX_READ_BYTES = 64 * 1024

_MAX_MESSAGE_PATH_LENGTH = 200


def _safe_path_fragment(value: str) -> str:
    """Render a caller-supplied path value safely for inclusion in a
    persisted message — same discipline as patch.py's helper of the
    same name."""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in value)
    if len(cleaned) > _MAX_MESSAGE_PATH_LENGTH:
        cleaned = cleaned[:_MAX_MESSAGE_PATH_LENGTH] + "...(truncated)"
    return cleaned


class _ValidationFailure(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_path(worktree: Path, relative_path: str) -> Path:
    if not relative_path:
        raise _ValidationFailure(ErrorCode.TOOL_INPUT_INVALID, "relative_path must be nonempty")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise _ValidationFailure(
            ErrorCode.TOOL_INPUT_INVALID,
            f"relative_path must not be absolute: {_safe_path_fragment(relative_path)!r}",
        )
    if any(part == ".." for part in candidate.parts):
        raise _ValidationFailure(
            ErrorCode.TOOL_INPUT_INVALID,
            f"relative_path must not contain '..': {_safe_path_fragment(relative_path)!r}",
        )

    resolved_worktree = worktree.resolve()
    target = (resolved_worktree / candidate).resolve()
    if not (target == resolved_worktree or resolved_worktree in target.parents):
        raise _ValidationFailure(
            ErrorCode.TOOL_INPUT_INVALID,
            f"relative_path resolves outside the worktree: {_safe_path_fragment(relative_path)!r}",
        )

    # Reject if any path component (including the final one) is a
    # symlink — a symlink anywhere along the path is untrusted
    # regardless of where it ultimately points, not merely rejected
    # when it happens to escape the worktree (that case is already
    # caught above by the fully-resolved containment check).
    probe = resolved_worktree
    for part in candidate.parts:
        probe = probe / part
        if probe.is_symlink():
            raise _ValidationFailure(
                ErrorCode.TOOL_INPUT_INVALID,
                f"path escapes through a symlink: {_safe_path_fragment(relative_path)!r}",
            )
        if probe == target:
            break

    if not target.is_file():
        raise _ValidationFailure(
            ErrorCode.TOOL_INPUT_INVALID,
            f"target file does not exist: {_safe_path_fragment(relative_path)!r}",
        )

    return target


class WorktreeFileReader:
    """Reads exactly one UTF-8 text file from inside a fixed worktree
    root, up to MAX_READ_BYTES. Constructed once per worktree, like
    codeagent.patch.GitPatchApplier and codeagent.executor.DockerVerifier."""

    def __init__(self, worktree_path: Path) -> None:
        resolved = Path(worktree_path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"worktree_path must be an existing directory, got {resolved!r}")
        self._worktree_path = resolved
        self._next_error_id_seq = 0

    def _next_error_id(self) -> str:
        self._next_error_id_seq += 1
        return f"read-err-{self._next_error_id_seq}"

    def _failure(self, code: ErrorCode, message: str) -> ReadResult:
        return ReadResult(
            success=False,
            content=None,
            byte_count=0,
            error=OperationalError(code=code, error_id=self._next_error_id(), message=message),
        )

    def read_file(self, relative_path: str) -> ReadResult:
        try:
            target = _validate_path(self._worktree_path, relative_path)
        except _ValidationFailure as exc:
            return self._failure(exc.code, exc.message)

        # Enforce the limit on the actual read, not on a preceding
        # stat(): a separate stat-then-read_bytes(complete file) would
        # both duplicate the size check as two filesystem round trips
        # and, worse, still read and hold an arbitrarily large file in
        # memory if the file grew between the stat and the read. Read
        # at most one byte past the limit instead — enough to detect
        # "too large" without ever materializing more than
        # MAX_READ_BYTES + 1 bytes.
        try:
            with target.open("rb") as f:
                raw = f.read(MAX_READ_BYTES + 1)
        except OSError:
            return self._failure(
                ErrorCode.TOOL_EXECUTION_FAILED,
                f"target file could not be read: {_safe_path_fragment(relative_path)!r}",
            )

        if len(raw) > MAX_READ_BYTES:
            return self._failure(
                ErrorCode.TOOL_EXECUTION_FAILED,
                f"target file exceeds the {MAX_READ_BYTES}-byte read limit: "
                f"{_safe_path_fragment(relative_path)!r}",
            )

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._failure(
                ErrorCode.TOOL_EXECUTION_FAILED,
                f"target file is not valid UTF-8: {_safe_path_fragment(relative_path)!r}",
            )

        return ReadResult(success=True, content=content, byte_count=len(raw))
