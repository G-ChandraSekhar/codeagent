"""Milestone 1 slice B: GitWorktree lifecycle tests.

Uses a real, throwaway Git repository (tests/support/fixture_repo.py)
and real git subprocess calls — no mocking of git itself, since the
whole point is proving real worktree/cleanup/protection behavior.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from codeagent.workspace import GitWorktree, GitWorktreeCleanupError, GitWorktreeError
from tests.support.fixture_repo import FIXTURE_SOURCE, real_fixture_repo


def _status(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--branch"],
        capture_output=True,
        text=True,
    ).stdout


def _head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_rejects_nonexistent_source_path(tmp_path) -> None:
    with pytest.raises(GitWorktreeError):
        GitWorktree(tmp_path / "does-not-exist", run_id="r1")


def test_rejects_non_git_directory(tmp_path) -> None:
    (tmp_path / "plain_dir").mkdir()
    with pytest.raises(GitWorktreeError):
        GitWorktree(tmp_path / "plain_dir", run_id="r1")


def test_rejects_empty_run_id() -> None:
    with real_fixture_repo() as repo:
        with pytest.raises(ValueError):
            GitWorktree(repo, run_id="")


def test_successful_creation_and_cleanup() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-lifecycle")
        assert wt.path is None  # not exposed before creation
        with wt as path:
            assert wt.path is path
            assert path.is_dir()
            assert (path / "jobs" / "worker.py").is_file()
        assert wt.path is None  # cleared on exit
        assert not path.exists()


def test_cleanup_removes_worktree_registration_from_source() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-registration") as path:
            listing_during = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout
            assert str(path) in listing_during

        listing_after = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        assert str(path) not in listing_after


def test_cleanup_happens_after_an_exception_inside_the_with_block() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-exception")
        captured_path = None
        with pytest.raises(RuntimeError):
            with wt as path:
                captured_path = path
                raise RuntimeError("boom")
        assert wt.path is None
        assert not captured_path.exists()


def test_original_checkout_unchanged_by_worktree_lifecycle() -> None:
    with real_fixture_repo() as repo:
        before_status = _status(repo)
        before_head = _head(repo)
        before_content = (repo / "jobs" / "worker.py").read_text()

        with GitWorktree(repo, run_id="r-protect") as path:
            # Expected, temporary administrative change while the
            # worktree exists: the source repo's .git/worktrees registry
            # gains an entry. That is not a working-copy change.
            assert (repo / ".git" / "worktrees").is_dir()

        after_status = _status(repo)
        after_head = _head(repo)
        after_content = (repo / "jobs" / "worker.py").read_text()

        assert after_status == before_status
        assert after_head == before_head
        assert after_content == before_content


def test_source_repository_path_containing_spaces() -> None:
    """The source repository's own path (not the temp worktree's,
    which never embeds caller input per correction #6) may realistically
    contain spaces — e.g. "My Projects/repo name" — and must work."""
    with TemporaryDirectory(prefix="codeagent fixture with spaces ") as tmp:
        repo = Path(tmp) / "retry worker repo"
        shutil.copytree(FIXTURE_SOURCE, repo)
        subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "add", "-A"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "commit", "--quiet", "-m", "init"],
            check=True,
        )
        assert " " in str(repo)

        with GitWorktree(repo, run_id="r-spaces") as path:
            assert (path / "jobs" / "worker.py").is_file()
        assert not path.exists()


def test_original_checkout_with_preexisting_dirty_state_is_unaffected() -> None:
    """A source repo with its own uncommitted/untracked changes before
    the worktree is created must have those exact changes — byte for
    byte, and status-equivalent — afterward."""
    with real_fixture_repo() as repo:
        worker_path = repo / "jobs" / "worker.py"
        worker_path.write_text(worker_path.read_text() + "\n# pre-existing local edit\n")
        untracked_path = repo / "scratch.txt"
        untracked_path.write_text("pre-existing untracked content\n")

        before_status = _status(repo)
        before_head = _head(repo)
        before_worker = worker_path.read_text()
        before_untracked = untracked_path.read_text()

        with GitWorktree(repo, run_id="r-dirty-source") as path:
            assert (path / "jobs" / "worker.py").is_file()

        assert _status(repo) == before_status
        assert _head(repo) == before_head
        assert worker_path.read_text() == before_worker
        assert untracked_path.read_text() == before_untracked


def test_snapshot_source_matches_actual_head_and_status() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-snapshot")
        snapshot = wt.snapshot_source()
        assert snapshot.head == _head(repo)
        assert snapshot.status_porcelain == _status(repo)


def _file_digest_manifest(repo: Path) -> dict[str, str]:
    """sha256 of every file's content, path relative to repo root,
    excluding .git — a full working-copy content fingerprint, not just
    the one fixture file other tests happen to check."""
    import hashlib

    manifest: dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        manifest[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        capture_output=True,
        text=True,
    ).stdout


def _worktree_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout


def test_complete_working_copy_refs_and_worktree_registry_are_unchanged() -> None:
    """Stronger than the other protection tests: a full file-content
    digest (every file, not a hand-picked one), a full ref inventory
    (not just HEAD), and the worktree-list snapshot compared before
    creation and after cleanup — not merely "no error while checking
    one thing.\""""
    with real_fixture_repo() as repo:
        before_manifest = _file_digest_manifest(repo)
        before_refs = _refs(repo)
        before_worktree_list = _worktree_list(repo)

        with GitWorktree(repo, run_id="r-full-digest") as path:
            # Mutate freely inside the worktree — must have zero effect
            # on the source repo checked below.
            (path / "jobs" / "worker.py").write_text("mutated in worktree only\n")
            (path / "new_file.py").write_text("new in worktree only\n")

        assert _file_digest_manifest(repo) == before_manifest
        assert _refs(repo) == before_refs
        assert _worktree_list(repo) == before_worktree_list


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run_git_to_fail_remove_and_noop_prune(monkeypatch) -> None:
    """Simulate `git worktree remove` failing and `git worktree prune`
    silently not fixing it, so the stale registration genuinely
    persists — everything else (add, rev-parse, status, list) still
    calls the real git binary."""
    import codeagent.workspace as workspace_module

    real_run_git = workspace_module._run_git

    def fake_run_git(*args: str, check: bool = True):
        if "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        if "prune" in args:
            return _FakeCompletedProcess(returncode=0)  # "succeeds" but does nothing
        return real_run_git(*args, check=check)

    monkeypatch.setattr(workspace_module, "_run_git", fake_run_git)


def test_cleanup_surfaces_a_failure_when_remove_and_prune_cannot_recover(monkeypatch) -> None:
    _patch_run_git_to_fail_remove_and_noop_prune(monkeypatch)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-cleanup-fail")
        captured_path = None
        with pytest.raises(GitWorktreeCleanupError):
            with wt as path:
                captured_path = path

        # The temporary directory is still removed (finally path) even
        # though the git-level registration could not be cleaned up.
        assert not captured_path.exists()
        assert wt.cleanup_error is not None
        assert wt.path is None


def test_cleanup_failure_does_not_mask_an_exception_already_propagating(monkeypatch) -> None:
    _patch_run_git_to_fail_remove_and_noop_prune(monkeypatch)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-cleanup-fail-mask")
        with pytest.raises(RuntimeError, match="boom"):
            with wt as path:
                raise RuntimeError("boom")

        # The original exception won, not GitWorktreeCleanupError — but
        # the cleanup problem is still recorded, not silently dropped.
        assert wt.cleanup_error is not None
        assert not path.exists()


def test_registration_status_treats_a_failed_listing_as_unknown_not_absent(monkeypatch) -> None:
    """A `git worktree list` command that itself fails must never be
    read as "confirmed not registered" — that would let a genuinely
    stale registration go unreported as a cleanup success."""
    import codeagent.workspace as workspace_module

    real_run_git = workspace_module._run_git

    def fake_run_git(*args: str, check: bool = True):
        if "list" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated listing failure")
        return real_run_git(*args, check=check)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-status-unknown")
        monkeypatch.setattr(workspace_module, "_run_git", fake_run_git)
        status = wt._registration_status(Path("/does/not/matter"))
        assert status is None  # unknown, not False


def test_registration_status_parses_exact_records_not_substrings() -> None:
    """A path that is a textual prefix of another registered path must
    not produce a false positive from substring matching."""
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-status-exact")
        with wt as path:
            similar_but_different = Path(str(path) + "-similarly-named-but-different")
            assert wt._registration_status(similar_but_different) is False
            assert wt._registration_status(path) is True


def test_reentry_of_an_already_active_worktree_is_rejected() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-reentry")
        with wt:
            with pytest.raises(GitWorktreeError):
                wt.__enter__()
