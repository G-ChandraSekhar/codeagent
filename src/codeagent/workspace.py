"""Milestone 1 slice B, hardened per ADR 0006 (Milestone 2, T-M3
closure for workspace creation and source-repository inspection only):
a narrowly scoped disposable Git worktree.

Scope: create a detached, task-specific `git worktree` from a real
source repository, expose its path only after successful creation, and
remove it unconditionally on exit — nothing else. No branch management,
no remote operations, no general workspace tooling (that is Milestone 2
territory). All git invocations use structured argv (never
`shell=True`) and only porcelain commands — nothing here hand-edits
`.git` internals.

This module never has a reference to anything other than the source
repository path and the worktree path it creates — it structurally
cannot write into any other location.

Git safety (ADR 0006), this slice's actual scope:

- Every Git invocation here goes through `codeagent._git_safety.run_git`
  via this module's own `_run` seam: the Git >= 2.45 / `--no-lazy-fetch`
  preflight (checked once, in `__init__`, before any repository access),
  the sanitized `GIT_*` environment, the hardened baseline argv
  (`core.hooksPath=/dev/null`, `core.fsmonitor=false`,
  `core.autocrlf=false`, `submodule.recurse=false`, `--no-pager`,
  `--no-lazy-fetch`), a bounded timeout, and categorical, sanitized
  failure translation into `GitWorktreeError` — never raw stderr,
  argv, repository/worktree paths, environment values, or untrusted
  Git output.
- Repository, global, and system-level Git configuration remain fully
  visible to every invocation, intentionally: `GIT_CONFIG_NOSYSTEM` is
  never set, matching `_git_safety`'s own discipline.
- Worktree creation never performs a materializing checkout before
  ADR 0006 section 1's primary control has run: `git worktree add
  --no-checkout --detach` registers the worktree and sets its detached
  HEAD without touching the filesystem or the index; `git read-tree`
  then populates only the index; every tracked path's `filter`
  attribute is inspected (`_git_safety.evaluate_tracked_filter_safety`,
  bounded/chunked/NUL-safe, driver-set-dependent per finding 16) before
  a real, hardened `git checkout -- .` ever materializes a single file.
  A refusal or any failure after the worktree is registered removes
  exactly that registration (no repository-wide sweep, no `prune`) and
  confirms it by observation. If that confirmation fails — the
  registration persists, its status can't be determined, or the
  temporary directory itself can't be removed — a sanitized
  `GitWorktreeCleanupError` is raised in place of the original
  refusal/failure, which is preserved as its explicit `__cause__`
  (`raise cleanup_error from failure`) rather than silently dropped.
- `snapshot_source()`'s `git status` call additionally carries ADR 0006
  section 1's secondary control (enumerate-and-neutralize) as a
  backstop, since `status` can trigger a clean filter during Git's
  racy-stat comparison and has no pre-materialization inspection point
  of its own.

Explicitly **not** done in this slice (out of scope; see the ADR 0006
workspace-integration session in ENGINEERING_LOG.md): `patch.py`,
controller/checkpoint-ref/lifecycle-store integration, cancellation,
reconciliation, CLI or frontend work, and ADR 0004's complete owned-
resource lifecycle. In particular, `__exit__`'s cleanup fallback still
calls `shutil.rmtree` and a repository-wide `git worktree prune` when
`git worktree remove` itself fails — this is the same pre-existing gap
threat-model T-M3 already flagged (ADR 0004 I2) and is **not** resolved
here; only the *new* enter-time failure path (`_cleanup_failed_worktree`)
is held to the stricter "no repository-wide sweep" standard this slice
introduces for its own new code.

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

from codeagent import _git_safety

_WORKTREE_TEMPDIR_PREFIX = "codeagent-worktree-"


class GitWorktreeError(Exception):
    """Raised when the source repository is invalid, or a git worktree
    operation fails. Callers that want a sanitized OperationalError
    should catch this and translate it themselves — this module doesn't
    know about the error taxonomy.

    Messages are fixed, categorical text only: never raw stderr, git
    argv, repository/worktree paths, environment values, or untrusted
    Git output (matching `codeagent._git_safety.GitSafetyError`, which
    every message here either reuses directly or matches in style).
    """


class GitWorktreeCleanupError(GitWorktreeError):
    """Raised from __exit__ when the worktree's git registration could
    not be removed or recovered, and no other exception is already
    propagating from the with-block (see module docstring)."""


def _run(
    repo_path: Path, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The single seam every Git invocation in this module goes
    through: `codeagent._git_safety.run_git`'s preflight-verified
    baseline argv, sanitized environment, and bounded timeout.

    Never raises for a nonzero exit — callers must check
    `result.returncode` themselves, exactly like
    `subprocess.run(check=False)`. Raises `GitWorktreeError` only for
    an infrastructure-level failure (`_git_safety.GitSafetyError`: the
    executable could not be launched, a timeout, an unsupported Git
    version/substrate, or a filter/attribute-enumeration failure),
    reusing that error's already-sanitized categorical message.
    """
    try:
        return _git_safety.run_git(repo_path, *args, input_text=input_text)
    except _git_safety.GitSafetyError as exc:
        raise GitWorktreeError(exc.message) from exc


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
        # ADR 0006 section 6: a one-time capability check, before any
        # repository access at all.
        try:
            _git_safety.check_git_preflight()
        except _git_safety.GitSafetyError as exc:
            raise GitWorktreeError(exc.message) from exc

        try:
            self.source_repo_path = Path(source_repo_path).resolve()
        except (OSError, RuntimeError) as exc:
            # Path.resolve() can raise OSError (e.g. a permission
            # problem) or RuntimeError (a symlink loop) whose own
            # message embeds the filesystem path. `from None` discards
            # that exception entirely (not merely its message) so the
            # path can never resurface via `__cause__`/`__context__` in
            # a traceback.
            raise GitWorktreeError("the source repository path could not be resolved") from None
        if not run_id:
            raise ValueError("run_id must be a nonempty string")
        self.run_id = run_id
        self._validate_source_repo()
        self.path: Path | None = None
        self.cleanup_error: GitWorktreeCleanupError | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None

    def _validate_source_repo(self) -> None:
        if not self.source_repo_path.is_dir():
            raise GitWorktreeError("source repository path does not exist")
        result = _run(self.source_repo_path, "rev-parse", "--is-inside-work-tree")
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise GitWorktreeError("source repository path is not a git working tree")

    def _rev_parse(self, ref: str) -> str:
        result = _run(self.source_repo_path, "rev-parse", ref)
        if result.returncode != 0:
            raise GitWorktreeError("git rev-parse failed for the source repository")
        return result.stdout.strip()

    def _registration_status(self, path: Path) -> bool | None:
        """True: still registered. False: confirmed absent. None:
        couldn't determine (the listing command itself failed, or could
        not even be launched) — this must never be treated as
        "confirmed absent"; a caller that can't verify cleanup succeeded
        has to assume it didn't.

        Parses exact `worktree <path>` record lines from
        `--porcelain` output rather than substring-matching the raw
        text, so a path that happens to be a prefix of another
        registered path (e.g. `/tmp/run-1` vs. `/tmp/run-12`) can't
        produce a false positive or negative.
        """
        try:
            result = _run(self.source_repo_path, "worktree", "list", "--porcelain")
        except GitWorktreeError:
            return None
        if result.returncode != 0:
            return None
        # git resolves symlinked temp-directory components (e.g. macOS's
        # /var -> /private/var) in its own porcelain output; comparing
        # against an unresolved path would never match even when the
        # entry genuinely is present, so resolve before comparing.
        # resolve() works even if the path no longer exists on disk —
        # but it can still raise (e.g. a permission problem or a
        # symlink loop). That must be "unknown," never a raw
        # filesystem-detail leak or an uncaught exception: the caller
        # (enter-time cleanup) needs to still attempt the independent
        # tempdir-cleanup step and surface a sanitized
        # GitWorktreeCleanupError, not crash here.
        try:
            target = str(Path(path).resolve())
        except (OSError, RuntimeError):
            return None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if line[len("worktree ") :] == target:
                    return True
        return False

    def snapshot_source(self) -> SourceSnapshot:
        """Record source HEAD and working-copy status. Safe to call at
        any time — read-only.

        `git status` carries ADR 0006 section 1's secondary control
        (enumerate-and-neutralize) as a backstop: `status` can trigger
        a clean filter during Git's racy-stat comparison, and unlike
        `worktree add` it has no pre-materialization inspection point
        of its own to rely on instead.
        """
        try:
            neutralization = _git_safety.enumerate_filter_neutralization(self.source_repo_path)
        except _git_safety.GitSafetyError as exc:
            raise GitWorktreeError(exc.message) from exc

        status_result = _run(
            self.source_repo_path,
            *neutralization.args,
            "status",
            "--porcelain=v1",
            "--branch",
        )
        if status_result.returncode != 0:
            raise GitWorktreeError("git status failed for the source repository")

        worktree_list_result = _run(self.source_repo_path, "worktree", "list", "--porcelain")
        if worktree_list_result.returncode != 0:
            raise GitWorktreeError("git worktree list failed for the source repository")

        return SourceSnapshot(
            head=self._rev_parse("HEAD"),
            status_porcelain=status_result.stdout,
            worktree_list_porcelain=worktree_list_result.stdout,
        )

    def __enter__(self) -> Path:
        if self.path is not None:
            raise GitWorktreeError(
                f"GitWorktree for run {self.run_id!r} is already active — create a new "
                "instance for a new worktree rather than re-entering one that's in use"
            )
        # A retry after a previously failed __enter__() attempt (self.path
        # is None, so re-entry is allowed above) must not carry forward a
        # stale cleanup_error from that earlier attempt.
        self.cleanup_error = None
        source_head = self._rev_parse("HEAD")
        try:
            self._tempdir = tempfile.TemporaryDirectory(prefix=_WORKTREE_TEMPDIR_PREFIX)
        except OSError:
            # `from None`: the raw OSError's message can embed a
            # filesystem path (e.g. a permissions failure naming the
            # parent temp directory) and must never resurface via
            # __cause__/__context__ in a traceback.
            raise GitWorktreeError("a temporary directory could not be created") from None
        worktree_path = Path(self._tempdir.name) / "worktree"

        # From here on, `git worktree add` is treated as potentially
        # mutating no matter how it concludes — a nonzero result, an
        # infrastructure error from `_run`, or the caller being
        # interrupted right after Git actually created the
        # registration all land in the same except block below, which
        # always attempts exact removal and confirms it by observation
        # rather than assuming a failure report means nothing exists.
        try:
            # Step 1 (ADR 0006 section 1): register the worktree with
            # --no-checkout. Nothing is materialized and the index is
            # left empty by Git itself — no filter or hook can run yet.
            add_result = _run(
                self.source_repo_path,
                "worktree",
                "add",
                "--no-checkout",
                "--detach",
                str(worktree_path),
                source_head,
            )
            if add_result.returncode != 0:
                raise GitWorktreeError("git worktree add failed")

            # Step 2: populate only the index — no filter/hook
            # execution point exists for `read-tree`.
            read_tree_result = _run(worktree_path, "read-tree", source_head)
            if read_tree_result.returncode != 0:
                raise GitWorktreeError("populating the worktree index failed")

            # Step 3: the primary control. Refuse before a single file
            # is materialized if any tracked path has an active or
            # ambiguous filter attribute (ADR 0006 finding 16).
            try:
                safe = _git_safety.evaluate_tracked_filter_safety(worktree_path)
            except _git_safety.GitSafetyError as exc:
                raise GitWorktreeError(
                    "the source commit's filter-attribute safety could not be verified"
                ) from exc
            if not safe:
                raise GitWorktreeError(
                    "refused: the source commit contains a tracked path with an active "
                    "or ambiguous filter attribute"
                )

            # Step 4: only now, with every tracked path already cleared,
            # perform the real materializing checkout under the same
            # hardened baseline.
            checkout_result = _run(worktree_path, "checkout", "--", ".")
            if checkout_result.returncode != 0:
                raise GitWorktreeError("materializing the worktree's files failed")
        except BaseException as failure:
            # Both cleanup steps are always attempted, regardless of
            # whether the first one failed, and regardless of whether
            # `add` itself is what failed: a nonzero add result is
            # never assumed to mean nothing was created, so the same
            # exact-removal-plus-observation applies whether the
            # failure came from `add`, `read-tree`, the filter-safety
            # check, or `checkout`.
            registration_error = self._cleanup_failed_worktree(worktree_path)
            tempdir_error = self._cleanup_failed_tempdir()
            cleanup_error = self._combine_cleanup_errors(registration_error, tempdir_error)
            if cleanup_error is not None:
                # Fail closed: a cleanup problem after a refusal is
                # itself surfaced, not merely recorded — but the
                # original refusal/failure is preserved as the
                # explicit cause, never silently dropped.
                self.cleanup_error = cleanup_error
                raise cleanup_error from failure
            raise

        self.path = worktree_path
        return self.path

    def _cleanup_failed_worktree(self, worktree_path: Path) -> GitWorktreeCleanupError | None:
        """Undo exactly the worktree registration this failed
        `__enter__` attempt created — no repository-wide sweep (no
        `prune`, no `rmtree` fallback), unlike `__exit__`'s existing,
        broader recovery path (see module docstring: that gap is
        ADR 0004 I2, not resolved here).

        Best-effort removal, confirmed by observation. Returns a
        sanitized `GitWorktreeCleanupError` if removal cannot be
        confirmed (still registered, or the listing itself could not
        be determined); returns `None` on confirmed removal. Never
        raises itself — the caller decides what to do with the result.
        """
        try:
            _run(self.source_repo_path, "worktree", "remove", "--force", str(worktree_path))
        except GitWorktreeError:
            pass
        if self._registration_status(worktree_path) is not False:
            return GitWorktreeCleanupError(
                "the worktree created for this failed attempt could not be confirmed removed"
            )
        return None

    def _cleanup_failed_tempdir(self) -> GitWorktreeCleanupError | None:
        """Best-effort removal of the temporary directory backing a
        failed `__enter__` attempt. Any failure (permissions, a locked
        file, or any other `OSError`) is translated into a sanitized
        categorical error — never the raw `OSError`, whose message
        embeds the filesystem path. Returns `None` on success or when
        there is no temp directory to clean up."""
        tempdir = self._tempdir
        self._tempdir = None
        if tempdir is None:
            return None
        try:
            tempdir.cleanup()
        except OSError:
            return GitWorktreeCleanupError(
                "the temporary directory for this failed attempt could not be removed"
            )
        return None

    def _combine_cleanup_errors(
        self,
        registration_error: GitWorktreeCleanupError | None,
        tempdir_error: GitWorktreeCleanupError | None,
    ) -> GitWorktreeCleanupError | None:
        """Combine the two independent enter-time cleanup outcomes into
        at most one error. If both failed, that fact is represented in
        a single sanitized message — neither failure is silently
        discarded in favor of the other."""
        if registration_error is not None and tempdir_error is not None:
            return GitWorktreeCleanupError(
                "neither the worktree registration nor the temporary directory for this "
                "failed attempt could be confirmed removed"
            )
        return registration_error or tempdir_error

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
                try:
                    remove_result = _run(
                        self.source_repo_path, "worktree", "remove", "--force", str(removed_path)
                    )
                    remove_failed = remove_result.returncode != 0
                except GitWorktreeError:
                    remove_failed = True
                if remove_failed:
                    # Recovery attempt: ensure the directory is actually
                    # gone, then prune the now-stale registration.
                    # NOT resolved to the narrower "exact removal only"
                    # standard in this slice — see module docstring
                    # (ADR 0004 I2).
                    shutil.rmtree(removed_path, ignore_errors=True)
                    try:
                        _run(self.source_repo_path, "worktree", "prune")
                    except GitWorktreeError:
                        pass
                    status = self._registration_status(removed_path)
                    # Anything other than a *confirmed* absence (False)
                    # — still registered (True), or the listing command
                    # itself failed (None) — counts as an unresolved
                    # cleanup failure. Never treat "couldn't check" as
                    # "must be fine."
                    if status is not False:
                        cleanup_error = GitWorktreeCleanupError(
                            "failed to remove the git worktree registration for this run "
                            "even after pruning"
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
