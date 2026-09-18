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

Explicitly **not** done in this slice (out of scope): `patch.py`
integration beyond what `entry_gate()`/`dispose()`/`preserve()` expose,
checkpoint-ref/lifecycle-store durability, cancellation, reconciliation,
CLI or frontend work, and ADR 0004's complete owned-resource lifecycle
(locks, dead-run reconciliation, abandonment).

Ownership and cleanup discipline (Milestone 2 slice 2B-2, ADR 0003
Amendment 2) — `dispose()`/`preserve()`/`__exit__`:

- `dispose()` is the **one real disposal path**: exact `git worktree
  remove --force`, confirmed registration absence, and confirmed
  directory absence. **No `shutil.rmtree` and no repository-wide `git
  worktree prune` fallback anywhere in this module** — an unconfirmed
  exact disposal raises `GitWorktreeCleanupError` loudly instead of
  silently degrading to a broader sweep. (This closes the T-M3/ADR 0004
  I2 gap `__exit__`'s previous prune/rmtree fallback left open; the
  enter-time failure path, `_cleanup_failed_worktree`, already met this
  "no repository-wide sweep" bar and is unchanged.)
- `preserve()` marks the worktree deliberately retained — used only by
  a controller when verifier/container cleanup is `UNCONFIRMED` — so
  `__exit__` cannot silently dispose of a resource that was
  intentionally kept for later reconciliation.
- `__exit__` dispatches on the tri-state `active`/`disposed`/`preserved`
  disposition: `preserved`/`disposed` are no-ops; `active` (either an
  exception before a controller's own teardown began, or a direct,
  non-controller caller) runs the same exact `dispose()` path. A direct
  `with GitWorktree(...) as path:` caller therefore still gets exact,
  idempotent, loudly-failing disposal with no special controller
  involvement required.
- The outcome is recorded on `self.cleanup_error` so a caller can check
  it even when `__exit__` chose not to re-raise it (because another
  exception was already propagating from the `with` block).
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal

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
        self._initial_commit: str | None = None
        # "active": ordinary state, not yet disposed or preserved.
        # "disposed": dispose() has confirmed exact removal.
        # "preserved": the controller deliberately retained this
        # worktree (verifier/container cleanup was UNCONFIRMED) — see
        # preserve()/dispose()/__exit__'s module-docstring-level
        # ownership-transfer discipline (ADR 0003 Amendment 2).
        self._disposition: Literal["active", "disposed", "preserved"] = "active"

    @property
    def initial_commit(self) -> str:
        """The pinned starting commit this worktree was created from —
        the sole authority for ADR 0003 Amendment 2's initial
        checkpoint (no separate caller-controlled `initial_checkpoint_id`
        exists). Raises before the worktree has ever been successfully
        entered."""
        if self._initial_commit is None:
            raise GitWorktreeError("initial_commit is not available before __enter__ succeeds")
        return self._initial_commit

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
        self._initial_commit = source_head
        return self.path

    def entry_gate(self, expected_commit: str) -> None:
        """ADR 0003 Amendment 2's workspace entry gate: before
        establishing or relying on a checkpoint (lazy establishment, or
        resuming before a further patch attempt), freshly confirm the
        worktree is exactly where it is expected to be:

        - the worktree's freshly observed `HEAD` equals `expected_commit`;
        - the staged index is clean relative to `HEAD`;
        - the tracked working tree is clean relative to `HEAD`;
        - there are no untracked paths at all.

        Any condition that cannot be evaluated (a failed Git call) fails
        closed via `GitWorktreeError` — never silently treated as
        satisfied. This is an entry/resume-time gate only (ADR 0003
        point 6): it says nothing about the tree's state once a patch
        attempt is actively in progress.
        """
        if self.path is None:
            raise GitWorktreeError("entry_gate requires an active worktree")

        observed_head = self._rev_parse_at(self.path, "HEAD")
        if observed_head != expected_commit:
            raise GitWorktreeError(
                "the worktree's HEAD does not match the expected checkpoint"
            )

        staged_diff = _run(self.path, "diff", "--cached", "--quiet", "HEAD", "--")
        if staged_diff.returncode == 1:
            raise GitWorktreeError("the worktree's staged index is not clean")
        if staged_diff.returncode != 0:
            raise GitWorktreeError("the worktree's staged index could not be inspected")

        tree_diff = _run(self.path, "diff", "--quiet", "HEAD", "--")
        if tree_diff.returncode == 1:
            raise GitWorktreeError("the worktree's tracked working tree is not clean")
        if tree_diff.returncode != 0:
            raise GitWorktreeError("the worktree's tracked working tree could not be inspected")

        status_result = _run(self.path, "status", "--porcelain=v1", "--untracked-files=all")
        if status_result.returncode != 0:
            raise GitWorktreeError("the worktree's status could not be inspected")
        if status_result.stdout.strip() != "":
            raise GitWorktreeError("the worktree contains untracked paths")

    def _rev_parse_at(self, repo_path: Path, ref: str) -> str:
        result = _run(repo_path, "rev-parse", ref)
        if result.returncode != 0:
            raise GitWorktreeError("git rev-parse failed for the worktree")
        return result.stdout.strip()

    def dispose(self) -> None:
        """The one real disposal path: exact `git worktree remove
        --force`, confirmed registration absence, and confirmed
        directory absence. No `shutil.rmtree`, no `git worktree prune`
        fallback anywhere in this path.

        Idempotent: a no-op once already `disposed` or `preserved` (see
        `preserve()`). Raises `GitWorktreeCleanupError` loudly if exact
        disposal cannot be confirmed — never silently treated as
        successful.
        """
        if self._disposition != "active":
            return
        if self.path is None:
            self._disposition = "disposed"
            return

        worktree_path = self.path
        # `git worktree remove`'s own reported outcome (nonzero exit, or
        # an infrastructure error from `_run`) is deliberately NOT part
        # of the success decision below — it is only an *attempt*. A
        # command that reports failure can still have actually removed
        # the registration (e.g. a race, a partial failure after the
        # mutating effect already landed, or a wrapper/version
        # difference in what counts as a reportable error); conversely
        # a command that reports success is not itself trusted either.
        # Only the independent final observation — registration status
        # plus directory presence — decides whether disposal succeeded.
        try:
            _run(self.source_repo_path, "worktree", "remove", "--force", str(worktree_path))
        except GitWorktreeError:
            pass

        registration_status = self._registration_status(worktree_path)
        try:
            directory_absent = not worktree_path.exists()
        except OSError:
            directory_absent = False

        if registration_status is not False or not directory_absent:
            raise GitWorktreeCleanupError(
                "the git worktree could not be confirmed exactly removed"
            )

        # Only reached once the worktree's own registration and content
        # are confirmed gone: the temporary directory wrapping it is now
        # an ordinary (already-empty, or nearly so) directory, not live
        # worktree content being force-deleted as a substitute for a
        # failed `git worktree remove` — this is not the "rmtree
        # fallback" this design forbids.
        #
        # `self._tempdir` is deliberately left alone (not cleared) on a
        # tempdir-cleanup failure, so a later retry of dispose() — the
        # worktree portion above being independently idempotent and
        # already-confirmed-gone — can attempt the tempdir cleanup
        # again rather than being permanently stuck. A `cleanup()` call
        # that raises `FileNotFoundError` (the directory is already
        # gone, e.g. a prior attempt actually succeeded despite raising
        # for an unrelated reason) is treated as success, confirmed by
        # the same real-observation discipline as the worktree check
        # above rather than by trusting `cleanup()`'s own outcome alone.
        if self._tempdir is not None:
            tempdir_path = Path(self._tempdir.name)
            try:
                self._tempdir.cleanup()
            except FileNotFoundError:
                pass
            except OSError:
                raise GitWorktreeCleanupError(
                    "the temporary directory backing this worktree could not be removed"
                ) from None
            if tempdir_path.exists():
                raise GitWorktreeCleanupError(
                    "the temporary directory backing this worktree could not be confirmed removed"
                )
            self._tempdir = None

        self.path = None
        self._disposition = "disposed"

    def preserve(self) -> None:
        """Mark this worktree as deliberately retained. Called only by
        the controller, only when verifier/container cleanup is
        UNCONFIRMED: the worktree (and, separately, the controller's own
        checkpoint session/ref — never touched by this method) must
        survive so a future reconciliation pass can inspect it. Once
        preserved, `__exit__` takes no action on it, and a subsequent
        `dispose()` call is a no-op (see `dispose()`'s idempotence).

        Raises `GitWorktreeError` if called from any state other than
        `active` — preserving an already-disposed or already-preserved
        worktree is a caller error, not a legal no-op.
        """
        if self._disposition != "active":
            raise GitWorktreeError("cannot preserve a worktree that is not active")
        self._disposition = "preserved"

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
        """Tri-state dispatch (ADR 0003 Amendment 2's ownership-transfer
        discipline):

        - `preserved`: the controller deliberately retained this
          worktree (verifier/container cleanup was UNCONFIRMED) — do
          nothing, so a `with`-block exit can never silently dispose of
          a resource the controller chose to keep.
        - `disposed`: `dispose()` already ran and confirmed exact
          removal — idempotent no-op.
        - `active`: `dispose()` was never called — either the
          controller raised before its own teardown began, or this is a
          direct (non-controller) caller. Performs the exact same
          `dispose()` path a controller-driven disposal would, so a
          direct `with GitWorktree(...) as path:` caller still gets
          exact, idempotent, loudly-failing disposal.
        """
        if self._disposition in ("preserved", "disposed"):
            return

        try:
            self.dispose()
            self.cleanup_error = None
        except GitWorktreeCleanupError as cleanup_error:
            self.cleanup_error = cleanup_error
            if exc_type is None:
                # Only raise when nothing else is already propagating —
                # otherwise this would replace (mask) the real failure
                # the with-block raised. The caller can still see
                # cleanup_error on this instance either way.
                raise
