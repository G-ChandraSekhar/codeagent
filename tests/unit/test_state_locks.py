"""Tests for `codeagent.state_locks` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1 section 11 / section 6)."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import time

import pytest

from codeagent import _lifecycle_fs as lf
from codeagent import state_locks as sl
from codeagent.state_root import (
    init_state_root,
    open_or_create_canonical_root,
)


def _make_state_root(tmp_path):
    location = lf.StateRootLocation(
        path=str(tmp_path / "state-root"),
        origin=lf.StateRootOrigin.EXPLICIT,
        conventional_parent_creation_allowed=False,
    )
    fd, canonical = open_or_create_canonical_root(location)
    return init_state_root(fd, canonical)


# ---------------------------------------------------------------------------
# LockScope validation
# ---------------------------------------------------------------------------


def test_lock_scope_repository_forbids_lifecycle_id():
    with pytest.raises(lf.LifecycleFsError):
        sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32, lifecycle_id="b" * 32)


def test_lock_scope_lifecycle_requires_lifecycle_id():
    with pytest.raises(lf.LifecycleFsError):
        sl.LockScope(kind=sl.LockKind.LIFECYCLE, repo_key="a" * 32)


def test_lock_scope_validates_hex32_repo_key():
    with pytest.raises(lf.LifecycleFsError):
        sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="not-hex")


def test_lock_scope_lifecycle_valid():
    scope = sl.LockScope(kind=sl.LockKind.LIFECYCLE, repo_key="a" * 32, lifecycle_id="b" * 32)
    assert scope.lifecycle_id == "b" * 32


def test_lock_scope_equality():
    a = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
    b = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
    assert a == b


# ---------------------------------------------------------------------------
# acquire_lock_nonblocking_at
# ---------------------------------------------------------------------------


def test_acquire_lock_nonblocking_at_basic(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        assert handle.is_held
        handle.release()
        assert not handle.is_held
    finally:
        os.close(parent_fd)


def test_acquire_lock_nonblocking_at_busy_when_already_held(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle1 = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        assert excinfo.value.reason is sl.LockFailure.BUSY
        handle1.release()
    finally:
        os.close(parent_fd)


def test_lock_file_never_truncated_or_replaced(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lock_path = tmp_path / "x.lock"
        lock_path.write_bytes(b"preexisting content")
        os.chmod(lock_path, 0o600)  # a safe pre-existing lock file
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        assert lock_path.read_bytes() == b"preexisting content"
        handle.release()
    finally:
        os.close(parent_fd)


def test_lock_release_after_body_exception_still_releases(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        try:
            raise RuntimeError("body failure")
        except RuntimeError:
            handle.release()
        handle2 = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        handle2.release()
    finally:
        os.close(parent_fd)


def test_lock_handle_is_context_manager(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        with sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock") as handle:
            assert handle.is_held
        assert not handle.is_held
    finally:
        os.close(parent_fd)


def test_release_unlock_failure_only(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        real_flock = fcntl.flock

        def _fake_flock(fd, op):
            if op == fcntl.LOCK_UN:
                raise OSError("simulated unlock failure")
            return real_flock(fd, op)

        monkeypatch.setattr(sl.fcntl, "flock", _fake_flock)
        with pytest.raises(sl.LockError) as excinfo:
            handle.release()
        assert excinfo.value.reason is sl.LockFailure.RELEASE_UNCONFIRMED
        assert "released" in excinfo.value.message
        assert "descriptor" not in excinfo.value.message
    finally:
        os.close(parent_fd)


def test_release_close_failure_only(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        real_close = os.close

        def _fake_close(fd):
            raise OSError("simulated close failure")

        monkeypatch.setattr(sl.os, "close", _fake_close)
        try:
            with pytest.raises(sl.LockError) as excinfo:
                handle.release()
            assert excinfo.value.reason is sl.LockFailure.RELEASE_UNCONFIRMED
            assert "closed" in excinfo.value.message
        finally:
            monkeypatch.setattr(sl.os, "close", real_close)
            real_close(handle._fd)
    finally:
        os.close(parent_fd)


def test_release_both_unlock_and_close_failure_combined(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        def _fake_flock(fd, op):
            raise OSError("simulated unlock failure")

        def _fake_close(fd):
            raise OSError("simulated close failure")

        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(sl.fcntl, "flock", _fake_flock)
                scoped.setattr(sl.os, "close", _fake_close)
                with pytest.raises(sl.LockError) as excinfo:
                    handle.release()
                assert excinfo.value.reason is sl.LockFailure.RELEASE_UNCONFIRMED
                assert "unlocked" in excinfo.value.message and "closed" in excinfo.value.message
        finally:
            try:
                os.close(handle._fd)
            except OSError:
                pass
    finally:
        os.close(parent_fd)


def test_release_body_exception_and_release_failure_chains(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        def _fake_flock(fd, op):
            raise OSError("simulated unlock failure")

        monkeypatch.setattr(sl.fcntl, "flock", _fake_flock)
        body_exc = ValueError("body failure")
        try:
            with handle:
                raise body_exc
        except sl.LockError as release_exc:
            assert release_exc.__cause__ is body_exc
        else:
            pytest.fail("expected LockError")
        real_close = __import__("os").close
        try:
            real_close(handle._fd)
        except OSError:
            pass
    finally:
        os.close(parent_fd)


def test_acquire_lock_refuses_unsafe_existing_mode(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lock_path = tmp_path / "x.lock"
        lock_path.write_bytes(b"")
        os.chmod(lock_path, 0o644)  # world-readable: unsafe
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(parent_fd)


def test_acquire_lock_refuses_symlink_lock_file(tmp_path):
    real = tmp_path / "real.lock"
    real.write_bytes(b"")
    os.chmod(real, 0o600)
    (tmp_path / "x.lock").symlink_to(real)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Correction pass: partial-acquisition-failure cleanup must never swallow
# unlock/close failures, and must chain from the original BUSY/substrate error
# ---------------------------------------------------------------------------


def test_partial_acquisition_failure_unlock_fails_chains_from_busy(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        holder = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        real_flock = fcntl.flock

        def _fake_flock(fd, op):
            if op == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                raise BlockingIOError("busy")  # the real acquisition failure
            raise OSError("simulated unlock cleanup failure")  # the LOCK_UN cleanup attempt

        with monkeypatch.context() as scoped:
            scoped.setattr(sl.fcntl, "flock", _fake_flock)
            with pytest.raises(sl.LockError) as excinfo:
                sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
            assert excinfo.value.reason is sl.LockFailure.RELEASE_UNCONFIRMED
            assert isinstance(excinfo.value.__cause__, sl.LockError)
            assert excinfo.value.__cause__.reason is sl.LockFailure.BUSY
        holder.release()
    finally:
        os.close(parent_fd)


def test_partial_acquisition_failure_both_unlock_and_close_fail(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        holder = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")

        def _fake_flock(fd, op):
            if op == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                raise BlockingIOError("busy")
            raise OSError("simulated unlock cleanup failure")

        real_close = os.close

        def _fake_close(fd):
            raise OSError("simulated close cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(sl.fcntl, "flock", _fake_flock)
            scoped.setattr(sl.os, "close", _fake_close)
            with pytest.raises(sl.LockError) as excinfo:
                sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
            assert excinfo.value.reason is sl.LockFailure.RELEASE_UNCONFIRMED
            assert "unlocked" in excinfo.value.message and "closed" in excinfo.value.message
            assert excinfo.value.__cause__.reason is sl.LockFailure.BUSY
        holder.release()
    finally:
        os.close(parent_fd)


def test_open_lock_file_at_cloexec_failure_does_not_swallow_close_failure(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        def _failing_assert_cloexec(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced cloexec failure")

        def _failing_close(fd):
            raise OSError("simulated close failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(sl, "_assert_cloexec", _failing_assert_cloexec)
            scoped.setattr(sl.os, "close", _failing_close)
            with pytest.raises(sl.LockError) as excinfo:
                sl._open_lock_file_at(parent_fd, "x.lock")
            assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE
            assert "closed" in excinfo.value.message
    finally:
        os.close(parent_fd)


def test_acquire_repository_lock_acquisition_failure_and_parent_cleanup_failure_both_preserved(
    tmp_path, monkeypatch
):
    """When acquisition itself fails (BUSY) AND the repo-locks parent-fd
    cleanup that follows also fails, the original BUSY failure must be
    preserved as the chained cause — never silently replaced."""
    state_root = _make_state_root(tmp_path)
    try:
        repo_key = "f1" + "0" * 30
        holder = sl.acquire_repository_lock(state_root, repo_key)
        try:
            def _failing_cleanup(parent_fd, *, primary):
                os.close(parent_fd)
                raise sl.LockError(sl.LockFailure.SUBSTRATE_UNAVAILABLE, "forced parent cleanup failure") from primary

            with monkeypatch.context() as scoped:
                scoped.setattr(sl, "_dominant_parent_cleanup", _failing_cleanup)
                with pytest.raises(sl.LockError) as excinfo:
                    sl.acquire_repository_lock(state_root, repo_key)
                assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE
                assert isinstance(excinfo.value.__cause__, sl.LockError)
                assert excinfo.value.__cause__.reason is sl.LockFailure.BUSY
        finally:
            holder.release()
    finally:
        state_root.close()


def test_acquire_lock_no_descriptor_leak_on_busy(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
        handle = sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        fd_count_before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            with pytest.raises(sl.LockError):
                sl.acquire_lock_nonblocking_at(parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock")
        fd_count_after = len(os.listdir("/dev/fd"))
        assert fd_count_after == fd_count_before
        handle.release()
    finally:
        os.close(parent_fd)


def test_inode_mismatch_refusal(tmp_path):
    """A lock file that is atomically replaced beneath a pathname
    between the flock and the lstat/fstat identity check must be
    refused: fstat(fd) and lstat(path) must identify the same inode."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (tmp_path / "x.lock").write_bytes(b"")

        class _FakeStat:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                if name == "st_ino":
                    return self._real.st_ino + 1
                return getattr(self._real, name)

        original_lstat = os.lstat

        def _fake_lstat(path, *, dir_fd=None):
            return _FakeStat(original_lstat(path, dir_fd=dir_fd))

        import codeagent.state_locks as sl_mod

        orig = sl_mod.os.lstat
        sl_mod.os.lstat = _fake_lstat
        try:
            scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="a" * 32)
            with pytest.raises(sl.LockError) as excinfo:
                sl.acquire_lock_nonblocking_at(
                    parent_fd, "x.lock", scope=scope, diagnostic_path="x.lock"
                )
            assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE
        finally:
            sl_mod.os.lstat = orig
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# acquire_repository_lock: full composition
# ---------------------------------------------------------------------------


def test_acquire_repository_lock_basic(tmp_path):
    state_root = _make_state_root(tmp_path)
    try:
        repo_key = "a" * 32
        handle = sl.acquire_repository_lock(state_root, repo_key)
        assert handle.is_held
        assert handle.scope == sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key=repo_key)
        handle.release()
    finally:
        state_root.close()


def test_acquire_repository_lock_busy_second_caller(tmp_path):
    state_root = _make_state_root(tmp_path)
    try:
        repo_key = "b" * 32
        handle1 = sl.acquire_repository_lock(state_root, repo_key)
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_repository_lock(state_root, repo_key)
        assert excinfo.value.reason is sl.LockFailure.BUSY
        handle1.release()
    finally:
        state_root.close()


def test_acquire_repository_lock_validates_repo_key():
    class _FakeStateRoot:
        def open_repo_locks_dir(self):
            raise AssertionError("should not be called")

    with pytest.raises(lf.LifecycleFsError):
        sl.acquire_repository_lock(_FakeStateRoot(), "not-hex")


def test_acquire_repository_lock_parent_cleanup_failure_releases_acquired_lock(tmp_path, monkeypatch):
    state_root = _make_state_root(tmp_path)
    try:
        repo_key = "c" * 32
        original = sl._dominant_parent_cleanup

        def _failing_cleanup(parent_fd, *, primary):
            os.close(parent_fd)  # ensure no leak
            raise sl.LockError(sl.LockFailure.SUBSTRATE_UNAVAILABLE, "forced parent cleanup failure")

        monkeypatch.setattr(sl, "_dominant_parent_cleanup", _failing_cleanup)
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_repository_lock(state_root, repo_key)
        assert excinfo.value.reason is sl.LockFailure.SUBSTRATE_UNAVAILABLE

        monkeypatch.setattr(sl, "_dominant_parent_cleanup", original)
        # A second acquisition must succeed: the first lock was released.
        handle = sl.acquire_repository_lock(state_root, repo_key)
        handle.release()
    finally:
        state_root.close()


# ---------------------------------------------------------------------------
# Real two-process lock contention and release after SIGKILL
# ---------------------------------------------------------------------------


def _child_hold_lock(state_root_path: str, repo_key: str, ready_evt, release_evt) -> None:
    import codeagent._lifecycle_fs as _lf
    import codeagent.state_locks as _sl
    from codeagent.state_root import init_state_root, open_or_create_canonical_root

    location = _lf.StateRootLocation(
        path=state_root_path, origin=_lf.StateRootOrigin.EXPLICIT, conventional_parent_creation_allowed=False
    )
    fd, canonical = open_or_create_canonical_root(location)
    state_root = init_state_root(fd, canonical)
    handle = _sl.acquire_repository_lock(state_root, repo_key)
    ready_evt.set()
    release_evt.wait(timeout=30)


def test_real_two_process_lock_contention_and_release(tmp_path):
    state_root_path = str(tmp_path / "state-root")
    repo_key = "d" * 32
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(target=_child_hold_lock, args=(state_root_path, repo_key, ready_evt, release_evt))
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        state_root = _make_state_root(tmp_path)
        try:
            with pytest.raises(sl.LockError) as excinfo:
                sl.acquire_repository_lock(state_root, repo_key)
            assert excinfo.value.reason is sl.LockFailure.BUSY
        finally:
            state_root.close()
    finally:
        release_evt.set()
        proc.join(timeout=10)


def test_real_lock_acquirable_after_holder_sigkilled(tmp_path):
    state_root_path = str(tmp_path / "state-root")
    repo_key = "e" * 32
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(target=_child_hold_lock, args=(state_root_path, repo_key, ready_evt, release_evt))
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        assert proc.exitcode is not None and proc.exitcode != 0

        state_root = _make_state_root(tmp_path)
        try:
            handle = sl.acquire_repository_lock(state_root, repo_key)
            assert handle.is_held
            handle.release()
        finally:
            state_root.close()
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


# ---------------------------------------------------------------------------
# acquire_lifecycle_lock (Milestone 3 Slice 3A-2)
# ---------------------------------------------------------------------------


def test_acquire_lifecycle_lock_basic(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        handle = sl.acquire_lifecycle_lock(
            parent_fd, repo_key="a" * 32, lifecycle_id="b" * 32, diagnostic_path="x"
        )
        assert handle.is_held
        assert handle.scope == sl.LockScope(kind=sl.LockKind.LIFECYCLE, repo_key="a" * 32, lifecycle_id="b" * 32)
        handle.release()
        assert not handle.is_held
    finally:
        os.close(parent_fd)


def test_acquire_lifecycle_lock_validates_repo_key_and_lifecycle_id():
    with pytest.raises(lf.LifecycleFsError):
        sl.acquire_lifecycle_lock(0, repo_key="not-hex", lifecycle_id="b" * 32, diagnostic_path="x")
    with pytest.raises(lf.LifecycleFsError):
        sl.acquire_lifecycle_lock(0, repo_key="a" * 32, lifecycle_id="not-hex", diagnostic_path="x")


def test_acquire_lifecycle_lock_busy_when_already_held(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        handle1 = sl.acquire_lifecycle_lock(
            parent_fd, repo_key="a" * 32, lifecycle_id="b" * 32, diagnostic_path="x"
        )
        with pytest.raises(sl.LockError) as excinfo:
            sl.acquire_lifecycle_lock(parent_fd, repo_key="a" * 32, lifecycle_id="b" * 32, diagnostic_path="x")
        assert excinfo.value.reason is sl.LockFailure.BUSY
        handle1.release()
    finally:
        os.close(parent_fd)


def test_acquire_lifecycle_lock_does_not_close_caller_owned_parent_fd(tmp_path):
    # Unlike acquire_repository_lock, this wrapper opens no short-lived
    # parent-fd of its own: parent_fd remains open and usable by the
    # caller after acquisition (and after release), since it is the
    # long-lived run-directory descriptor the caller continues to own.
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        handle = sl.acquire_lifecycle_lock(
            parent_fd, repo_key="a" * 32, lifecycle_id="b" * 32, diagnostic_path="x"
        )
        # parent_fd is still valid: a fresh fstat succeeds.
        os.fstat(parent_fd)
        handle.release()
        os.fstat(parent_fd)
    finally:
        os.close(parent_fd)


def _child_hold_lifecycle_lock(run_dir_path: str, repo_key: str, lifecycle_id: str, ready_evt, release_evt) -> None:
    import codeagent.state_locks as _sl

    parent_fd = os.open(run_dir_path, os.O_RDONLY | os.O_DIRECTORY)
    handle = _sl.acquire_lifecycle_lock(
        parent_fd, repo_key=repo_key, lifecycle_id=lifecycle_id, diagnostic_path=run_dir_path
    )
    ready_evt.set()
    release_evt.wait(timeout=30)


def test_real_two_process_lifecycle_lock_contention_and_release(tmp_path):
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    repo_key = "f" * 32
    lifecycle_id = "e" * 32
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_child_hold_lifecycle_lock, args=(str(run_dir), repo_key, lifecycle_id, ready_evt, release_evt)
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        parent_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(sl.LockError) as excinfo:
                sl.acquire_lifecycle_lock(
                    parent_fd, repo_key=repo_key, lifecycle_id=lifecycle_id, diagnostic_path=str(run_dir)
                )
            assert excinfo.value.reason is sl.LockFailure.BUSY
        finally:
            os.close(parent_fd)
    finally:
        release_evt.set()
        proc.join(timeout=10)


def test_real_lifecycle_lock_acquirable_after_holder_sigkilled(tmp_path):
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    repo_key = "1" + "a" * 31
    lifecycle_id = "2" + "b" * 31
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_child_hold_lifecycle_lock, args=(str(run_dir), repo_key, lifecycle_id, ready_evt, release_evt)
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        assert proc.exitcode is not None and proc.exitcode != 0

        parent_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            handle = sl.acquire_lifecycle_lock(
                parent_fd, repo_key=repo_key, lifecycle_id=lifecycle_id, diagnostic_path=str(run_dir)
            )
            assert handle.is_held
            handle.release()
        finally:
            os.close(parent_fd)
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
