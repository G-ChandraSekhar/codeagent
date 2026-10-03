"""ADR 0004 Amendment 11: the real `LifecycleWorktreePublisher` driven by a
real `GitWorktree`, end to end, against a real `prepare_lifecycle()` lease.

This is an optional, *unwired* producer seam: no production composition
path supplies a worktree publisher. The SIGKILL tests prove the honest
current outcome of a crash -- fail-closed blocking of new-run admission
(`RECONCILIATION_BLOCKED`), not recovery. Worktree reconciliation and
removal do not exist yet.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
from pathlib import Path

import pytest

from codeagent import lifecycle_store as ls
from codeagent import worktree_lifecycle as wl
from codeagent.workspace import GitWorktree


def _run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path, *, object_format: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init = ["git", "init", "-q"] + ([f"--object-format={object_format}"] if object_format else []) + [str(repo)]
    result = subprocess.run(init, capture_output=True, text=True)
    if result.returncode != 0:
        if object_format:
            pytest.skip(f"installed git does not support --object-format={object_format}")
        raise AssertionError("git init failed")
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    return repo


def _head(path: Path) -> str:
    return _run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def _is_registered(repo: Path, path: Path) -> bool:
    out = _run("git", "-C", str(repo), "worktree", "list", "--porcelain", "-z").stdout
    registered = {
        os.path.realpath(t[len("worktree ") :]) for t in out.split("\0") if t.startswith("worktree ")
    }
    return os.path.realpath(path) in registered


class _ReloadingPublisher:
    """Delegates to the real publisher and, after each confirmed publish,
    records a fresh authoritative re-read of the durable projection."""

    def __init__(self, real, writer):
        self._real = real
        self._writer = writer
        self.durable: list[wl.WorktreeTransition] = []

    @property
    def repo_key(self):
        return self._real.repo_key

    @property
    def lifecycle_id(self):
        return self._real.lifecycle_id

    @property
    def state_root_id(self):
        return self._real.state_root_id

    def publish(self, transition):
        self._real.publish(transition)
        self.durable.append(self._writer.refresh().worktree)


@pytest.mark.parametrize("object_format", [None, "sha256"])
def test_real_publisher_full_forward_cycle(tmp_path, monkeypatch, object_format):
    repo = _make_repo(tmp_path, object_format=object_format)
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state-root"))
    head = _head(repo)
    assert len(head) == (64 if object_format == "sha256" else 40)

    lease = ls.prepare_lifecycle(str(repo), run_id="wt-e2e")
    try:
        writer, current = lease.open_projection_writer()
        bundle = ls.create_shared_lifecycle_publishers(writer, current)
        publisher = _ReloadingPublisher(bundle.worktree_publisher, writer)
        with lease.state_root.reserve_worktree_leaf(lease.repo_key, lease.lifecycle_id) as reservation:
            worktree = GitWorktree(repo, run_id="wt-e2e", reservation=reservation, worktree_publisher=publisher)
            with worktree as path:
                assert _is_registered(repo, path)
                assert _head(path) == head
            assert not _is_registered(repo, path)
            assert not os.path.lexists(path)

        assert publisher.durable == [
            wl.WorktreeTransition(intent=wl.WorktreeIntent.CREATING, expected_head=head),
            wl.WorktreeTransition(intent=wl.WorktreeIntent.PRESENT, expected_head=head),
            wl.WorktreeTransition(intent=wl.WorktreeIntent.DISPOSING, expected_head=head),
            wl.ABSENT_WORKTREE_TRANSITION,
        ]
        # The shared cursor tracked every write -- no spurious staleness.
        assert bundle.cursor.current.worktree == wl.ABSENT_WORKTREE_TRANSITION
        assert writer.refresh() == bundle.cursor.current
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Real SIGKILL boundaries. A crash leaves a non-absent worktree projection;
# current reconciliation REFUSES it, so a fresh prepare_lifecycle() is
# blocked. This is fail-closed blocking, NOT recovery.
# ---------------------------------------------------------------------------


def _enter_and_sigkill(repo_path: str, state_dir: str, id_file: str, stop_intent: str) -> None:
    """Module-level (picklable) child target: prepares a real lease,
    enters a real publisher-mode GitWorktree, and self-SIGKILLs
    immediately after the durable write for `stop_intent` — before the
    reservation's or GitWorktree's own cleanup could ever run."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_child
    import codeagent.workspace as ws_child

    lease = ls_child.prepare_lifecycle(repo_path, run_id=f"wt-sigkill-{stop_intent}")
    with open(id_file, "w") as f:
        f.write(f"{lease.repo_key}\n{lease.lifecycle_id}")
        f.flush()
        os.fsync(f.fileno())
    writer, current = lease.open_projection_writer()
    real = ls_child.LifecycleWorktreePublisher(writer, current)

    class _Killer:
        repo_key = real.repo_key
        lifecycle_id = real.lifecycle_id
        state_root_id = real.state_root_id

        def publish(self, transition):
            real.publish(transition)
            if transition.intent.value == stop_intent:
                os.kill(os.getpid(), signal.SIGKILL)

    reservation = lease.state_root.reserve_worktree_leaf(lease.repo_key, lease.lifecycle_id)
    worktree = ws_child.GitWorktree(
        repo_path, run_id=f"wt-sigkill-{stop_intent}", reservation=reservation, worktree_publisher=_Killer()
    )
    worktree.__enter__()
    os.kill(os.getpid(), signal.SIGKILL)  # unreachable for creating/present


def _spawn_and_await_sigkill(args) -> None:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_enter_and_sigkill, args=args)
    proc.start()
    proc.join(timeout=60)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("child process did not self-SIGKILL within the timeout")
    assert proc.exitcode == -signal.SIGKILL


def _crash_and_inspect(tmp_path, monkeypatch, stop_intent):
    repo = _make_repo(tmp_path)
    state_dir = tmp_path / "state-root"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state_dir))
    id_file = tmp_path / "ids.txt"
    _spawn_and_await_sigkill((str(repo), str(state_dir), str(id_file), stop_intent))
    repo_key, lifecycle_id = id_file.read_text().split("\n")
    projections = [p for p in state_dir.rglob("lifecycle.json") if lifecycle_id in p.parts]
    assert len(projections) == 1
    payload = json.loads(projections[0].read_text())
    leaf = state_dir / "worktrees" / repo_key / lifecycle_id
    return repo, leaf, payload


def _blocked(repo) -> None:
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="wt-after-crash")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED


def _teardown_crashed(repo: Path, leaf: Path) -> None:
    """Reliable teardown of the state a crash deliberately left behind,
    confirmed by observation."""
    if _is_registered(repo, leaf):
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(leaf)], capture_output=True)
    if os.path.isdir(leaf) and not os.path.islink(leaf) and not any(leaf.iterdir()):
        os.rmdir(leaf)
    assert not _is_registered(repo, leaf)
    assert not os.path.lexists(leaf)


def test_real_sigkill_after_durable_creating_blocks_admission(tmp_path, monkeypatch):
    repo, leaf, payload = _crash_and_inspect(tmp_path, monkeypatch, "creating")
    try:
        assert payload["worktree"] == {"intent": "creating", "expected_head": _head(repo)}
        # Before any Git mutation: the empty reserved leaf, unregistered.
        assert leaf.is_dir() and not any(leaf.iterdir())
        assert not _is_registered(repo, leaf)
        _blocked(repo)
    finally:
        _teardown_crashed(repo, leaf)


def test_real_sigkill_after_durable_present_blocks_admission(tmp_path, monkeypatch):
    repo, leaf, payload = _crash_and_inspect(tmp_path, monkeypatch, "present")
    try:
        assert payload["worktree"] == {"intent": "present", "expected_head": _head(repo)}
        assert _is_registered(repo, leaf)
        assert (leaf / "f.txt").exists()
        _blocked(repo)
    finally:
        _teardown_crashed(repo, leaf)
