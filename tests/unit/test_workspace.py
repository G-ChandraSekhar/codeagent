"""Milestone 1 slice B: GitWorktree lifecycle tests.

Uses a real, throwaway Git repository (tests/support/fixture_repo.py)
and real git subprocess calls — no mocking of git itself, since the
whole point is proving real worktree/cleanup/protection behavior.
"""

from __future__ import annotations

import shutil
import subprocess
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from codeagent import _git_safety
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


def _patch_run_git_to_fail_remove(monkeypatch) -> None:
    """Simulate `git worktree remove` failing so the stale registration
    genuinely persists — everything else (add, rev-parse, status, list)
    still calls the real git binary via the real, hardened `_run` seam.
    `dispose()` no longer has a `prune`/`rmtree` fallback to simulate
    around (ADR 0003 Amendment 2): once `remove` fails, disposal simply
    raises `GitWorktreeCleanupError`, so there is nothing left to
    recover from."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run

    def fake_run(repo_path, *args: str, input_text: str | None = None):
        if "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        return real_run(repo_path, *args, input_text=input_text)

    monkeypatch.setattr(workspace_module, "_run", fake_run)


def test_cleanup_surfaces_a_failure_when_remove_cannot_be_confirmed(monkeypatch) -> None:
    _patch_run_git_to_fail_remove(monkeypatch)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-cleanup-fail")
        captured_path = None
        with pytest.raises(GitWorktreeCleanupError):
            with wt as path:
                captured_path = path

        # No rmtree/prune fallback exists any more (ADR 0003 Amendment
        # 2): an unconfirmed `git worktree remove` is surfaced loudly,
        # and the worktree's own content is deliberately left exactly
        # as it was — force-deleting it here would be indistinguishable
        # from the forbidden rmtree fallback.
        assert captured_path.exists()
        assert wt.cleanup_error is not None
        assert wt.path is not None


def test_cleanup_failure_does_not_mask_an_exception_already_propagating(monkeypatch) -> None:
    _patch_run_git_to_fail_remove(monkeypatch)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-cleanup-fail-mask")
        with pytest.raises(RuntimeError, match="boom"):
            with wt as path:
                raise RuntimeError("boom")

        # The original exception won, not GitWorktreeCleanupError — but
        # the cleanup problem is still recorded, not silently dropped.
        assert wt.cleanup_error is not None
        assert path.exists()


def test_registration_status_treats_a_failed_listing_as_unknown_not_absent(monkeypatch) -> None:
    """A `git worktree list` command that itself fails must never be
    read as "confirmed not registered" — that would let a genuinely
    stale registration go unreported as a cleanup success."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run

    def fake_run(repo_path, *args: str, input_text: str | None = None):
        if "list" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated listing failure")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-status-unknown")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
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


# ---------------------------------------------------------------------------
# ADR 0006 workspace-integration slice: hardened worktree creation and
# source-status inspection. Real fixtures throughout; every refusal test
# is paired with a positive control proving the underlying mechanism
# (hook/filter) is actually live, so a refusal can't be mistaken for
# "nothing was configured to begin with."
# ---------------------------------------------------------------------------

import os

_IDENTITY = ["-c", "user.name=CodeAgent Test", "-c", "user.email=codeagent-test@example.invalid"]


def _git_raw(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Deliberately unhardened raw git call, used only to configure test
    fixtures (hostile hooks/filters/attributes) or to run a genuine,
    unprotected positive control — never to exercise GitWorktree itself."""
    return subprocess.run(
        ["git", "-C", str(repo), *_IDENTITY, *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _configure_hostile_post_checkout_hook(repo: Path, marker: Path) -> None:
    hooks_dir = repo / "hostile-hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho POST-CHECKOUT-RAN >> {marker}\n")
    hook.chmod(0o755)
    assert _git_raw(repo, "config", "core.hooksPath", str(hooks_dir)).returncode == 0


def test_worktree_creation_does_not_execute_a_configured_post_checkout_hook(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_post_checkout_hook(repo, marker)

        with GitWorktree(repo, run_id="r-hook") as path:
            assert (path / "jobs" / "worker.py").is_file()

        assert not marker.exists()


def test_positive_control_the_post_checkout_hook_fixture_really_fires() -> None:
    """Note: Git has no "pre-checkout" hook (confirmed against `git help
    hooks`); only `post-checkout` exists, so that is what's tested here
    and above. Proves the hook fixture is genuinely live: an unhardened,
    unprotected `git checkout` on the same repo really invokes it."""
    with real_fixture_repo() as repo:
        marker = Path(repo).parent / "positive-control-marker.log"
        _configure_hostile_post_checkout_hook(repo, marker)
        head = _head(repo)

        result = _git_raw(repo, "checkout", head, "--", "jobs/worker.py")

        assert result.returncode == 0
        assert marker.exists()
        assert "POST-CHECKOUT-RAN" in marker.read_text()


def _configure_hostile_filter(repo: Path, driver: str, marker: Path, subkey: str = "smudge") -> None:
    command = f"sh -c 'echo {subkey.upper()}-RAN:{driver} >> {marker}; cat'"
    assert _git_raw(repo, "config", f"filter.{driver}.{subkey}", command).returncode == 0


def _assign_filter_attribute(repo: Path, path: str, driver_or_state: str) -> None:
    attrs = repo / ".gitattributes"
    existing = attrs.read_text() if attrs.exists() else ""
    attrs.write_text(existing + f"{path} filter={driver_or_state}\n")
    assert _git_raw(repo, "add", ".gitattributes").returncode == 0
    assert _git_raw(repo, "commit", "-m", "add filter attribute").returncode == 0


def test_worktree_creation_refuses_an_active_smudge_filter(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")
        before_listing = _worktree_list(repo)

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-smudge-refuse"):
                pass

        assert not marker.exists()
        assert _worktree_list(repo) == before_listing


def test_worktree_creation_refuses_an_active_clean_filter(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="clean")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-clean-refuse"):
                pass

        assert not marker.exists()


def test_worktree_creation_refuses_an_active_process_filter(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="process")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-process-refuse"):
                pass

        assert not marker.exists()


def test_positive_control_hostile_smudge_filter_really_executes_when_unprotected() -> None:
    """Proves the filter fixture itself is live, so the refusal above is
    a real safety decision, not a coincidence of an inert fixture."""
    with real_fixture_repo() as repo:
        marker = Path(repo).parent / "positive-control-filter-marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")
        # git skips re-smudging a file that already matches the index by
        # stat/content; removing it first forces a genuine re-checkout.
        (repo / "jobs" / "worker.py").unlink()

        result = _git_raw(repo, "checkout", "--", "jobs/worker.py")

        assert result.returncode == 0
        assert marker.exists()
        assert "SMUDGE-RAN:hostile" in marker.read_text()


def test_worktree_creation_refuses_a_driver_named_unset(tmp_path) -> None:
    """ADR 0006 finding 16: check-attr reports the literal string
    "unset" both for a genuine negation and for an explicit assignment
    naming a driver called "unset". A live driver by that name must
    still cause refusal (not be treated as safe merely because the
    reported string matches the "safe" string)."""
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "unset", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "unset")

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-magic-unset"):
                pass

        assert not marker.exists()


def test_worktree_creation_refuses_a_driver_named_unspecified(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "unspecified", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "unspecified")

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-magic-unspecified"):
                pass

        assert not marker.exists()


def test_positive_control_driver_named_unset_really_executes_when_unprotected() -> None:
    with real_fixture_repo() as repo:
        marker = Path(repo).parent / "positive-control-unset-marker.log"
        _configure_hostile_filter(repo, "unset", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "unset")
        (repo / "jobs" / "worker.py").unlink()

        result = _git_raw(repo, "checkout", "--", "jobs/worker.py")

        assert result.returncode == 0
        assert marker.exists()


def test_worktree_creation_refuses_a_filter_assigned_via_info_attributes(tmp_path) -> None:
    """The active filter attribute need not come from a committed
    .gitattributes file at all — `.git/info/attributes` is a real, live
    input to attribute resolution (ADR 0006 finding 14)."""
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        (repo / ".git" / "info" / "attributes").write_text("jobs/worker.py filter=hostile\n")

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-info-attributes"):
                pass

        assert not marker.exists()


def test_worktree_creation_refuses_a_filter_driver_defined_only_at_global_scope(tmp_path) -> None:
    """Simulates the real-world "ambient Git LFS" shape: the filter
    *driver* is defined at global scope (as `git lfs install` or a CI
    runner image's system-wide LFS setup would do), while the
    repository itself only assigns the attribute. Isolated HOME/
    XDG_CONFIG_HOME, matching test_git_safety.py's discipline --
    GIT_CONFIG_NOSYSTEM is never used, so this exercises the same
    merged-config code path system-level configuration would."""
    home = tmp_path / "isolated-home"
    xdg_config_home = tmp_path / "isolated-xdg-config"
    home.mkdir()
    xdg_config_home.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg_config_home)}

    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        command = f"sh -c 'echo GLOBAL-DRIVER-RAN >> {marker}; cat'"
        assert (
            subprocess.run(
                ["git", "config", "--global", "filter.simulatedlfs.smudge", command],
                capture_output=True,
                text=True,
                env=env,
            ).returncode
            == 0
        )
        (repo / ".gitattributes").write_text("jobs/worker.py filter=simulatedlfs\n")
        assert _git_raw(repo, "add", ".gitattributes", env=env).returncode == 0
        assert _git_raw(repo, "commit", "-m", "assign simulated lfs filter", env=env).returncode == 0

        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-global-driver"):
                pass

        assert not marker.exists()


def test_worktree_creation_succeeds_when_ambient_filter_config_does_not_apply_to_any_tracked_path(
    tmp_path,
) -> None:
    """Negative control for the ambient-config tests above: a filter
    driver existing in global config, with no `.gitattributes`
    assignment pointing any tracked path at it, must not cause a
    refusal — proving the refusal above is caused by the attribute
    assignment, not merely by the driver's existence somewhere."""
    home = tmp_path / "isolated-home"
    xdg_config_home = tmp_path / "isolated-xdg-config"
    home.mkdir()
    xdg_config_home.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg_config_home)}

    with real_fixture_repo() as repo:
        assert (
            subprocess.run(
                ["git", "config", "--global", "filter.unused.smudge", "cat"],
                capture_output=True,
                text=True,
                env=env,
            ).returncode
            == 0
        )

        with GitWorktree(repo, run_id="r-unused-ambient-driver") as path:
            assert (path / "jobs" / "worker.py").is_file()


def test_worktree_creation_hostile_git_dir_does_not_redirect_the_worktree(tmp_path) -> None:
    """A hostile GIT_DIR/GIT_WORK_TREE inherited from the calling
    process's environment must never redirect worktree creation onto a
    different repository -- confirmed by pointing the decoy at a
    completely unrelated repository with a different HEAD."""
    with real_fixture_repo() as repo, real_fixture_repo() as decoy:
        assert _git_raw(decoy, "commit", "--allow-empty", "-m", "decoy-only commit").returncode == 0
        real_head = _head(repo)
        decoy_head = _head(decoy)
        assert real_head != decoy_head

        hostile_env = {
            **os.environ,
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(decoy),
            "GIT_NAMESPACE": "hostile-namespace",
        }
        old_environ = dict(os.environ)
        os.environ.clear()
        os.environ.update(hostile_env)
        try:
            with GitWorktree(repo, run_id="r-hostile-env") as path:
                result = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                )
                assert result.stdout.strip() == real_head
        finally:
            os.environ.clear()
            os.environ.update(old_environ)


def test_worktree_creation_cleanup_after_refusal_leaves_no_registration_or_directory(
    tmp_path, monkeypatch
) -> None:
    """Captures the exact worktree path __enter__ generates internally
    (never otherwise exposed after a failure) via a controlled seam --
    subclassing TemporaryDirectory to record the path it creates --
    then proves both the exact directory and the exact registration
    are gone, rather than inferring absence from a coarser signal."""
    import codeagent.workspace as workspace_module

    real_tempdir_cls = workspace_module.tempfile.TemporaryDirectory
    captured: dict[str, Path] = {}

    class RecordingTempDir(real_tempdir_cls):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            captured["worktree_path"] = Path(self.name) / "worktree"

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", RecordingTempDir)

    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")
        before_listing = _worktree_list(repo)

        wt = GitWorktree(repo, run_id="r-refusal-cleanup")
        with pytest.raises(GitWorktreeError):
            with wt:
                pass  # pragma: no cover -- refusal must happen first

        exact_path = captured["worktree_path"]
        assert wt.path is None
        assert wt.cleanup_error is None  # confirmed removed, not merely attempted
        assert _worktree_list(repo) == before_listing
        assert not exact_path.exists()
        assert wt._registration_status(exact_path) is False


def test_enter_time_cleanup_raises_when_remove_reports_nonzero(monkeypatch) -> None:
    """Nonzero exact-remove result: the registration cannot be
    confirmed removed, so a sanitized GitWorktreeCleanupError must be
    raised (not merely recorded), chaining the original refusal as its
    cause, and `prune` must never be invoked on this path."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    calls: list[tuple] = []

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        if "worktree" in args and "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        marker = repo.parent / "marker-remove-fail.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        wt = GitWorktree(repo, run_id="r-remove-fail")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        with pytest.raises(GitWorktreeCleanupError) as excinfo:
            with wt:
                pass  # pragma: no cover

        # Original-failure preservation: the exact refusal instance
        # survives as the explicit cause, not merely "some exception."
        assert isinstance(excinfo.value.__cause__, GitWorktreeError)
        assert "active" in str(excinfo.value.__cause__) or "ambiguous" in str(
            excinfo.value.__cause__
        )
        assert wt.cleanup_error is excinfo.value
        # Sanitization: no repository or worktree path in either message.
        assert str(repo) not in str(excinfo.value)
        assert str(repo) not in str(excinfo.value.__cause__)
        # No repository-wide sweep on this path.
        assert not any("prune" in call for call in calls)


def test_enter_time_cleanup_raises_when_registration_status_cannot_be_determined(
    monkeypatch,
) -> None:
    """Unavailable registration observation: `worktree remove` itself
    succeeds, but the subsequent confirmation listing fails, so the
    outcome is unknown -- never treated as confirmed removal."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    calls: list[tuple] = []

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        if "worktree" in args and "list" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated listing failure")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        marker = repo.parent / "marker-list-fail.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        wt = GitWorktree(repo, run_id="r-list-fail")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        with pytest.raises(GitWorktreeCleanupError) as excinfo:
            with wt:
                pass  # pragma: no cover

        assert isinstance(excinfo.value.__cause__, GitWorktreeError)
        assert str(repo) not in str(excinfo.value)
        assert not any("prune" in call for call in calls)


def test_enter_time_cleanup_raises_when_tempdir_cannot_be_removed(monkeypatch, tmp_path) -> None:
    """Temporary-directory cleanup failure (e.g. a permissions or
    file-locking error) must also be surfaced categorically, without
    exposing the filesystem path the raw OSError would have named.

    Scoped to `codeagent.workspace`'s own `tempfile.TemporaryDirectory`
    reference only (a subclass swapped in via that module's attribute)
    so `real_fixture_repo()`'s unrelated, separately-imported use of
    the real `TemporaryDirectory` is never affected."""
    import codeagent.workspace as workspace_module

    real_tempdir_cls = workspace_module.tempfile.TemporaryDirectory
    created_names: list[str] = []

    class FailingCleanupTempDir(real_tempdir_cls):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            created_names.append(self.name)

        def cleanup(self) -> None:
            raise OSError(f"simulated failure removing {self.name}")

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", FailingCleanupTempDir)

    try:
        with real_fixture_repo() as repo:
            marker = tmp_path / "marker.log"
            _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
            _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

            wt = GitWorktree(repo, run_id="r-tempdir-fail")
            with pytest.raises(GitWorktreeCleanupError) as excinfo:
                with wt:
                    pass  # pragma: no cover

            assert isinstance(excinfo.value.__cause__, GitWorktreeError)
            assert "temporary directory" in str(excinfo.value)
            # Sanitized: the raw OSError's message (which names the
            # real path) must not have leaked into the raised message.
            assert str(repo) not in str(excinfo.value)
            assert wt.cleanup_error is excinfo.value
    finally:
        # This test deliberately made the real tempdir unremovable via
        # the patched cleanup(); undo the patch and remove it for real
        # so the test's own resources don't leak onto disk.
        monkeypatch.undo()
        for name in created_names:
            shutil.rmtree(name, ignore_errors=True)


def test_evaluate_filter_safety_infrastructure_failure_refuses_rather_than_proceeds(
    monkeypatch,
) -> None:
    """If the filter-safety evaluation itself cannot be completed (a
    categorical GitSafetyError from the shared helper — malformed
    output, a bound exceeded, a timeout), that must refuse the whole
    worktree, never be treated as "safe by default.\""""
    import codeagent.workspace as workspace_module

    def fake_evaluate(repo_path):
        raise _git_safety.GitSafetyError(
            _git_safety.GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED, "simulated malformed output"
        )

    monkeypatch.setattr(
        workspace_module._git_safety, "evaluate_tracked_filter_safety", fake_evaluate
    )

    with real_fixture_repo() as repo:
        with pytest.raises(GitWorktreeError):
            with GitWorktree(repo, run_id="r-eval-failure"):
                pass


def test_worktree_add_infrastructure_timeout_raises_sanitized_error(monkeypatch, tmp_path) -> None:
    """A timeout from the underlying git-safety seam must surface as a
    sanitized GitWorktreeError -- no raw stderr, argv, or repository
    path in the message."""
    import codeagent.workspace as workspace_module

    real_run_git = workspace_module._git_safety.run_git

    def fake_run_git(repo_path, *args, input_text=None):
        if "worktree" in args and "add" in args:
            raise _git_safety.GitSafetyError(
                _git_safety.GitSafetyFailure.GIT_COMMAND_TIMEOUT,
                "a git command did not finish within its time limit",
            )
        return real_run_git(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-timeout")
        monkeypatch.setattr(workspace_module._git_safety, "run_git", fake_run_git)
        with pytest.raises(GitWorktreeError) as excinfo:
            with wt:
                pass

        message = str(excinfo.value)
        assert str(repo) not in message
        assert "stderr" not in message.lower()


def test_error_messages_never_contain_the_source_repo_path(tmp_path) -> None:
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        with pytest.raises(GitWorktreeError) as excinfo:
            with GitWorktree(repo, run_id="r-message-check"):
                pass

        assert str(repo) not in str(excinfo.value)
        assert "worker.py" not in str(excinfo.value)


def test_snapshot_source_status_does_not_execute_a_configured_filter(tmp_path) -> None:
    """ADR 0006 section 1/4's secondary control: `git status` itself
    must not trigger a clean filter, even though snapshot_source() is
    read-only and predates worktree creation."""
    with real_fixture_repo() as repo:
        marker = tmp_path / "marker.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="clean")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")
        # Touch the working-tree file so status performs a real content
        # comparison rather than trusting a matching mtime.
        worker = repo / "jobs" / "worker.py"
        worker.write_text(worker.read_text())

        wt = GitWorktree(repo, run_id="r-status-filter")
        wt.snapshot_source()

        assert not marker.exists()


def test_snapshot_source_status_does_not_run_fsmonitor() -> None:
    """core.fsmonitor must be suppressed with an explicit boolean, not
    merely by clearing GIT_* variables (an empty-string override does
    not suppress it -- ADR 0006 section 5)."""
    with real_fixture_repo() as repo:
        marker = repo.parent / "fsmonitor-marker.log"
        assert (
            _git_raw(repo, "config", "core.fsmonitor", f"sh -c 'echo FSMONITOR-RAN >> {marker}'")
            .returncode
            == 0
        )

        wt = GitWorktree(repo, run_id="r-fsmonitor")
        wt.snapshot_source()

        assert not marker.exists()


# ---------------------------------------------------------------------------
# Lazy-fetch refusal (ADR 0006 section 6): worktree creation over a
# genuinely partial clone must never silently fetch a missing object
# from the network, and must fail closed instead.
# ---------------------------------------------------------------------------


def _free_tcp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _count_objects(repo: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(repo), "count-objects", "-v"], capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("in-pack:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError("git count-objects did not report in-pack")


@pytest.fixture
def git_daemon_origin(tmp_path):
    """A real `git://` daemon serving a small two-commit repository,
    with partial-clone filtering enabled — the same real-transport
    mechanism ADR 0006 finding 15 used to prove GIT_NO_LAZY_FETCH's
    effect, applied here at the workspace-integration level."""
    base = tmp_path / "daemon-base"
    origin = base / "origin"
    origin.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(origin)], check=True)
    _git_raw(origin, "commit", "--allow-empty", "-m", "first")
    (origin / "a.txt").write_text("content one\n")
    _git_raw(origin, "add", "a.txt")
    _git_raw(origin, "commit", "-m", "add a.txt")
    assert _git_raw(origin, "config", "uploadpack.allowFilter", "true").returncode == 0
    assert _git_raw(origin, "config", "uploadpack.allowAnySHA1InWant", "true").returncode == 0

    port = _free_tcp_port()
    daemon = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            f"--base-path={base}",
            "--export-all",
            f"--port={port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import time

        time.sleep(0.5)  # let the daemon bind before the first clone attempt
        yield origin, port
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)


def test_worktree_creation_over_partial_clone_fails_closed_without_fetching(
    git_daemon_origin, tmp_path
) -> None:
    _origin, port = git_daemon_origin
    partial_clone = tmp_path / "partial-clone"
    clone_result = subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-local",
            "--filter=blob:none",
            "--no-checkout",
            f"git://127.0.0.1:{port}/origin",
            str(partial_clone),
        ],
        capture_output=True,
        text=True,
    )
    assert clone_result.returncode == 0, clone_result.stderr
    _git_raw(partial_clone, "read-tree", "HEAD")
    objects_before = _count_objects(partial_clone)

    with pytest.raises(GitWorktreeError):
        with GitWorktree(partial_clone, run_id="r-lazy-fetch-refuse"):
            pass

    # Fail closed: no object was fetched as a side effect of the
    # refused attempt (GIT_NO_LAZY_FETCH=1 + --no-lazy-fetch prevented
    # the network round-trip, rather than merely erroring afterward).
    assert _count_objects(partial_clone) == objects_before


def test_positive_control_the_same_partial_clone_really_lazily_fetches_by_default(
    git_daemon_origin, tmp_path
) -> None:
    """Proves the daemon/partial-clone fixture is genuinely capable of
    lazy-fetching, so the refusal above is a real safety decision (the
    daemon exists and would serve the object), not an artifact of no
    network being reachable at all."""
    _origin, port = git_daemon_origin
    partial_clone = tmp_path / "partial-clone-unprotected"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-local",
            "--filter=blob:none",
            "--no-checkout",
            f"git://127.0.0.1:{port}/origin",
            str(partial_clone),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    _git_raw(partial_clone, "read-tree", "HEAD")
    objects_before = _count_objects(partial_clone)

    # Default environment: no GIT_NO_LAZY_FETCH, no --no-lazy-fetch.
    result = subprocess.run(
        ["git", "-C", str(partial_clone), "checkout", "--", "a.txt"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert (partial_clone / "a.txt").read_text() == "content one\n"
    assert _count_objects(partial_clone) > objects_before


# ---------------------------------------------------------------------------
# `git worktree add`'s outcome is ambiguous: a nonzero result, an
# infrastructure error, or an interruption right after Git actually
# created the registration must all be treated as potentially
# mutating, never as proof nothing was created.
# ---------------------------------------------------------------------------


def _emergency_real_cleanup(repo: Path, worktree_path: Path) -> None:
    """Test-only teardown: force-remove a possibly still-dangling
    worktree registration using the real git binary directly (bypasses
    any monkeypatched seam). `prune` here is test cleanup, not the
    production path under test, so it is not the thing being asserted
    against."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True
    )
    shutil.rmtree(worktree_path, ignore_errors=True)


def test_worktree_add_synthetic_nonzero_after_real_success_is_fully_cleaned_up(
    monkeypatch,
) -> None:
    """Even when `git worktree add` genuinely succeeds, this module
    must not trust a nonzero *report* as proof nothing was created --
    it independently confirms removal by observation."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    calls: list[tuple] = []
    captured: dict[str, Path] = {}

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        if "worktree" in args and "add" in args:
            real_result = real_run(repo_path, *args, input_text=input_text)
            assert real_result.returncode == 0  # sanity: it really did succeed
            captured["worktree_path"] = Path(args[args.index("--detach") + 1])
            return _FakeCompletedProcess(returncode=1, stderr="synthetic ambiguous failure")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-synthetic-nonzero")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        with pytest.raises(GitWorktreeError):
            with wt:
                pass  # pragma: no cover

        exact_path = captured["worktree_path"]
        assert not any("prune" in call for call in calls)
        monkeypatch.undo()
        assert wt._registration_status(exact_path) is False
        assert not exact_path.exists()
        _emergency_real_cleanup(repo, exact_path)


def test_worktree_add_synthetic_infra_error_after_real_success_is_fully_cleaned_up(
    monkeypatch,
) -> None:
    """Same as above, but the seam raises an infrastructure error
    (as `_run` does when `_git_safety.run_git` fails) instead of
    returning a nonzero result."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    calls: list[tuple] = []
    captured: dict[str, Path] = {}

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        if "worktree" in args and "add" in args:
            real_result = real_run(repo_path, *args, input_text=input_text)
            assert real_result.returncode == 0
            captured["worktree_path"] = Path(args[args.index("--detach") + 1])
            raise GitWorktreeError("simulated infrastructure error reporting add's outcome")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-synthetic-infra-error")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        with pytest.raises(GitWorktreeError):
            with wt:
                pass  # pragma: no cover

        exact_path = captured["worktree_path"]
        assert not any("prune" in call for call in calls)
        monkeypatch.undo()
        assert wt._registration_status(exact_path) is False
        assert not exact_path.exists()
        _emergency_real_cleanup(repo, exact_path)


def test_ordinary_add_failure_without_registration_preserves_original_error(monkeypatch) -> None:
    """A genuine add failure that creates no registration at all (an
    invalid target commit) must propagate its own sanitized message
    unchanged -- not a GitWorktreeCleanupError -- once cleanup confirms
    there was nothing to clean up."""
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-ordinary-add-fail")
        monkeypatch.setattr(wt, "_rev_parse", lambda ref: "0" * 40)  # a well-formed, nonexistent commit

        with pytest.raises(GitWorktreeError) as excinfo:
            with wt:
                pass  # pragma: no cover

        assert not isinstance(excinfo.value, GitWorktreeCleanupError)
        assert "git worktree add failed" in str(excinfo.value)
        assert wt.cleanup_error is None


def test_ambiguous_add_failure_with_unconfirmed_cleanup_raises_cleanup_error(monkeypatch) -> None:
    """Add's outcome is ambiguous (a synthetic nonzero after a real
    success) *and* the subsequent cleanup cannot confirm removal --
    GitWorktreeCleanupError must be raised, chaining the original add
    failure as its cause, with no `prune` call anywhere on this path."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    calls: list[tuple] = []
    captured: dict[str, Path] = {}

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        if "worktree" in args and "add" in args:
            real_run(repo_path, *args, input_text=input_text)
            captured["worktree_path"] = Path(args[args.index("--detach") + 1])
            return _FakeCompletedProcess(returncode=1, stderr="synthetic ambiguous add failure")
        if "worktree" in args and "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        if "worktree" in args and "list" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated listing failure")
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-ambiguous-unconfirmed")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        with pytest.raises(GitWorktreeCleanupError) as excinfo:
            with wt:
                pass  # pragma: no cover

        assert isinstance(excinfo.value.__cause__, GitWorktreeError)
        assert "git worktree add failed" in str(excinfo.value.__cause__)
        assert wt.cleanup_error is excinfo.value
        assert not any("prune" in call for call in calls)

        exact_path = captured["worktree_path"]
        monkeypatch.undo()
        _emergency_real_cleanup(repo, exact_path)
        assert wt._registration_status(exact_path) is False


def test_simultaneous_registration_and_tempdir_cleanup_failures_are_both_represented(
    monkeypatch,
) -> None:
    """Neither cleanup failure is discarded in favor of the other:
    both are represented in the single raised error."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run
    real_tempdir_cls = workspace_module.tempfile.TemporaryDirectory
    created_names: list[str] = []

    class FailingCleanupTempDir(real_tempdir_cls):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            created_names.append(self.name)

        def cleanup(self) -> None:
            raise OSError(f"simulated tempdir cleanup failure removing {self.name}")

    def fake_run(repo_path, *args, input_text=None):
        if "worktree" in args and "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        if "worktree" in args and "list" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated listing failure")
        return real_run(repo_path, *args, input_text=input_text)

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", FailingCleanupTempDir)

    try:
        with real_fixture_repo() as repo:
            marker = repo.parent / "marker-both-fail.log"
            _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
            _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

            wt = GitWorktree(repo, run_id="r-both-cleanup-fail")
            monkeypatch.setattr(workspace_module, "_run", fake_run)
            with pytest.raises(GitWorktreeCleanupError) as excinfo:
                with wt:
                    pass  # pragma: no cover

            message = str(excinfo.value)
            assert "registration" in message
            assert "temporary directory" in message
            assert isinstance(excinfo.value.__cause__, GitWorktreeError)
    finally:
        monkeypatch.undo()
        for name in created_names:
            shutil.rmtree(name, ignore_errors=True)


def _full_traceback_text(exc: BaseException) -> str:
    """The complete rendered traceback, including any chained
    __cause__/__context__ Python would print -- a stricter check than
    `str(exc)`, which only covers the top-level exception's own
    message and would miss a leak sitting in a chained exception."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def test_tempdir_construction_failure_does_not_leak_a_fake_secret_path(monkeypatch) -> None:
    import codeagent.workspace as workspace_module

    def fake_temporary_directory(*args, **kwargs):
        raise OSError("simulated failure: /fake/secret/construction/path")

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", fake_temporary_directory)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-tempdir-construct-fail")
        with pytest.raises(GitWorktreeError) as excinfo:
            wt.__enter__()

        assert "/fake/secret/construction/path" not in str(excinfo.value)
        # `from None`: the raw OSError is fully detached, not merely
        # summarized -- neither __cause__ nor a printed traceback can
        # surface it.
        assert excinfo.value.__cause__ is None
        assert "/fake/secret/construction/path" not in _full_traceback_text(excinfo.value)
        assert wt.path is None


def test_add_failure_tempdir_cleanup_error_does_not_leak_a_fake_secret_path(monkeypatch) -> None:
    """Combines an ordinary add failure (bogus commit, no registration
    created) with a tempdir-cleanup failure carrying an obviously fake
    secret path, proving both the chaining and the sanitization at
    once."""
    import codeagent.workspace as workspace_module

    real_tempdir_cls = workspace_module.tempfile.TemporaryDirectory
    created_names: list[str] = []

    class FailingCleanupTempDir(real_tempdir_cls):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            created_names.append(self.name)

        def cleanup(self) -> None:
            raise OSError("simulated failure: /fake/secret/cleanup/path")

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", FailingCleanupTempDir)

    try:
        with real_fixture_repo() as repo:
            wt = GitWorktree(repo, run_id="r-fake-secret-cleanup")
            monkeypatch.setattr(wt, "_rev_parse", lambda ref: "0" * 40)
            with pytest.raises(GitWorktreeCleanupError) as excinfo:
                with wt:
                    pass  # pragma: no cover

            assert "/fake/secret/cleanup/path" not in str(excinfo.value)
            assert isinstance(excinfo.value.__cause__, GitWorktreeError)
            assert "git worktree add failed" in str(excinfo.value.__cause__)
    finally:
        monkeypatch.undo()
        for name in created_names:
            shutil.rmtree(name, ignore_errors=True)


def test_source_path_resolution_failure_does_not_leak_a_fake_secret_path(monkeypatch) -> None:
    def fake_resolve(self, *args, **kwargs):
        raise RuntimeError("Symlink loop from /fake/secret/resolve/path")

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(GitWorktreeError) as excinfo:
        GitWorktree("/some/path", run_id="r-resolve-fail")

    assert "/fake/secret/resolve/path" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert "/fake/secret/resolve/path" not in _full_traceback_text(excinfo.value)


def test_registration_status_path_resolution_failure_during_cleanup(monkeypatch) -> None:
    """Injects a resolution failure specifically inside
    `_registration_status()`'s own `Path(path).resolve()` call during
    enter-time cleanup (not during __init__), and proves all of:
    (a) registration cleanup becomes unconfirmed even though the real
        `git worktree remove` genuinely succeeded;
    (b) the independent tempdir-cleanup step still runs and succeeds;
    (c) GitWorktreeCleanupError is raised with the original operational
        failure preserved as its __cause__;
    (d) the fake path never appears in the complete rendered traceback;
    (e) no `prune` call occurs anywhere on this path.
    """
    import codeagent.workspace as workspace_module

    real_resolve = Path.resolve
    real_run = workspace_module._run
    real_tempdir_cls = workspace_module.tempfile.TemporaryDirectory
    captured: dict[str, Path] = {}
    calls: list[tuple] = []

    class RecordingTempDir(real_tempdir_cls):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            captured["worktree_path"] = Path(self.name) / "worktree"

    def fake_resolve(self, *args, **kwargs):
        if "codeagent-worktree-" in str(self):
            raise RuntimeError("Symlink loop from /fake/secret/registration/path")
        return real_resolve(self, *args, **kwargs)

    def fake_run(repo_path, *args, input_text=None):
        calls.append(args)
        return real_run(repo_path, *args, input_text=input_text)

    monkeypatch.setattr(workspace_module.tempfile, "TemporaryDirectory", RecordingTempDir)

    with real_fixture_repo() as repo:
        marker = repo.parent / "marker-registration-resolve-fail.log"
        _configure_hostile_filter(repo, "hostile", marker, subkey="smudge")
        _assign_filter_attribute(repo, "jobs/worker.py", "hostile")

        wt = GitWorktree(repo, run_id="r-registration-resolve-fail")
        monkeypatch.setattr(workspace_module, "_run", fake_run)
        monkeypatch.setattr(Path, "resolve", fake_resolve)
        with pytest.raises(GitWorktreeCleanupError) as excinfo:
            with wt:
                pass  # pragma: no cover

        exact_path = captured["worktree_path"]

        # (a) registration cleanup unconfirmed, despite real removal
        # having genuinely succeeded.
        assert "could not be confirmed removed" in str(excinfo.value)

        # (b) the independent tempdir-cleanup step still ran and
        # actually removed the directory (unaffected by the patched
        # resolve(), since it never calls Path.resolve()).
        assert not exact_path.exists()

        # (c) original operational failure preserved as cause.
        assert isinstance(excinfo.value.__cause__, GitWorktreeError)
        assert "active" in str(excinfo.value.__cause__) or "ambiguous" in str(
            excinfo.value.__cause__
        )
        assert wt.cleanup_error is excinfo.value

        # (d) the fake path never surfaces, including via chaining.
        assert "/fake/secret/registration/path" not in _full_traceback_text(excinfo.value)

        # (e) no repository-wide sweep on this path.
        assert not any("prune" in call for call in calls)

        # Independent confirmation, via the real git binary with the
        # patches undone, that the registration genuinely was removed
        # despite our own code (correctly) reporting it as unconfirmed.
        monkeypatch.undo()
        real_status = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        assert str(exact_path) not in real_status


# ---------------------------------------------------------------------------
# Milestone 2 slice 2B-2 (ADR 0003 Amendment 2): initial_commit,
# entry_gate, dispose/preserve, and the tri-state __exit__ dispatch.
# ---------------------------------------------------------------------------


def test_initial_commit_unavailable_before_enter() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-initial-commit-early")
        with pytest.raises(GitWorktreeError):
            _ = wt.initial_commit


def test_initial_commit_is_the_pinned_starting_sha() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-initial-commit")
        with wt as path:
            expected = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert wt.initial_commit == expected
            # rev-parse inside the worktree itself agrees.
            observed = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert wt.initial_commit == observed


def test_entry_gate_accepts_a_clean_worktree_at_the_expected_commit() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-ok")
        with wt as path:
            wt.entry_gate(wt.initial_commit)  # must not raise


def test_entry_gate_rejects_wrong_expected_commit() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-wrong-head")
        with wt as path:
            with pytest.raises(GitWorktreeError):
                wt.entry_gate("0" * 40)


def test_entry_gate_rejects_dirty_working_tree() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-dirty-tree")
        with wt as path:
            tracked = next(path.rglob("*.py"))
            tracked.write_text(tracked.read_text() + "\n# dirty\n")
            with pytest.raises(GitWorktreeError):
                wt.entry_gate(wt.initial_commit)


def test_entry_gate_rejects_dirty_staged_index() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-dirty-index")
        with wt as path:
            tracked = next(path.rglob("*.py"))
            tracked.write_text(tracked.read_text() + "\n# staged\n")
            subprocess.run(
                ["git", "-C", str(path), "add", "-A"], check=True, capture_output=True
            )
            with pytest.raises(GitWorktreeError):
                wt.entry_gate(wt.initial_commit)


def test_entry_gate_rejects_untracked_paths() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-untracked")
        with wt as path:
            (path / "untracked_new_file.txt").write_text("surprise\n")
            with pytest.raises(GitWorktreeError):
                wt.entry_gate(wt.initial_commit)


def test_entry_gate_requires_an_active_worktree() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-gate-inactive")
        with pytest.raises(GitWorktreeError):
            wt.entry_gate("0" * 40)


def test_dispose_removes_registration_and_directory() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-ok")
        with wt as path:
            wt.dispose()
            assert not path.exists()
            listing = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert str(path) not in listing
            assert wt.path is None


def test_dispose_is_idempotent() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-twice")
        with wt as path:
            wt.dispose()
            wt.dispose()  # must not raise, must not attempt another git call


def test_dispose_accepts_confirmed_absence_despite_reported_remove_failure(monkeypatch) -> None:
    """Correction pass (defect 5): `git worktree remove`'s own reported
    status must not decide success — only the independent final
    observation does. The real removal is allowed to actually happen;
    only the *reported* result is fabricated as a failure."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run

    def fake_run(repo_path, *args: str, input_text: str | None = None):
        result = real_run(repo_path, *args, input_text=input_text)
        if "remove" in args:
            return _FakeCompletedProcess(returncode=1, stderr="simulated remove failure")
        return result

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-despite-reported-failure")
        with wt as path:
            monkeypatch.setattr(workspace_module, "_run", fake_run)
            wt.dispose()  # must NOT raise: final observation confirms exact absence
            assert not path.exists()
            assert wt.path is None
            listing = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert str(path) not in listing


def test_dispose_raises_when_absence_is_genuinely_unconfirmed_despite_reported_success(
    monkeypatch,
) -> None:
    """The converse of the fix above: a command that reports *success*
    is equally untrusted — if the final observation cannot confirm
    absence, dispose() still raises."""
    import codeagent.workspace as workspace_module

    real_run = workspace_module._run

    def fake_run(repo_path, *args: str, input_text: str | None = None):
        if "remove" in args:
            return _FakeCompletedProcess(returncode=0)  # claims success, does nothing real
        return real_run(repo_path, *args, input_text=input_text)

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-false-success")
        with wt as path:
            monkeypatch.setattr(workspace_module, "_run", fake_run)
            with pytest.raises(GitWorktreeCleanupError):
                wt.dispose()
            assert wt.path is not None
            assert wt._disposition == "active"
            # Undo the fabricated-success patch and dispose for real
            # before the `with` block's own __exit__ runs, so __exit__
            # sees an already-disposed instance and no-ops cleanly
            # rather than repeating the same fabricated failure
            # uncaught.
            monkeypatch.undo()
            wt.dispose()
            assert wt.path is None
            assert wt._disposition == "disposed"


def test_dispose_retry_recovers_after_tempdir_cleanup_failure(monkeypatch) -> None:
    """Correction pass (defect 5): a tempdir-cleanup failure must not
    permanently strand the instance — self._tempdir is left intact so a
    later retry can succeed once the transient problem clears, and the
    already-confirmed-gone worktree portion is not redone destructively."""
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-tempdir-retry")
        with wt as path:
            call_count = {"n": 0}
            real_cleanup = wt._tempdir.cleanup

            def flaky_cleanup():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise PermissionError("simulated transient failure")
                return real_cleanup()

            monkeypatch.setattr(wt._tempdir, "cleanup", flaky_cleanup)

            with pytest.raises(GitWorktreeCleanupError):
                wt.dispose()
            # The worktree itself is already confirmed gone; only the
            # tempdir wrapper cleanup failed, and disposition/path stay
            # "active" so a retry is possible rather than stuck forever.
            assert not path.exists()
            assert wt.path is not None
            assert wt._disposition == "active"

            wt.dispose()  # retry, now that the transient failure clears
            assert wt.path is None
            assert wt._disposition == "disposed"
            assert call_count["n"] == 2


def test_exit_after_dispose_is_a_noop(monkeypatch) -> None:
    """A direct dispose() followed by the with-block's own __exit__
    must not attempt a second removal (which would fail against an
    already-gone registration and incorrectly surface as a cleanup
    error)."""
    called_remove = False

    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-dispose-then-exit")
        with wt as path:
            wt.dispose()

            import codeagent.workspace as workspace_module

            real_run = workspace_module._run

            def fake_run(repo_path, *args: str, input_text: str | None = None):
                nonlocal called_remove
                if "remove" in args:
                    called_remove = True
                return real_run(repo_path, *args, input_text=input_text)

            monkeypatch.setattr(workspace_module, "_run", fake_run)

        assert called_remove is False


def test_preserve_then_exit_leaves_worktree_intact() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-preserve")
        with wt as path:
            wt.preserve()
        # __exit__ has now run and must have done nothing.
        assert path.exists()
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert str(path) in listing
        assert wt.path is not None

    # Clean up what the test itself intentionally preserved, so this
    # test doesn't leak a real worktree registration/tempdir.
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
        capture_output=True,
    )


def test_preserve_requires_active_state() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-preserve-twice")
        with wt as path:
            wt.preserve()
            with pytest.raises(GitWorktreeError):
                wt.preserve()
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
            capture_output=True,
        )


def test_preserve_after_dispose_is_rejected() -> None:
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-preserve-after-dispose")
        with wt as path:
            wt.dispose()
            with pytest.raises(GitWorktreeError):
                wt.preserve()


def test_direct_context_manager_use_disposes_exactly() -> None:
    """A caller that never touches preserve()/dispose() at all (the
    existing, pre-2B-2 usage pattern) still gets exact disposal via
    __exit__ alone."""
    with real_fixture_repo() as repo:
        captured_path = None
        with GitWorktree(repo, run_id="r-direct-use") as path:
            captured_path = path
        assert not captured_path.exists()
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert str(captured_path) not in listing


def test_exceptional_exit_before_teardown_still_disposes_exactly() -> None:
    """An exception raised inside the with-block, before any explicit
    dispose()/preserve() call, must still result in exact disposal via
    __exit__'s active-state fallback — never a leaked worktree."""
    captured_path = None
    with real_fixture_repo() as repo:
        wt = GitWorktree(repo, run_id="r-exceptional-exit")
        with pytest.raises(RuntimeError, match="boom"):
            with wt as path:
                captured_path = path
                raise RuntimeError("boom")
        assert not captured_path.exists()
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert str(captured_path) not in listing


def test_no_rmtree_or_prune_reference_exists_in_workspace_module() -> None:
    """Static AST-level proof that no `shutil` import, no `rmtree` call,
    and no `"prune"` string literal remains anywhere in workspace.py's
    actual code (docstrings are deliberately excluded — this module's
    own docstrings now describe the *absence* of both, by name, which
    would otherwise make a bare substring check false-positive) — a
    regression guard against either fallback silently reappearing."""
    import ast
    import inspect

    import codeagent.workspace as workspace_module

    source = inspect.getsource(workspace_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "shutil", "workspace.py must not import shutil"
        if isinstance(node, ast.Attribute) and node.attr == "rmtree":
            raise AssertionError("workspace.py must not call any *.rmtree(...)")
        if isinstance(node, ast.Constant) and node.value == "prune":
            raise AssertionError("workspace.py must not pass a 'prune' argument to git")
