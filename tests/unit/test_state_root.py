"""Tests for `codeagent.state_root` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1 sections 2, 6, 7, 15)."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import time

import pytest

from codeagent import _lifecycle_fs as lf
from codeagent import state_root as sr


def _explicit_location(path):
    return lf.StateRootLocation(
        path=str(path), origin=lf.StateRootOrigin.EXPLICIT, conventional_parent_creation_allowed=False
    )


# ---------------------------------------------------------------------------
# open_or_create_canonical_root
# ---------------------------------------------------------------------------


def test_open_or_create_canonical_root_creates_new(tmp_path):
    location = _explicit_location(tmp_path / "state-root")
    fd, canonical = sr.open_or_create_canonical_root(location)
    try:
        assert os.path.isdir(canonical)
        st = os.fstat(fd)
        assert stat.S_IMODE(st.st_mode) == 0o700
    finally:
        os.close(fd)


def test_open_or_create_canonical_root_reopens_existing(tmp_path):
    location = _explicit_location(tmp_path / "state-root")
    fd1, canonical1 = sr.open_or_create_canonical_root(location)
    os.close(fd1)
    fd2, canonical2 = sr.open_or_create_canonical_root(location)
    try:
        assert canonical1 == canonical2
    finally:
        os.close(fd2)


def test_open_or_create_canonical_root_refuses_group_writable(tmp_path):
    path = tmp_path / "state-root"
    path.mkdir(mode=0o770)
    os.chmod(path, 0o770)
    location = _explicit_location(path)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        sr.open_or_create_canonical_root(location)
    assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS


def test_open_or_create_canonical_root_bounded_parent_creation_macos(tmp_path):
    location = lf.StateRootLocation(
        path=str(tmp_path / "Library" / "Application Support" / "CodeAgent"),
        origin=lf.StateRootOrigin.MACOS_DEFAULT,
        conventional_parent_creation_allowed=True,
    )
    fd, canonical = sr.open_or_create_canonical_root(location)
    try:
        assert (tmp_path / "Library" / "Application Support" / "CodeAgent").is_dir()
    finally:
        os.close(fd)


def test_open_or_create_canonical_root_bounded_parent_creation_linux(tmp_path):
    location = lf.StateRootLocation(
        path=str(tmp_path / ".local" / "state" / "codeagent"),
        origin=lf.StateRootOrigin.LINUX_HOME_DEFAULT,
        conventional_parent_creation_allowed=True,
    )
    fd, canonical = sr.open_or_create_canonical_root(location)
    try:
        assert (tmp_path / ".local" / "state" / "codeagent").is_dir()
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Directional containment: equality/descendant/prefix-sibling/unrelated
# ---------------------------------------------------------------------------


def test_containment_refuses_state_root_inside_common_dir():
    context = sr.TrustedRepositoryContext(working_tree_root="/repo", common_dir="/repo/.git")
    with pytest.raises(lf.LifecycleFsError):
        sr.validate_state_root_containment("/repo/.git/state-root", context)


def test_containment_refuses_state_root_inside_working_tree():
    context = sr.TrustedRepositoryContext(working_tree_root="/repo", common_dir="/repo/.git")
    with pytest.raises(lf.LifecycleFsError):
        sr.validate_state_root_containment("/repo/state-root", context)


def test_containment_refuses_working_tree_inside_state_root_worktrees():
    context = sr.TrustedRepositoryContext(working_tree_root="/state-root/worktrees/x", common_dir="/repo/.git")
    with pytest.raises(lf.LifecycleFsError):
        sr.validate_state_root_containment("/state-root", context)


def test_containment_refuses_common_dir_inside_state_root_worktrees():
    context = sr.TrustedRepositoryContext(working_tree_root="/repo", common_dir="/state-root/worktrees/x/.git")
    with pytest.raises(lf.LifecycleFsError):
        sr.validate_state_root_containment("/state-root", context)


def test_containment_ok_unrelated_paths():
    context = sr.TrustedRepositoryContext(working_tree_root="/repo", common_dir="/repo/.git")
    sr.validate_state_root_containment("/state-root", context)  # must not raise


def test_containment_prefix_sibling_not_flagged():
    # "/state-root2" must not be treated as contained in "/state-root".
    context = sr.TrustedRepositoryContext(working_tree_root="/state-root2/repo", common_dir="/state-root2/repo/.git")
    sr.validate_state_root_containment("/state-root", context)  # must not raise


def test_containment_bare_repository_skips_working_tree_checks():
    context = sr.TrustedRepositoryContext(working_tree_root=None, common_dir="/repo/.git")
    sr.validate_state_root_containment("/state-root", context)  # must not raise


# ---------------------------------------------------------------------------
# State-root VALID/ABSENT/RETRYABLE_PARTIAL/PERMANENTLY_INVALID
# ---------------------------------------------------------------------------


def _root_fd(tmp_path):
    d = tmp_path / "root"
    d.mkdir(mode=0o700)
    return os.open(d, os.O_RDONLY | os.O_DIRECTORY)


def test_init_state_root_absent_creates_new(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    try:
        assert len(state_root.state_root_id) == 32
        assert (tmp_path / "root" / "state-root.json").is_file()
    finally:
        state_root.close()


def test_init_state_root_valid_existing_reused(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root1 = sr.init_state_root(root_fd, str(tmp_path / "root"))
    id1 = state_root1.state_root_id
    state_root1.close()

    root_fd2 = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    state_root2 = sr.init_state_root(root_fd2, str(tmp_path / "root"))
    try:
        assert state_root2.state_root_id == id1
    finally:
        state_root2.close()


def test_init_state_root_ordinary_startup_empty_file_never_retries(tmp_path):
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").write_bytes(b"")
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    os.close(root_fd)


def test_init_state_root_ordinary_startup_malformed_json_never_retries(tmp_path):
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").write_bytes(b"{not json")
    with pytest.raises(lf.LifecycleFsError):
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    os.close(root_fd)


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xfe not utf-8",
        b'{"schema_version": 1, "state_root_id": "a" * 32}'.replace(b'"a" * 32', b'"' + b"a" * 5 + b'"'),
        b'{"schema_version": 1, "state_root_id": "' + b"a" * 32 + b'", "extra": 1}',
    ],
)
def test_init_state_root_permanently_invalid_content_never_retries(tmp_path, content):
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").write_bytes(content)
    with pytest.raises(lf.LifecycleFsError):
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    os.close(root_fd)


def test_init_state_root_symlink_is_permanently_invalid(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"schema_version": 1, "state_root_id": "' + "a" * 32 + '"}')
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").symlink_to(real)
    with pytest.raises(lf.LifecycleFsError):
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    os.close(root_fd)


def test_init_state_root_never_regenerates_corrupt_file(tmp_path):
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").write_bytes(b"corrupt")
    with pytest.raises(lf.LifecycleFsError):
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    assert (tmp_path / "root" / "state-root.json").read_bytes() == b"corrupt"
    os.close(root_fd)


# ---------------------------------------------------------------------------
# Correction pass: probing hardening
# ---------------------------------------------------------------------------


def test_probe_asserts_cloexec_on_state_root_json(tmp_path, monkeypatch):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    state_root.close()

    root_fd2 = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        def _failing_assert_cloexec(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(sr, "_assert_cloexec", _failing_assert_cloexec)
            with pytest.raises(lf.LifecycleFsError):
                sr.init_state_root(root_fd2, str(tmp_path / "root"))
    finally:
        os.close(root_fd2)


def test_probe_rejects_wrong_owner(tmp_path, monkeypatch):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    state_root.close()
    real_uid = os.getuid()

    root_fd2 = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(os, "getuid", lambda: real_uid + 1)
            with pytest.raises(lf.LifecycleFsError) as excinfo:
                sr.init_state_root(root_fd2, str(tmp_path / "root"))
            assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(root_fd2)


def test_probe_rejects_unsafe_mode(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    state_root.close()
    os.chmod(tmp_path / "root" / "state-root.json", 0o644)  # world-readable: unsafe

    root_fd2 = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            sr.init_state_root(root_fd2, str(tmp_path / "root"))
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(root_fd2)


def test_probe_cleanup_failure_dominates_over_valid_classification(tmp_path, monkeypatch):
    """A close failure during a successful probe must dominate — the
    caller must see the cleanup failure, never the classified VALID
    outcome silently returned despite it."""
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    state_root.close()

    root_fd2 = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_close_confirmed = sr.close_confirmed

        def _failing_close_confirmed(fds):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

        monkeypatch.setattr(sr, "close_confirmed", _failing_close_confirmed)
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            sr._probe_once(root_fd2)
        assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
        monkeypatch.setattr(sr, "close_confirmed", real_close_confirmed)
    finally:
        os.close(root_fd2)


def test_schema_rejects_boolean_schema_version():
    payload = {"schema_version": True, "state_root_id": "a" * 32}
    assert sr._valid_schema(payload) is False


def test_schema_accepts_real_int_one():
    payload = {"schema_version": 1, "state_root_id": "a" * 32}
    assert sr._valid_schema(payload) is True


def test_init_state_root_empty_root_creates_new(tmp_path):
    # A genuinely empty root (nothing at all, not even state-root.json)
    # must create a fresh identity file.
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    try:
        assert len(state_root.state_root_id) == 32
    finally:
        state_root.close()


def test_init_state_root_unrelated_entry_fails_closed(tmp_path):
    # ADR 0004 section 2 step 2: a root containing anything other than
    # state-root.json, while state-root.json itself is absent, must
    # fail closed — never silently adopted, never regenerated.
    (tmp_path / "root").mkdir(mode=0o700)
    (tmp_path / "root" / "unrelated.txt").write_text("x")
    root_fd = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            sr.init_state_root(root_fd, str(tmp_path / "root"))
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
        # The unrelated entry itself is left completely untouched.
        assert (tmp_path / "root" / "unrelated.txt").read_text() == "x"
        assert not (tmp_path / "root" / "state-root.json").exists()
    finally:
        os.close(root_fd)


def test_init_state_root_unrelated_entry_but_concurrent_creation_completes(tmp_path):
    # The one narrow legitimate case: the root has an unrelated entry
    # AND a concurrent (real) creator finishes between our initial
    # ABSENT observation and our single immediate re-probe.
    (tmp_path / "root").mkdir(mode=0o700)
    (tmp_path / "root" / "unrelated.txt").write_text("x")
    root_fd = os.open(tmp_path / "root", os.O_RDONLY | os.O_DIRECTORY)

    real_probe_once = sr._probe_once
    call_count = {"n": 0}

    def _fake_probe_once(fd):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Simulate a concurrent winner finishing its write exactly
            # between our first ABSENT observation and our re-probe.
            payload = {"schema_version": 1, "state_root_id": "c" * 32}
            winner_fd = os.open("state-root.json", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
            try:
                os.write(winner_fd, lf.canonical_json_dumps(payload))
            finally:
                os.close(winner_fd)
        return real_probe_once(fd)

    import codeagent.state_root as sr_mod

    sr_mod._probe_once = _fake_probe_once
    try:
        state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
        try:
            assert state_root.state_root_id == "c" * 32
        finally:
            state_root.close()
    finally:
        sr_mod._probe_once = real_probe_once


# ---------------------------------------------------------------------------
# Fake-clock bounded-window retry tests
# ---------------------------------------------------------------------------


def test_retry_window_succeeds_once_winner_completes(tmp_path):
    root_fd = _root_fd(tmp_path)
    path = tmp_path / "root" / "state-root.json"
    # Simulate a winner having created an empty placeholder (EEXIST case).
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)

    calls = {"n": 0}

    def fake_clock():
        return calls["n"]

    def fake_sleeper(_seconds):
        calls["n"] += 1
        if calls["n"] == 2:
            # "winner" finishes writing on the second poll
            payload = {"schema_version": 1, "state_root_id": "b" * 32}
            path.write_bytes(lf.canonical_json_dumps(payload))

    payload = sr._retry_after_eexist(root_fd, clock=fake_clock, sleeper=fake_sleeper)
    try:
        assert payload["state_root_id"] == "b" * 32
    finally:
        os.close(root_fd)


def test_retry_window_times_out_if_never_resolves(tmp_path):
    root_fd = _root_fd(tmp_path)
    path = tmp_path / "root" / "state-root.json"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)

    fake_time = {"t": 0.0}

    def fake_clock():
        return fake_time["t"]

    def fake_sleeper(seconds):
        fake_time["t"] += seconds

    with pytest.raises(lf.LifecycleFsError) as excinfo:
        sr._retry_after_eexist(root_fd, clock=fake_clock, sleeper=fake_sleeper)
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    os.close(root_fd)


def test_retry_window_permanently_invalid_stops_immediately(tmp_path):
    root_fd = _root_fd(tmp_path)
    path = tmp_path / "root" / "state-root.json"
    path.write_bytes(b"\xff\xfe invalid")

    calls = {"n": 0}

    def fake_sleeper(_seconds):
        calls["n"] += 1

    with pytest.raises(lf.LifecycleFsError):
        sr._retry_after_eexist(root_fd, clock=lambda: 0.0, sleeper=fake_sleeper)
    assert calls["n"] == 0  # never slept for a permanently-invalid observation
    os.close(root_fd)


# ---------------------------------------------------------------------------
# One real cross-process creation race
# ---------------------------------------------------------------------------


def _child_init_state_root(root_path: str, ready_evt, go_evt, result_queue) -> None:
    import os as _os

    import codeagent.state_root as _sr

    # Signal ready BEFORE waiting on go: the parent waits for both
    # children's ready signals before releasing go, so both children
    # actually start racing at the same moment instead of one running
    # to completion (or timing out on go) before the other even begins.
    ready_evt.set()
    go_evt.wait(timeout=30)
    root_fd = _os.open(root_path, _os.O_RDONLY | _os.O_DIRECTORY)
    try:
        state_root = _sr.init_state_root(root_fd, root_path)
        result_queue.put(("ok", state_root.state_root_id))
        state_root.close()
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))


def test_real_cross_process_creation_race(tmp_path):
    root_path = tmp_path / "root"
    root_path.mkdir(mode=0o700)

    ctx = multiprocessing.get_context("spawn")
    go_evt = ctx.Event()
    ready_evt1 = ctx.Event()
    ready_evt2 = ctx.Event()
    queue = ctx.Queue()

    p1 = ctx.Process(target=_child_init_state_root, args=(str(root_path), ready_evt1, go_evt, queue))
    p2 = ctx.Process(target=_child_init_state_root, args=(str(root_path), ready_evt2, go_evt, queue))
    p1.start()
    p2.start()
    ready_evt1.wait(timeout=10)
    ready_evt2.wait(timeout=10)
    go_evt.set()
    p1.join(timeout=15)
    p2.join(timeout=15)

    results = [queue.get(timeout=5), queue.get(timeout=5)]
    assert all(status == "ok" for status, _ in results), results
    ids = {value for _, value in results}
    assert len(ids) == 1, "both processes must converge on the same state_root_id"


# ---------------------------------------------------------------------------
# StateRoot managed-directory access
# ---------------------------------------------------------------------------


def test_state_root_open_repo_locks_dir(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    try:
        fd = state_root.open_repo_locks_dir()
        try:
            assert (tmp_path / "root" / "repo-locks").is_dir()
        finally:
            os.close(fd)
    finally:
        state_root.close()


def test_state_root_open_repo_dir_validates_hex32(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    try:
        with pytest.raises(lf.LifecycleFsError):
            state_root.open_repo_dir("not-hex")
    finally:
        state_root.close()


def test_state_root_close_is_idempotent(tmp_path):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    state_root.close()
    state_root.close()  # must not raise


def test_state_root_context_manager_closes_on_success(tmp_path):
    root_fd = _root_fd(tmp_path)
    with sr.init_state_root(root_fd, str(tmp_path / "root")) as state_root:
        assert state_root.state_root_id
    with pytest.raises(OSError):
        os.fstat(state_root.root_fd)


def test_state_root_context_manager_close_failure_propagates(tmp_path, monkeypatch):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    monkeypatch.setattr(sr, "close_confirmed", lambda fds: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")))
    with pytest.raises(lf.LifecycleFsError):
        with state_root:
            pass


def test_state_root_context_manager_close_failure_chains_body_exception(tmp_path, monkeypatch):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    monkeypatch.setattr(sr, "close_confirmed", lambda fds: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")))
    body_exc = ValueError("body failure")
    try:
        with state_root:
            raise body_exc
    except lf.LifecycleFsError as cleanup_exc:
        assert cleanup_exc.__cause__ is body_exc
    else:
        pytest.fail("expected LifecycleFsError")


def test_state_root_close_failure_never_reported_as_success_and_never_retried(tmp_path, monkeypatch):
    root_fd = _root_fd(tmp_path)
    state_root = sr.init_state_root(root_fd, str(tmp_path / "root"))
    monkeypatch.setattr(sr, "close_confirmed", lambda fds: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")))
    with pytest.raises(lf.LifecycleFsError):
        state_root.close()
    # A second call must not silently report success, and must not
    # attempt to touch the descriptor again (never retry an ambiguous close).
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        state_root.close()
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED


def test_init_state_root_failure_leaves_root_fd_owned_by_caller(tmp_path):
    root_fd = _root_fd(tmp_path)
    (tmp_path / "root" / "state-root.json").write_bytes(b"")  # ordinary-startup empty: permanent failure
    with pytest.raises(lf.LifecycleFsError):
        sr.init_state_root(root_fd, str(tmp_path / "root"))
    # root_fd must still be open and usable: init_state_root never
    # closed a descriptor it did not itself open on this failure path.
    st = os.fstat(root_fd)
    assert stat.S_ISDIR(st.st_mode)
    os.close(root_fd)
