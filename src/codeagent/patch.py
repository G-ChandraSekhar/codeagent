"""Milestone 1 slice B: one deliberately narrow, controlled patch
operation — exact expected-text replacement in a single file — plus a
real Git commit as the checkpoint.

This is NOT the Milestone 2 patch engine. It supports **exactly one**
operation (enforced at construction) and nothing else — no add/delete/
rename, no multiple operations, no fuzzy matching, no general path
validation beyond what this one operation needs. Multiple operations
against the same file starting from the same original content would
overwrite one another; rather than build the bookkeeping to do that
safely, this is deferred to Milestone 2's atomic multi-file patch
engine, where it belongs.

Every check below — including approved-path authorization, see
`apply()` — runs to completion *before* anything is written to disk or
committed. A rejected operation leaves the worktree exactly as it was:
no write, no `git add`, no commit.

Persisted error messages are sanitized: no raw Git stderr, no absolute
filesystem paths, no repository content, and any caller-supplied path
value is stripped of non-printable characters and length-bounded before
inclusion. See `_safe_path_fragment`.

Known limitation, not fixed by this module: if a Git stage fails
*after* the file has already been written to disk (i.e. `git add`,
`diff`, `commit`, or `rev-parse` fails following a successful write),
the worktree is left with that uncommitted write present until its
surrounding `GitWorktree` context tears the whole worktree down.
There is no in-place rollback of a partial application within a single
`apply()` call — transactional rollback is Milestone 2 work. This is a
real gap, not a theoretical one: it means a worktree inspected between
such a failure and its `GitWorktree` cleanup would show an uncommitted,
dirty change.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeagent.controller import PatchResult
from codeagent.errors import ErrorCode, OperationalError

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent",
    "-c",
    "user.email=codeagent@example.invalid",
]

_MAX_MESSAGE_PATH_LENGTH = 200


def _safe_path_fragment(value: str) -> str:
    """Render a caller-supplied path value safely for inclusion in a
    persisted message: strip non-printable characters (defends against
    log-injection via control characters) and bound the length."""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in value)
    if len(cleaned) > _MAX_MESSAGE_PATH_LENGTH:
        cleaned = cleaned[:_MAX_MESSAGE_PATH_LENGTH] + "...(truncated)"
    return cleaned


def _run_git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *_GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True)
class PatchOperation:
    """Replace exactly one occurrence of `expected_text` with `new_text`
    in the file at `relative_path` (relative to the worktree root)."""

    relative_path: str
    expected_text: str
    new_text: str


class _ValidationFailure(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _validate_path(worktree: Path, relative_path: str) -> Path:
    if not relative_path:
        raise _ValidationFailure("relative_path must be nonempty")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise _ValidationFailure(
            f"relative_path must not be absolute: {_safe_path_fragment(relative_path)!r}"
        )
    if any(part == ".." for part in candidate.parts):
        raise _ValidationFailure(
            f"relative_path must not contain '..': {_safe_path_fragment(relative_path)!r}"
        )

    resolved_worktree = worktree.resolve()
    target = (resolved_worktree / candidate).resolve()
    if not (target == resolved_worktree or resolved_worktree in target.parents):
        raise _ValidationFailure(
            f"relative_path resolves outside the worktree: {_safe_path_fragment(relative_path)!r}"
        )

    # Reject if any existing path component is a symlink — a symlink
    # partway along the path could resolve on-disk writes outside the
    # worktree even though the textual path looks contained.
    probe = resolved_worktree
    for part in candidate.parts:
        probe = probe / part
        if probe.is_symlink():
            raise _ValidationFailure(
                f"path escapes through a symlink: {_safe_path_fragment(relative_path)!r}"
            )
        if probe == target:
            break

    if not target.is_file():
        raise _ValidationFailure(
            f"target file does not exist: {_safe_path_fragment(relative_path)!r}"
        )

    return target


def _validate_and_read(worktree: Path, operation: PatchOperation) -> tuple[Path, str]:
    if not operation.expected_text:
        raise _ValidationFailure("expected_text must be nonempty")
    if operation.expected_text == operation.new_text:
        raise _ValidationFailure(
            "expected_text and new_text must differ — this would be a no-op replacement"
        )

    target = _validate_path(worktree, operation.relative_path)

    try:
        original_text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _ValidationFailure(
            f"target file is not valid UTF-8: {_safe_path_fragment(operation.relative_path)!r}"
        ) from None
    except OSError:
        raise _ValidationFailure(
            f"target file could not be read: {_safe_path_fragment(operation.relative_path)!r}"
        ) from None

    occurrences = original_text.count(operation.expected_text)
    if occurrences == 0:
        raise _ValidationFailure(
            f"expected_text not found in {_safe_path_fragment(operation.relative_path)!r}"
        )
    if occurrences > 1:
        raise _ValidationFailure(
            f"expected_text matches {occurrences} times in "
            f"{_safe_path_fragment(operation.relative_path)!r}, ambiguous — must match exactly once"
        )
    return target, original_text


class GitPatchApplier:
    """Applies exactly one PatchOperation inside a real worktree and
    commits it as the checkpoint. Constructed with the operation to
    apply — see module docstring for why this is fixed at construction
    rather than accepting arbitrary patches at call time, and why it's
    exactly one rather than a tuple.
    """

    def __init__(self, worktree_path: Path, operations: tuple[PatchOperation, ...]) -> None:
        if len(operations) != 1:
            raise ValueError(
                "GitPatchApplier supports exactly one PatchOperation "
                f"(got {len(operations)}) — multi-operation transactional patching "
                "is Milestone 2 work, not partially implemented here"
            )
        self._worktree_path = Path(worktree_path)
        self._operation = operations[0]
        self._next_error_id_seq = 0

    def _next_error_id(self, run_id: str) -> str:
        self._next_error_id_seq += 1
        return f"{run_id}-patch-err-{self._next_error_id_seq}"

    def _validation_failure_result(self, run_id: str, message: str) -> PatchResult:
        return PatchResult(
            success=False,
            operation_count=0,
            changed_paths=(),
            diff_bytes=0,
            commit_hash=None,
            error=OperationalError(
                code=ErrorCode.PATCH_VALIDATION_FAILED,
                error_id=self._next_error_id(run_id),
                message=message,
            ),
        )

    def _application_failure_result(self, run_id: str, message: str) -> PatchResult:
        return PatchResult(
            success=False,
            operation_count=0,
            changed_paths=(),
            diff_bytes=0,
            commit_hash=None,
            error=OperationalError(
                code=ErrorCode.PATCH_APPLICATION_FAILED,
                error_id=self._next_error_id(run_id),
                message=message,
            ),
        )

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str]
    ) -> PatchResult:
        """Apply the configured operation, or reject it, and report what
        actually happened.

        `approved_paths` is the *preventive* authorization boundary:
        the operation's target must be one of these paths — normally
        `PlanProposal.proposed_file_paths` — checked before any
        validation, read, write, or Git command runs at all. This is
        the primary enforcement; RunController may additionally check
        `PatchResult.changed_paths` against the same set afterward as
        defense in depth, not as the thing that actually prevents an
        unapproved write (by the time that check ran, the mutation
        would already have happened) — see controller.py's
        `_dispatch_apply_patch` docstring.
        """
        operation = self._operation

        if operation.relative_path not in approved_paths:
            return self._validation_failure_result(
                run_id,
                f"target path is not in the approved plan's file list: "
                f"{_safe_path_fragment(operation.relative_path)!r}",
            )

        try:
            target, original_text = _validate_and_read(self._worktree_path, operation)
        except _ValidationFailure as exc:
            return self._validation_failure_result(run_id, exc.message)

        new_text = original_text.replace(operation.expected_text, operation.new_text, 1)

        try:
            target.write_text(new_text, encoding="utf-8")
        except OSError:
            return self._application_failure_result(
                run_id,
                f"failed to write the patched file: "
                f"{_safe_path_fragment(operation.relative_path)!r}",
            )

        # Stage only the validated target — never `git add -A`, which
        # would sweep in any other worktree change (from a concurrent
        # process, a leftover from a prior failed attempt, etc.) into
        # this checkpoint.
        try:
            _run_git(self._worktree_path, "add", "--", operation.relative_path)
        except (OSError, subprocess.CalledProcessError):
            return self._application_failure_result(run_id, "failed to stage the patched file")

        try:
            diff_output = _run_git(self._worktree_path, "diff", "--no-color", "--cached").stdout
        except (OSError, subprocess.CalledProcessError):
            return self._application_failure_result(run_id, "failed to compute the patch diff")
        diff_bytes = len(diff_output.encode("utf-8"))

        try:
            _run_git(
                self._worktree_path,
                "commit",
                "--quiet",
                "-m",
                f"codeagent: apply patch (iteration {iteration})",
            )
        except (OSError, subprocess.CalledProcessError):
            return self._application_failure_result(run_id, "failed to commit the patch")

        try:
            commit_hash = _run_git(self._worktree_path, "rev-parse", "HEAD").stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return self._application_failure_result(
                run_id, "failed to resolve the new commit hash"
            )

        return PatchResult(
            success=True,
            operation_count=1,
            changed_paths=(operation.relative_path,),
            diff_bytes=diff_bytes,
            commit_hash=commit_hash,
        )
