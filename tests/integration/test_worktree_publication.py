"""ADR 0004 Amendment 11: the real `LifecycleWorktreePublisher` driven by a
real `GitWorktree`, end to end, against a real `prepare_lifecycle()` lease.

This is an optional, *unwired* producer seam: no production composition
path supplies a worktree publisher. The SIGKILL tests prove the honest
outcome of a crash. A crash after a durable `creating` (empty, unregistered
reserved leaf) is now reconciled by the next admission (ADR 0004 Amendment
12), including when the reconciler itself is killed mid-row. A crash after
a durable `present` still blocks admission (`RECONCILIATION_BLOCKED`):
removal of a materialized worktree does not exist yet.
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


def _no_containers(monkeypatch):
    """This row never involves a container; the Docker listing is patched
    so the test does not depend on a local Docker daemon."""
    from codeagent import reconciliation as rc

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))


def _dead_projection(state_dir: Path, lifecycle_id: str) -> dict:
    projections = [p for p in state_dir.rglob("lifecycle.json") if lifecycle_id in p.parts]
    assert len(projections) == 1
    return json.loads(projections[0].read_text())


def _admit_and_close(repo) -> None:
    lease = ls.prepare_lifecycle(str(repo), run_id="wt-after-crash")
    lease.close()


def test_real_sigkill_after_durable_creating_is_reconciled_by_next_admission(tmp_path, monkeypatch):
    """ADR 0004 Amendment 12: before Amendment 12 this blocked admission."""
    repo, leaf, payload = _crash_and_inspect(tmp_path, monkeypatch, "creating")
    try:
        assert payload["worktree"] == {"intent": "creating", "expected_head": _head(repo)}
        # Before any Git mutation: the empty reserved leaf, unregistered.
        assert leaf.is_dir() and not any(leaf.iterdir())
        assert not _is_registered(repo, leaf)
        _no_containers(monkeypatch)
        _admit_and_close(repo)
        dead = _dead_projection(tmp_path / "state-root", leaf.name)
        assert dead["state"] == "RECONCILED"
        assert dead["worktree"] == {"intent": "absent", "expected_head": None}
        assert dead["reconciliation"]["attempts_total"] == 1
        assert not os.path.lexists(leaf)
    finally:
        _teardown_crashed(repo, leaf)


def _reconcile_and_sigkill(repo_path: str, state_dir: str, point: str) -> None:
    """Module-level (picklable) child: runs a real admission whose
    reconciliation pass self-SIGKILLs at `point` in the creating row --
    after M1 (durable RECONCILING), after M2 (the leaf's rmdir), or after
    M5 (durable creating -> absent)."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_child
    import codeagent.reconciliation as rc_child
    import codeagent.state_root as sr_child

    rc_child._docker_ps_all_id_name_pairs = lambda: ({}, {})

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    if point in ("after_m1", "after_m5"):
        real_publish = ls_child.publish_private_file_atomically_at
        seen = {"n": 0}
        stop_at = 1 if point == "after_m1" else 2

        def publish(*a, **k):
            real_publish(*a, **k)
            seen["n"] += 1
            if seen["n"] == stop_at:
                die()

        ls_child.publish_private_file_atomically_at = publish
    else:
        real_rmdir = os.rmdir

        def rmdir(path, *a, dir_fd=None, **k):
            real_rmdir(path, *a, dir_fd=dir_fd, **k)
            if dir_fd is not None and len(str(path)) == 32:
                die()

        sr_child.os.rmdir = rmdir
    ls_child.prepare_lifecycle(repo_path, run_id="wt-reconciler-killed")
    die()  # unreachable


@pytest.mark.parametrize(
    "point, disk_worktree, leaf_present",
    [("after_m1", "creating", True), ("after_m2", "creating", False), ("after_m5", "absent", False)],
)
def test_real_sigkill_inside_reconciler_resumes_without_reincrement(tmp_path, monkeypatch, point, disk_worktree, leaf_present):
    """Resume cases A (after M1), B (after M2), and C (after M5): a fresh
    admission completes the dead entry to RECONCILED with exactly one
    attempt counted across the crash."""
    repo, leaf, _ = _crash_and_inspect(tmp_path, monkeypatch, "creating")
    state_dir = tmp_path / "state-root"
    try:
        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(target=_reconcile_and_sigkill, args=(str(repo), str(state_dir), point))
        proc.start()
        proc.join(timeout=60)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=10)
            pytest.fail("reconciler child did not self-SIGKILL within the timeout")
        assert proc.exitcode == -signal.SIGKILL

        crashed = _dead_projection(state_dir, leaf.name)
        assert crashed["state"] == "RECONCILING"
        assert crashed["worktree"]["intent"] == disk_worktree
        assert crashed["reconciliation"]["attempts_total"] == 1
        assert os.path.lexists(leaf) is leaf_present

        _no_containers(monkeypatch)
        _admit_and_close(repo)
        dead = _dead_projection(state_dir, leaf.name)
        assert dead["state"] == "RECONCILED"
        assert dead["worktree"] == {"intent": "absent", "expected_head": None}
        assert dead["reconciliation"]["attempts_total"] == 1
        assert not os.path.lexists(leaf)
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
