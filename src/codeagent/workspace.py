"""Milestone 1 slice B: a narrowly scoped disposable Git worktree.

Scope: create a detached, task-specific `git worktree` from a real
source repository, expose its path only after successful creation, and
remove it unconditionally on exit — nothing else. No branch management,
no remote operations, no general workspace tooling (that is Milestone 2
territory). All git invocations use structured argv (never
`shell=True`) and only porcelain commands (`worktree`, `status`,
`rev-parse`) — nothing here hand-edits `.git` internals.

This module never has a reference to anything other than the source
repository path and the worktree path it creates — it structurally
cannot write into any other location.

Cleanup discipline (`__exit__`): the temporary directory is always
removed, in a `finally`. `git worktree remove` failure is detected, not
ignored; a stale registration left behind by that failure triggers a
recovery attempt (`git worktree prune`) before being surfaced as
`GitWorktreeCleanupError` — but only when no other exception is already
propagating out of the `with` block, so a real cleanup problem is never
allowed to replace and hide a real error the caller's code raised.
Either way, the outcome is recorded on `self.cleanup_error` so a caller
can check it even when it wasn't raised.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

_WORKTREE_TEMPDIR_PREFIX = "codeagent-worktree-"


class GitWorktreeError(Exception):
    """Raised when the source repository is invalid, or a git worktree
    operation fails. Callers that want a sanitized OperationalError
    should catch this and translate it themselves — this module doesn't
    know about the error taxonomy."""


class GitWorktreeCleanupError(GitWorktreeError):
    """Raised from __exit__ when the worktree's git registration could
    not be removed or recovered, and no other exception is already
    propagating from the with-block (see module docstring)."""


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True)
class SourceSnapshot:
    """What the source repository looked like immediately before a
    worktree was created from it — used to prove it was left alone."""

    head: str
    status_porcelain: str
    worktree_list_porcelain: str


class GitWorktree:
    """Context manager: `with GitWorktree(source_repo, run_id) as path:`
    creates a detached worktree at a fresh temporary location and
    removes it on exit, success or failure.
    """

    def __init__(self, source_repo_path: Path | str, run_id: str) -> None:
        self.source_repo_path = Path(source_repo_path).resolve()
        if not run_id:
            raise ValueError("run_id must be a nonempty string")
        self.run_id = run_id
        self._validate_source_repo()
        self.path: Path | None = None
        self.cleanup_error: GitWorktreeCleanupError | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None

    def _validate_source_repo(self) -> None:
        if not self.source_repo_path.is_dir():
            raise GitWorktreeError(f"source repo path does not exist: {self.source_repo_path}")
        result = _run_git(
            "-C", str(self.source_repo_path), "rev-parse", "--is-inside-work-tree", check=False
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise GitWorktreeError(
                f"{self.source_repo_path} is not a git working tree: {result.stderr.strip()}"
            )

    def _rev_parse(self, ref: str) -> str:
        result = _run_git("-C", str(self.source_repo_path), "rev-parse", ref)
        return result.stdout.strip()

    def _registration_status(self, path: Path) -> bool | None:
        """True: still registered. False: confirmed absent. None:
        couldn't determine (the listing command itself failed) — this
        must never be treated as "confirmed absent"; a caller that
        can't verify cleanup succeeded has to assume it didn't.

        Parses exact `worktree <path>` record lines from
        `--porcelain` output rather than substring-matching the raw
        text, so a path that happens to be a prefix of another
        registered path (e.g. `/tmp/run-1` vs. `/tmp/run-12`) can't
        produce a false positive or negative.
        """
        result = _run_git(
            "-C", str(self.source_repo_path), "worktree", "list", "--porcelain", check=False
        )
        if result.returncode != 0:
            return None
        # git resolves symlinked temp-directory components (e.g. macOS's
        # /var -> /private/var) in its own porcelain output; comparing
        # against an unresolved path would never match even when the
        # entry genuinely is present, so resolve before comparing.
        # resolve() works even if the path no longer exists on disk.
        target = str(Path(path).resolve())
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if line[len("worktree ") :] == target:
                    return True
        return False

    def snapshot_source(self) -> SourceSnapshot:
        """Record source HEAD and working-copy status. Safe to call at
        any time — read-only."""
        status = _run_git(
            "-C", str(self.source_repo_path), "status", "--porcelain=v1", "--branch"
        ).stdout
        worktree_list = _run_git(
            "-C", str(self.source_repo_path), "worktree", "list", "--porcelain"
        ).stdout
        return SourceSnapshot(
            head=self._rev_parse("HEAD"),
            status_porcelain=status,
            worktree_list_porcelain=worktree_list,
        )

    def __enter__(self) -> Path:
        if self.path is not None:
            raise GitWorktreeError(
                f"GitWorktree for run {self.run_id!r} is already active — create a new "
                "instance for a new worktree rather than re-entering one that's in use"
            )
        source_head = self._rev_parse("HEAD")
        self._tempdir = tempfile.TemporaryDirectory(prefix=_WORKTREE_TEMPDIR_PREFIX)
        worktree_path = Path(self._tempdir.name) / "worktree"
        try:
            _run_git(
                "-C",
                str(self.source_repo_path),
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                source_head,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self._tempdir.cleanup()
            self._tempdir = None
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            raise GitWorktreeError(f"git worktree add failed: {detail}") from exc
        self.path = worktree_path
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        cleanup_error: GitWorktreeCleanupError | None = None
        try:
            if self.path is not None:
                removed_path = self.path
                remove_result = _run_git(
                    "-C",
                    str(self.source_repo_path),
                    "worktree",
                    "remove",
                    "--force",
                    str(removed_path),
                    check=False,
                )
                if remove_result.returncode != 0:
                    # Recovery attempt: ensure the directory is actually
                    # gone, then prune the now-stale registration.
                    shutil.rmtree(removed_path, ignore_errors=True)
                    _run_git(
                        "-C", str(self.source_repo_path), "worktree", "prune", check=False
                    )
                    status = self._registration_status(removed_path)
                    # Anything other than a *confirmed* absence (False)
                    # — still registered (True), or the listing command
                    # itself failed (None) — counts as an unresolved
                    # cleanup failure. Never treat "couldn't check" as
                    # "must be fine."
                    if status is not False:
                        cleanup_error = GitWorktreeCleanupError(
                            f"failed to remove git worktree registration for run "
                            f"{self.run_id!r} even after pruning (status: "
                            f"{'unknown — listing failed' if status is None else 'still registered'})"
                        )
                self.path = None
        finally:
            if self._tempdir is not None:
                self._tempdir.cleanup()
                self._tempdir = None

        self.cleanup_error = cleanup_error
        if cleanup_error is not None and exc_type is None:
            # Only raise when nothing else is already propagating —
            # otherwise this would replace (mask) the real failure the
            # with-block raised. The caller can still see cleanup_error
            # on this instance either way.
            raise cleanup_error
