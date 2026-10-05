"""Tests for `codeagent.state_root` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1 sections 2, 6, 7, 15; Slice 3C-2's `reserve_worktree_leaf`/
`_WorktreeLeafReservation` additions, ADR 0004 sections 8/16)."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

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


# ---------------------------------------------------------------------------
# reserve_worktree_leaf / _WorktreeLeafReservation (Milestone 3 Slice 3C-2)
# ---------------------------------------------------------------------------

_REPO_KEY = "a" * 32
_LIFECYCLE_ID = "b" * 32
_LIFECYCLE_ID_2 = "c" * 32


def _state_root_for_worktree_tests(tmp_path):
    root_fd = _root_fd(tmp_path)
    return sr.init_state_root(root_fd, str(tmp_path / "root"))


def test_reserve_worktree_leaf_exact_accepted_adr_path(tmp_path):
    """Pins the exact deterministic layout ADR 0004 sections 8/16 and
    `reconciliation.py`'s own existing worktree check already assume."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            expected = Path(state_root.path) / "worktrees" / _REPO_KEY / _LIFECYCLE_ID
            assert reservation.path == expected
            assert reservation.path.is_dir()
            assert list(reservation.path.iterdir()) == []
    finally:
        state_root.close()


def test_reserve_worktree_leaf_parent_chain_idempotently_reopened(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as r1:
            parent = r1.path.parent
        # A different lifecycle_id under the same repo_key reopens the
        # already-created, idempotent parent chain successfully — the
        # parent is never exclusive, only the leaf is.
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID_2) as r2:
            assert r2.path.parent == parent
            assert r2.path != r1.path
    finally:
        state_root.close()


def test_reserve_worktree_leaf_same_leaf_collision_refused(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID):
            with pytest.raises(FileExistsError):
                state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    finally:
        state_root.close()


def test_reserve_worktree_leaf_refuses_symlink_at_leaf_name(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        parent = Path(state_root.path) / "worktrees" / _REPO_KEY
        parent.mkdir(parents=True)
        (parent / _LIFECYCLE_ID).symlink_to(tmp_path)  # hostile pre-planted symlink
        with pytest.raises((FileExistsError, lf.LifecycleFsError)):
            state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        # mkdirat's own exclusivity means the symlink is never followed
        # or adopted — it must still be exactly what was planted.
        assert (parent / _LIFECYCLE_ID).is_symlink()
    finally:
        state_root.close()


def test_reserve_worktree_leaf_refuses_wrong_type_at_leaf_name(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        parent = Path(state_root.path) / "worktrees" / _REPO_KEY
        parent.mkdir(parents=True)
        (parent / _LIFECYCLE_ID).write_bytes(b"not a directory")
        with pytest.raises((FileExistsError, lf.LifecycleFsError)):
            state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    finally:
        state_root.close()


def test_reserve_worktree_leaf_refuses_unsafe_parent(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        parent = Path(state_root.path) / "worktrees" / _REPO_KEY
        parent.mkdir(parents=True)
        os.chmod(parent, 0o777)  # group/other-writable: unsafe, matches open_repo_dir's discipline
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS
    finally:
        state_root.close()


def test_reserve_worktree_leaf_validates_hex32(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with pytest.raises(lf.LifecycleFsError):
            state_root.reserve_worktree_leaf("not-hex", _LIFECYCLE_ID)
        with pytest.raises(lf.LifecycleFsError):
            state_root.reserve_worktree_leaf(_REPO_KEY, "not-hex")
    finally:
        state_root.close()


def test_unused_reservation_removes_only_its_own_empty_leaf(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            path = reservation.path
            assert path.is_dir()
        assert not path.exists(), "an unused, never-consumed reservation must clean up its own leaf"
    finally:
        state_root.close()


def test_unused_reservation_refuses_to_remove_swapped_in_different_inode(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        path = reservation.path
        path.rmdir()
        path.mkdir()  # a different, same-user "attacker" directory at the identical name
        assert reservation.verify_identity() is False
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            reservation.__exit__(None, None, None)
        assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
        assert path.exists(), "the swapped-in foreign directory must never be removed"
    finally:
        state_root.close()


def test_unused_reservation_refuses_a_non_empty_leaf(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        (reservation.path / "unexpected-file").write_bytes(b"x")
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            reservation.__exit__(None, None, None)
        assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
        assert reservation.path.exists(), "a non-empty leaf must never be force-removed"
    finally:
        (reservation.path / "unexpected-file").unlink()
        reservation.path.rmdir()
        state_root.close()


def test_reservation_descriptors_closed_on_unused_cleanup(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            fd = reservation.fileno()
        # fileno() raises once descriptors are closed -- this also proves
        # the descriptor itself was actually closed, not merely forgotten.
        with pytest.raises(lf.LifecycleFsError):
            reservation.fileno()
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        state_root.close()


def test_reservation_descriptors_closed_after_consume(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        fd = reservation.fileno()
        reservation.claim()
        reservation.consume()
        reservation.__exit__(None, None, None)
        # consumed: __exit__ must never remove the (now real) worktree
        # directory, only close descriptors.
        assert reservation.path.exists()
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        reservation.path.rmdir()
        state_root.close()


def test_reservation_consume_is_not_reversed_by_later_close_failure(tmp_path, monkeypatch):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        reservation.claim()
        reservation.consume()
        monkeypatch.setattr(
            sr,
            "close_confirmed",
            lambda fds: (_ for _ in ()).throw(
                lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")
            ),
        )
        with pytest.raises(lf.LifecycleFsError):
            reservation.__exit__(None, None, None)
        # A descriptor-close failure after consume() must never cause the
        # (now real, live) worktree directory to be mistaken for an
        # unused reservation and removed.
        assert reservation.path.exists()
    finally:
        monkeypatch.undo()
        # Same fault-injection descriptor leak as the "simultaneous
        # removal and close failure" test above: the mocked
        # close_confirmed() never really closed anything.
        os.close(reservation._leaf_fd)
        os.close(reservation._parent_fd)
        reservation.path.rmdir()
        state_root.close()


def test_reservation_cannot_be_consumed_twice(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        reservation.claim()
        reservation.consume()
        with pytest.raises(lf.LifecycleFsError):
            reservation.consume()
    finally:
        reservation.path.rmdir()
        reservation.__exit__(None, None, None)
        state_root.close()


def test_reservation_simultaneous_removal_and_close_failure_combined(tmp_path, monkeypatch):
    """Neither the removal failure nor the descriptor-close failure is
    silently discarded in favor of the other — both are represented in
    one combined, sanitized cleanup-unconfirmed error."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        path = reservation.path
        path.rmdir()
        path.mkdir()  # forces the removal step to refuse (identity mismatch)
        monkeypatch.setattr(
            sr,
            "close_confirmed",
            lambda fds: (_ for _ in ()).throw(
                lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")
            ),
        )
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            reservation.__exit__(None, None, None)
        assert "removal" in excinfo.value.message and "closure" in excinfo.value.message
    finally:
        monkeypatch.undo()
        # __exit__'s mocked close_confirmed() threw without ever calling
        # the real os.close(): the reservation itself now believes its
        # descriptors are closed (self._descriptors_closed is set
        # unconditionally), but the real fds are still open. Close them
        # directly so this fault-injection test doesn't leak them.
        os.close(reservation._leaf_fd)
        os.close(reservation._parent_fd)
        path.rmdir()
        state_root.close()


def test_reservation_cleanup_failure_recorded_not_raised_when_body_exception_propagating(tmp_path):
    """Matches `GitWorktree.__exit__`'s own existing convention: a
    reservation-cleanup failure must never mask a more significant,
    already-in-flight exception from the `with` block — it is recorded
    on `cleanup_error` instead."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        path = reservation.path
        path.rmdir()
        path.mkdir()  # forces cleanup to fail
        body_exc = ValueError("body failure")
        # Directly exercise __exit__ as Python would during real unwinding.
        reservation.__exit__(type(body_exc), body_exc, None)
        assert reservation.cleanup_error is not None
        assert reservation.cleanup_error.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    finally:
        path.rmdir()
        state_root.close()


def test_reserve_worktree_leaf_closes_parent_fd_on_exclusive_create_failure(tmp_path, monkeypatch):
    """If the exclusive leaf creation fails after the parent chain was
    already opened, the parent descriptor must not leak."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        opened_fds = []
        orig_open_chain = sr.open_managed_directory_chain

        def spy_open_chain(parent_fd, components):
            fd = orig_open_chain(parent_fd, components)
            opened_fds.append(fd)
            return fd

        monkeypatch.setattr(sr, "open_managed_directory_chain", spy_open_chain)
        monkeypatch.setattr(
            sr,
            "create_exclusive_directory_at",
            lambda parent_fd, basename: (_ for _ in ()).throw(FileExistsError()),
        )
        with pytest.raises(FileExistsError):
            state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        assert len(opened_fds) == 1
        with pytest.raises(OSError):
            os.fstat(opened_fds[0])
    finally:
        state_root.close()


def test_no_rmtree_or_prune_reference_exists_in_state_root_module() -> None:
    """Static AST-level proof that `_WorktreeLeafReservation`'s cleanup
    logic (Slice 3C-2) never resorts to a recursive/broad removal
    fallback — matching `workspace.py`'s own identical regression guard.
    `shutil` must never be imported; `rmtree` must never be called; the
    string `"prune"` must never appear as a literal in the module's
    actual code (docstrings mentioning it in prose are not standalone
    `"prune"`-valued constants and don't trip this check)."""
    import ast
    import inspect

    import codeagent.state_root as state_root_module

    source = inspect.getsource(state_root_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "shutil", "state_root.py must not import shutil"
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "rmtree", "state_root.py must not import rmtree"
        if isinstance(node, ast.Attribute) and node.attr == "rmtree":
            raise AssertionError("state_root.py must not call any *.rmtree(...)")
        if isinstance(node, ast.Constant) and node.value == "prune":
            raise AssertionError("state_root.py must not reference a 'prune' operation")


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 11: reservation identity and observe_leaf().
# ---------------------------------------------------------------------------


def test_reservation_exposes_bound_identity(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            assert reservation.repo_key == _REPO_KEY
            assert reservation.lifecycle_id == _LIFECYCLE_ID
            assert reservation.state_root_id == state_root.state_root_id
    finally:
        state_root.close()


def test_observe_leaf_reserved_inode_then_absent_after_removal(tmp_path):
    """The retained leaf descriptor still refers to the now-unlinked
    inode, yet the fd-relative no-follow lookup of the name is a confirmed
    ENOENT -- the only thing ABSENT relies on (never link count)."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            assert reservation.observe_leaf() is sr.LeafObservation.RESERVED_INODE
            os.rmdir(reservation.path)
            assert reservation.observe_leaf() is sr.LeafObservation.ABSENT
            os.fstat(reservation.fileno())  # the held descriptor is still valid
    finally:
        state_root.close()


def test_observe_leaf_dangling_symlink_is_other_not_absent(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            os.rmdir(reservation.path)
            os.symlink(tmp_path / "does-not-exist", reservation.path)
            # The pathname check this replaces in publisher mode would
            # misreport this as absent.
            assert not reservation.path.exists()
            assert reservation.observe_leaf() is sr.LeafObservation.OTHER
            os.unlink(reservation.path)
    finally:
        state_root.close()


def test_observe_leaf_foreign_directory_and_wrong_type_are_other(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            os.rmdir(reservation.path)
            os.mkdir(reservation.path)
            assert reservation.observe_leaf() is sr.LeafObservation.OTHER
            os.rmdir(reservation.path)
            reservation.path.write_text("x")
            assert reservation.observe_leaf() is sr.LeafObservation.OTHER
            os.unlink(reservation.path)
    finally:
        state_root.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks are bypassed for root")
def test_observe_leaf_permission_failure_is_unknown_not_absent(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            parent = reservation.path.parent
            original = stat.S_IMODE(os.stat(parent).st_mode)
            os.chmod(parent, 0o000)
            try:
                assert reservation.observe_leaf() is sr.LeafObservation.UNKNOWN
            finally:
                os.chmod(parent, original)
    finally:
        state_root.close()


def test_observe_leaf_unknown_after_descriptors_closed_even_if_fd_numbers_reused(tmp_path):
    """fd-reuse defense: after the reservation closes its descriptors, the
    same integer descriptor numbers are reopened onto an unrelated
    directory that *does* contain an entry with the leaf's name. Relying
    on EBADF would then inspect the wrong directory; the closed-flag check
    must return UNKNOWN without touching the reused numbers."""
    state_root = _state_root_for_worktree_tests(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / _LIFECYCLE_ID).mkdir()
    reopened: list[int] = []
    try:
        with state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID) as reservation:
            parent_fd = reservation._parent_fd
            leaf_fd = reservation._leaf_fd
        # Reassign BOTH freed descriptor numbers: the parent number onto
        # the decoy directory, the leaf number onto the decoy's same-named
        # child. Without the closed-flag check, this would be misread as
        # the reserved inode.
        matched: set[int] = set()
        for _ in range(256):
            fd = os.open(decoy, os.O_RDONLY | os.O_DIRECTORY)
            if fd == leaf_fd:
                os.close(fd)
                fd = os.open(decoy / _LIFECYCLE_ID, os.O_RDONLY | os.O_DIRECTORY)
                assert fd == leaf_fd
            reopened.append(fd)
            if fd in (parent_fd, leaf_fd):
                matched.add(fd)
            if matched == {parent_fd, leaf_fd}:
                break
        assert matched == {parent_fd, leaf_fd}

        # Demonstrate the danger the flag guards against: with the flag
        # bypassed, the reused numbers resolve to the decoy and are
        # misclassified as the reserved inode.
        reservation._descriptors_closed = False
        assert reservation.observe_leaf() is sr.LeafObservation.RESERVED_INODE
        reservation._descriptors_closed = True
        assert reservation.observe_leaf() is sr.LeafObservation.UNKNOWN
    finally:
        for fd in reopened:
            os.close(fd)
        state_root.close()


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 12: open_abandoned_worktree_leaf, the shared removal
# primitive, the reservation's unchanged messages, and the close state
# machine.
# ---------------------------------------------------------------------------


def _open_fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def _abandoned_leaf_path(state_root) -> Path:
    return Path(state_root.path) / "worktrees" / _REPO_KEY / _LIFECYCLE_ID


def _make_parent(state_root) -> Path:
    parent = Path(state_root.path) / "worktrees" / _REPO_KEY
    parent.mkdir(parents=True, exist_ok=True)
    (Path(state_root.path) / "worktrees").chmod(0o700)
    parent.chmod(0o700)
    return parent


def _make_empty_leaf(state_root, mode: int = 0o700) -> Path:
    _make_parent(state_root)
    leaf = _abandoned_leaf_path(state_root)
    leaf.mkdir()
    leaf.chmod(mode)
    return leaf


@pytest.fixture
def abandoned_root(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        yield state_root
    finally:
        leaf = _abandoned_leaf_path(state_root)
        if leaf.is_symlink() or leaf.is_file():
            leaf.unlink()
        elif leaf.is_dir():
            leaf.chmod(0o700)
            for child in leaf.iterdir():
                child.unlink() if not child.is_dir() else child.rmdir()
            leaf.rmdir()
        state_root.close()


def test_open_abandoned_leaf_parent_chain_absent(abandoned_root):
    before = _open_fd_count()
    result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert result.observation is sr.AbandonedLeafObservation.ABSENT
    assert result.leaf is None
    assert _open_fd_count() == before


def test_open_abandoned_leaf_parent_present_leaf_absent(abandoned_root):
    _make_parent(abandoned_root)
    before = _open_fd_count()
    result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert result.observation is sr.AbandonedLeafObservation.ABSENT
    assert result.leaf is None
    assert _open_fd_count() == before


def test_open_abandoned_leaf_empty_private_directory_returns_owned_handle(abandoned_root):
    _make_empty_leaf(abandoned_root)
    before = _open_fd_count()
    result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert result.observation is sr.AbandonedLeafObservation.EMPTY_PRIVATE_DIRECTORY
    assert result.leaf is not None
    assert _open_fd_count() == before + 2
    result.leaf.close()
    assert _open_fd_count() == before


@pytest.mark.parametrize(
    "setup",
    ["dangling_symlink", "dir_symlink", "regular_file", "mode_0755", "mode_0500", "nonempty"],
)
def test_open_abandoned_leaf_conflicts(abandoned_root, tmp_path, setup):
    leaf = _abandoned_leaf_path(abandoned_root)
    _make_parent(abandoned_root)
    if setup == "dangling_symlink":
        os.symlink(tmp_path / "nowhere", leaf)
    elif setup == "dir_symlink":
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        os.symlink(target, leaf)
    elif setup == "regular_file":
        leaf.write_text("x")
    elif setup == "mode_0755":
        _abandoned_leaf_path(abandoned_root).mkdir()
        leaf.chmod(0o755)
    elif setup == "mode_0500":
        leaf.mkdir()
        leaf.chmod(0o500)
    elif setup == "nonempty":
        leaf.mkdir(mode=0o700)
        (leaf / "f").write_text("x")
    before = _open_fd_count()
    result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert result.observation is sr.AbandonedLeafObservation.CONFLICT
    assert result.leaf is None
    assert _open_fd_count() == before


def test_open_abandoned_leaf_symlinked_parent_is_conflict(abandoned_root, tmp_path):
    worktrees = Path(abandoned_root.path) / "worktrees"
    worktrees.mkdir(mode=0o700)
    target = tmp_path / "other-parent"
    target.mkdir(mode=0o700)
    os.symlink(target, worktrees / _REPO_KEY)
    try:
        result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        assert result.observation is sr.AbandonedLeafObservation.CONFLICT
    finally:
        (worktrees / _REPO_KEY).unlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks are bypassed for root")
def test_open_abandoned_leaf_unreadable_parent_is_unknown(abandoned_root):
    parent = _make_parent(abandoned_root)
    parent.chmod(0o000)
    try:
        result = abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        assert result.observation is sr.AbandonedLeafObservation.UNKNOWN
        assert result.leaf is None
    finally:
        parent.chmod(0o700)


def _failing_dominant_cleanup(calls):
    """Really closes every descriptor (so nothing leaks), then reports the
    cleanup as unconfirmed exactly like `_dominant_cleanup` would."""

    def fake(fds, primary):
        calls.append(list(fds))
        lf.close_confirmed(fds)
        err = lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "simulated")
        if primary is not None:
            raise err from primary
        raise err

    return fake


@pytest.mark.parametrize("setup", ["absent_leaf", "conflict", "unknown"])
def test_open_abandoned_leaf_setup_close_failure_raises_never_unknown(abandoned_root, monkeypatch, setup):
    leaf = _abandoned_leaf_path(abandoned_root)
    _make_parent(abandoned_root)
    expected = {
        "absent_leaf": sr.AbandonedLeafObservation.ABSENT,
        "conflict": sr.AbandonedLeafObservation.CONFLICT,
        "unknown": sr.AbandonedLeafObservation.UNKNOWN,
    }[setup]
    if setup == "conflict":
        leaf.write_text("x")
    if setup == "unknown":
        real_stat = os.stat

        def failing_stat(path, *a, **k):
            if path == _LIFECYCLE_ID and k.get("dir_fd") is not None:
                raise PermissionError(13, "denied")
            return real_stat(path, *a, **k)

        monkeypatch.setattr(sr.os, "stat", failing_stat)
    calls: list = []
    monkeypatch.setattr(sr, "_dominant_cleanup", _failing_dominant_cleanup(calls))
    before = _open_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, sr._LeafObservationDiagnostic)
    assert excinfo.value.__cause__.observation is expected
    assert len(calls) == 1 and len(calls[0]) == 1  # the parent, attempted exactly once
    assert _open_fd_count() == before


def test_open_abandoned_leaf_cloexec_failure_closes_both_and_raises(abandoned_root, monkeypatch):
    _make_empty_leaf(abandoned_root)

    def failing_assert(fd):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "no cloexec")

    monkeypatch.setattr(sr, "_assert_cloexec", failing_assert)
    before = _open_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    assert _open_fd_count() == before


def test_open_abandoned_leaf_cloexec_failure_plus_cleanup_failure_dominates(abandoned_root, monkeypatch):
    _make_empty_leaf(abandoned_root)
    cloexec_error = lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "no cloexec")

    def failing_assert(fd):
        raise cloexec_error

    calls: list = []
    monkeypatch.setattr(sr, "_assert_cloexec", failing_assert)
    monkeypatch.setattr(sr, "_dominant_cleanup", _failing_dominant_cleanup(calls))
    before = _open_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        abandoned_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    assert excinfo.value.__cause__ is cloexec_error
    assert len(calls) == 1 and len(calls[0]) == 2  # parent and leaf, each attempted once
    assert _open_fd_count() == before


def _open_handle(state_root):
    _make_empty_leaf(state_root)
    result = state_root.open_abandoned_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
    assert result.observation is sr.AbandonedLeafObservation.EMPTY_PRIVATE_DIRECTORY
    return result.leaf


def _patch_rmdir(monkeypatch, behavior):
    real_rmdir = os.rmdir

    def fake(path, *a, dir_fd=None, **k):
        if path == _LIFECYCLE_ID and dir_fd is not None:
            return behavior(real_rmdir, path, dir_fd)
        return real_rmdir(path, *a, dir_fd=dir_fd, **k)

    monkeypatch.setattr(sr.os, "rmdir", fake)


def _patch_post_stat_failure(monkeypatch):
    """Fail the second fd-relative lstat of the leaf name (the post-removal
    observation); the first (identity) succeeds."""
    real_stat = os.stat
    seen = {"n": 0}

    def fake(path, *a, **k):
        if path == _LIFECYCLE_ID and k.get("dir_fd") is not None:
            seen["n"] += 1
            if seen["n"] == 2:
                raise PermissionError(13, "denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(sr.os, "stat", fake)


def _real_then(after):
    def behavior(real_rmdir, path, dir_fd):
        real_rmdir(path, dir_fd=dir_fd)
        after(path, dir_fd)

    return behavior


def _replace_with_dir(path, dir_fd):
    os.mkdir(path, 0o700, dir_fd=dir_fd)


def _replace_with_symlink(path, dir_fd):
    os.symlink("/nonexistent-target", path, dir_fd=dir_fd)


def _raise_eio(*_):
    raise OSError(errno.EIO, "simulated")


def _real_then_raise(real_rmdir, path, dir_fd):
    real_rmdir(path, dir_fd=dir_fd)
    raise OSError(errno.EIO, "simulated")


@pytest.mark.parametrize(
    "case, rmdir_behavior, post_stat_fails, kind, report, name_present",
    [
        ("S1", None, False, "POST_ABSENT", "SUCCEEDED", False),
        ("S2", lambda real, p, fd: None, False, "ORIGINAL_STILL_PRESENT", "SUCCEEDED", True),
        ("S3-dir", _real_then(_replace_with_dir), False, "REPLACEMENT_CONFLICT", "SUCCEEDED", True),
        ("S3-symlink", _real_then(_replace_with_symlink), False, "REPLACEMENT_CONFLICT", "SUCCEEDED", True),
        ("S4", None, True, "POST_INSPECTION_FAILED", "SUCCEEDED", None),
        ("E1", _real_then_raise, False, "POST_ABSENT", "OTHER_ERROR", False),
        ("E2", lambda real, p, fd: _raise_eio(), False, "ORIGINAL_STILL_PRESENT", "OTHER_ERROR", True),
        (
            "E3",
            lambda real, p, fd: (real(p, dir_fd=fd), _replace_with_dir(p, fd), _raise_eio()),
            False,
            "REPLACEMENT_CONFLICT",
            "OTHER_ERROR",
            True,
        ),
        ("E4", lambda real, p, fd: _raise_eio(), True, "POST_INSPECTION_FAILED", "OTHER_ERROR", None),
    ],
)
def test_removal_post_observation_matrix(
    abandoned_root, monkeypatch, case, rmdir_behavior, post_stat_fails, kind, report, name_present
):
    leaf = _open_handle(abandoned_root)
    try:
        if rmdir_behavior is not None:
            _patch_rmdir(monkeypatch, rmdir_behavior)
        if post_stat_fails:
            _patch_post_stat_failure(monkeypatch)
        result = leaf.remove_if_still_empty()
        monkeypatch.undo()
        assert result.kind is sr.LeafRemovalKind[kind], case
        assert result.rmdir is sr.RmdirReport[report], case
        assert result.stage is sr.LeafRemovalStage.POST_OBSERVATION
        assert result.post_name_present is name_present
    finally:
        leaf.close()


def test_removal_post_inspection_failed_with_entry_present(abandoned_root, monkeypatch):
    """lstat finds an entry after rmdir, but the held descriptor cannot be
    fstat'd: POST_INSPECTION_FAILED with `post_name_present=True`."""
    leaf = _open_handle(abandoned_root)
    try:
        _patch_rmdir(monkeypatch, lambda real, p, fd: None)
        real_fstat = os.fstat
        seen = {"n": 0}

        def fake_fstat(fd):
            seen["n"] += 1
            if seen["n"] == 2:  # the post-observation fstat
                raise OSError(errno.EIO, "simulated")
            return real_fstat(fd)

        monkeypatch.setattr(sr.os, "fstat", fake_fstat)
        result = leaf.remove_if_still_empty()
        monkeypatch.undo()
        assert result.kind is sr.LeafRemovalKind.POST_INSPECTION_FAILED
        assert result.post_name_present is True
    finally:
        leaf.close()


def test_removal_already_absent_and_pre_conflicts(abandoned_root):
    leaf = _open_handle(abandoned_root)
    try:
        os.rmdir(_abandoned_leaf_path(abandoned_root))
        result = leaf.remove_if_still_empty()
        assert result.kind is sr.LeafRemovalKind.ALREADY_ABSENT
        assert result.rmdir is sr.RmdirReport.NOT_CALLED
    finally:
        leaf.close()

    leaf = _open_handle(abandoned_root)
    try:
        path = _abandoned_leaf_path(abandoned_root)
        os.rename(path, path.parent / "moved")
        path.mkdir(mode=0o700)  # a different inode at the name
        result = leaf.remove_if_still_empty()
        assert result.kind is sr.LeafRemovalKind.PRE_CONFLICT
        assert result.stage is sr.LeafRemovalStage.IDENTITY
        assert path.is_dir()  # nothing removed
        (path.parent / "moved").rmdir()
    finally:
        leaf.close()
    _abandoned_leaf_path(abandoned_root).rmdir()

    leaf = _open_handle(abandoned_root)
    try:
        (_abandoned_leaf_path(abandoned_root) / "late").write_text("x")
        result = leaf.remove_if_still_empty()
        assert result.kind is sr.LeafRemovalKind.PRE_CONFLICT
        assert result.stage is sr.LeafRemovalStage.LISTING
        assert result.rmdir is sr.RmdirReport.NOT_CALLED
    finally:
        leaf.close()


def test_removal_not_empty_at_rmdir_removes_nothing(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    path = _abandoned_leaf_path(abandoned_root)
    try:

        def fill_then_rmdir(real, p, fd):
            (path / "raced").write_text("x")
            return real(p, dir_fd=fd)

        _patch_rmdir(monkeypatch, fill_then_rmdir)
        result = leaf.remove_if_still_empty()
        monkeypatch.undo()
        assert result.kind is sr.LeafRemovalKind.NOT_EMPTY_AT_RMDIR
        assert result.rmdir is sr.RmdirReport.ENOTEMPTY
        assert (path / "raced").exists()
    finally:
        leaf.close()


def test_removal_pre_inspection_failures(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    try:
        monkeypatch.setattr(sr, "_directory_has_any_entry", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "x")))
        result = leaf.remove_if_still_empty()
        monkeypatch.undo()
        assert result.kind is sr.LeafRemovalKind.PRE_INSPECTION_FAILED
        assert result.stage is sr.LeafRemovalStage.LISTING
        assert _abandoned_leaf_path(abandoned_root).is_dir()
    finally:
        leaf.close()


_RESERVATION_MESSAGES = {
    "identity": "the worktree-leaf reservation's identity could not be reconfirmed; no removal was attempted",
    "contents": "the worktree-leaf reservation's contents could not be inspected before removal",
    "nonempty": "the worktree-leaf reservation is not empty and was not removed",
    "rmdir": "the worktree-leaf reservation could not be removed",
    "still": "the worktree-leaf reservation's removal could not be confirmed; an entry still exists at that name",
    "unconfirmed": "the worktree-leaf reservation's removal could not be confirmed",
}


@pytest.mark.parametrize(
    "kind, stage, report, present, expected",
    [
        ("ALREADY_ABSENT", "IDENTITY", "NOT_CALLED", None, None),
        ("PRE_CONFLICT", "IDENTITY", "NOT_CALLED", None, "identity"),
        ("PRE_INSPECTION_FAILED", "IDENTITY", "NOT_CALLED", None, "identity"),
        ("PRE_INSPECTION_FAILED", "LISTING", "NOT_CALLED", None, "contents"),
        ("PRE_CONFLICT", "LISTING", "NOT_CALLED", None, "nonempty"),
        ("NOT_EMPTY_AT_RMDIR", "RMDIR", "ENOTEMPTY", None, "rmdir"),
        ("POST_ABSENT", "POST_OBSERVATION", "OTHER_ERROR", False, "rmdir"),
        ("ORIGINAL_STILL_PRESENT", "POST_OBSERVATION", "OTHER_ERROR", True, "rmdir"),
        ("POST_INSPECTION_FAILED", "POST_OBSERVATION", "OTHER_ERROR", None, "rmdir"),
        ("POST_ABSENT", "POST_OBSERVATION", "SUCCEEDED", False, None),
        ("ORIGINAL_STILL_PRESENT", "POST_OBSERVATION", "SUCCEEDED", True, "still"),
        ("REPLACEMENT_CONFLICT", "POST_OBSERVATION", "SUCCEEDED", True, "still"),
        ("POST_INSPECTION_FAILED", "POST_OBSERVATION", "SUCCEEDED", True, "still"),
        ("POST_INSPECTION_FAILED", "POST_OBSERVATION", "SUCCEEDED", None, "unconfirmed"),
    ],
)
def test_reservation_mapping_preserves_exact_existing_messages(kind, stage, report, present, expected):
    result = sr._LeafRemovalResult(
        sr.LeafRemovalKind[kind], sr.LeafRemovalStage[stage], sr.RmdirReport[report], post_name_present=present
    )
    error = sr._reservation_removal_error(result)
    if expected is None:
        assert error is None
    else:
        assert error.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
        assert str(error) == _RESERVATION_MESSAGES[expected]


def test_reservation_exit_reports_entry_still_exists_when_rmdir_is_a_noop(tmp_path, monkeypatch):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        reservation = state_root.reserve_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)
        _patch_rmdir(monkeypatch, lambda real, p, fd: None)
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            reservation.__exit__(None, None, None)
        monkeypatch.undo()
        assert str(excinfo.value) == _RESERVATION_MESSAGES["still"]
        reservation.path.rmdir()
    finally:
        state_root.close()


# --- close state machine -----------------------------------------------------


def _spy_close(monkeypatch, fail_fds=()):
    """Spy on os.close as used by close_confirmed. A descriptor in
    `fail_fds` is really closed, then reported as failed."""
    real_close = os.close
    calls: list[int] = []

    def fake(fd):
        calls.append(fd)
        real_close(fd)
        if fd in fail_fds:
            raise OSError(errno.EIO, "simulated")

    monkeypatch.setattr(lf.os, "close", fake)
    return calls


def test_close_attempts_both_once_then_never_again(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    fds = {leaf._leaf_fd, leaf._parent_fd}
    calls = _spy_close(monkeypatch)
    leaf.close()
    assert set(calls) == fds and len(calls) == 2
    leaf.close()
    with leaf:
        pass
    assert len(calls) == 2  # CLOSED_CONFIRMED: no OS call ever again


def test_close_failure_is_latched_and_reraised_identically_without_reclose(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    calls = _spy_close(monkeypatch, fail_fds={leaf._leaf_fd})
    with pytest.raises(lf.LifecycleFsError) as first:
        leaf.close()
    err = first.value
    assert err.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    close_exc = err.__cause__
    assert isinstance(close_exc, lf.LifecycleFsError)
    assert close_exc.__cause__ is None
    assert len(calls) == 2
    for attempt in (leaf.close, lambda: leaf.__exit__(None, None, None)):
        with pytest.raises(lf.LifecycleFsError) as again:
            attempt()
        assert again.value is err
        assert again.value.__cause__ is close_exc
        assert close_exc.__cause__ is None
    assert len(calls) == 2  # never re-closed


def test_close_failure_chain_preserves_body_exception_beneath_close_error(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    _spy_close(monkeypatch, fail_fds={leaf._parent_fd})
    body = ValueError("body")
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        with leaf:
            raise body
    err = excinfo.value
    assert isinstance(err.__cause__, lf.LifecycleFsError)  # close_confirmed's error
    assert err.__cause__.__cause__ is body
    with pytest.raises(lf.LifecycleFsError) as again:
        leaf.close()
    assert again.value is err and err.__cause__.__cause__ is body


def test_exit_with_body_exception_and_clean_close_propagates_body(abandoned_root):
    leaf = _open_handle(abandoned_root)
    with pytest.raises(ValueError):
        with leaf:
            raise ValueError("body")
    leaf.close()  # CLOSED_CONFIRMED: returns normally


def test_remove_refused_after_close_and_on_second_call(abandoned_root, monkeypatch):
    leaf = _open_handle(abandoned_root)
    leaf.remove_if_still_empty()
    with pytest.raises(lf.LifecycleFsError):
        leaf.remove_if_still_empty()
    leaf.close()
    with pytest.raises(lf.LifecycleFsError):
        leaf.remove_if_still_empty()


def test_close_failure_never_touches_reused_descriptor_numbers(abandoned_root, tmp_path, monkeypatch):
    leaf = _open_handle(abandoned_root)
    numbers = {leaf._leaf_fd, leaf._parent_fd}
    _spy_close(monkeypatch, fail_fds=numbers)
    with pytest.raises(lf.LifecycleFsError):
        leaf.close()
    monkeypatch.undo()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    reopened = []
    try:
        for _ in range(64):
            fd = os.open(decoy, os.O_RDONLY)
            reopened.append(fd)
            if numbers <= set(reopened):
                break
        assert numbers <= set(reopened)
        calls = _spy_close(monkeypatch)
        with pytest.raises(lf.LifecycleFsError):
            leaf.close()
        assert calls == []
        for fd in numbers:
            os.fstat(fd)  # the decoys reusing those numbers are still open
    finally:
        monkeypatch.undo()
        for fd in reopened:
            os.close(fd)


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 13: observe_materialized_worktree_leaf (fresh, no held
# descriptor, never distinguishes the original from a valid replacement).
# ---------------------------------------------------------------------------


def _observe(state_root):
    return state_root.observe_materialized_worktree_leaf(_REPO_KEY, _LIFECYCLE_ID)


def _make_materialized_leaf(state_root, *, mode=0o700, git="file"):
    leaf = _make_empty_leaf(state_root)
    if git == "file":
        (leaf / ".git").write_text("gitdir: /nowhere\n")
    elif git == "dir":
        (leaf / ".git").mkdir()
    elif git == "symlink":
        os.symlink("/nowhere", leaf / ".git")
    (leaf / "content.txt").write_text("x")
    leaf.chmod(mode)
    return leaf


def test_materialized_observer_absent_parent_and_absent_leaf(abandoned_root):
    before = _open_fd_count()
    assert _observe(abandoned_root) is sr.MaterializedLeafObservation.ABSENT
    _make_parent(abandoned_root)
    assert _observe(abandoned_root) is sr.MaterializedLeafObservation.ABSENT
    assert _open_fd_count() == before


def test_materialized_observer_materialized_and_replacement_is_still_materialized(abandoned_root):
    leaf = _make_materialized_leaf(abandoned_root)
    before = _open_fd_count()
    assert _observe(abandoned_root) is sr.MaterializedLeafObservation.MATERIALIZED
    assert _open_fd_count() == before
    # A valid replacement directory cannot be told apart from the original.
    os.rename(leaf, leaf.parent / "original")
    _make_materialized_leaf(abandoned_root)
    assert _observe(abandoned_root) is sr.MaterializedLeafObservation.MATERIALIZED
    for child in (leaf.parent / "original").iterdir():
        child.unlink()
    (leaf.parent / "original").rmdir()


@pytest.mark.parametrize("setup", ["mode_0755", "git_dir", "git_symlink", "git_missing", "leaf_symlink", "leaf_file"])
def test_materialized_observer_conflicts(abandoned_root, tmp_path, setup):
    leaf = _abandoned_leaf_path(abandoned_root)
    if setup == "mode_0755":
        _make_materialized_leaf(abandoned_root, mode=0o755)
    elif setup == "git_dir":
        _make_materialized_leaf(abandoned_root, git="dir")
    elif setup == "git_symlink":
        _make_materialized_leaf(abandoned_root, git="symlink")
    elif setup == "git_missing":
        _make_materialized_leaf(abandoned_root, git=None)
    elif setup == "leaf_symlink":
        _make_parent(abandoned_root)
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        (target / ".git").write_text("x")
        os.symlink(target, leaf)
    elif setup == "leaf_file":
        _make_parent(abandoned_root)
        leaf.write_text("x")
    before = _open_fd_count()
    assert _observe(abandoned_root) is sr.MaterializedLeafObservation.CONFLICT
    assert _open_fd_count() == before
    if leaf.is_dir() and not leaf.is_symlink():
        leaf.chmod(0o700)
        git = leaf / ".git"
        if git.is_dir() and not git.is_symlink():
            git.rmdir()


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks are bypassed for root")
def test_materialized_observer_unreadable_parent_is_unknown(abandoned_root):
    parent = _make_parent(abandoned_root)
    parent.chmod(0o000)
    try:
        assert _observe(abandoned_root) is sr.MaterializedLeafObservation.UNKNOWN
    finally:
        parent.chmod(0o700)


def test_materialized_observer_cloexec_failure_alone_and_with_cleanup_failure(abandoned_root, monkeypatch):
    _make_materialized_leaf(abandoned_root)
    cloexec_error = lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "no cloexec")
    monkeypatch.setattr(sr, "_assert_cloexec", lambda fd: (_ for _ in ()).throw(cloexec_error))
    before = _open_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        _observe(abandoned_root)
    assert excinfo.value is cloexec_error
    assert _open_fd_count() == before
    calls: list = []
    monkeypatch.setattr(sr, "_dominant_cleanup", _failing_dominant_cleanup(calls))
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        _observe(abandoned_root)
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    assert excinfo.value.__cause__ is cloexec_error
    assert len(calls) == 1 and len(calls[0]) == 2
    assert _open_fd_count() == before


@pytest.mark.parametrize("setup", ["materialized", "conflict", "absent_leaf"])
def test_materialized_observer_setup_close_failure_raises_never_unknown(abandoned_root, monkeypatch, setup):
    if setup == "materialized":
        _make_materialized_leaf(abandoned_root)
    elif setup == "conflict":
        _make_materialized_leaf(abandoned_root, git=None)
    else:
        _make_parent(abandoned_root)
    calls: list = []
    monkeypatch.setattr(sr, "_dominant_cleanup", _failing_dominant_cleanup(calls))
    before = _open_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        _observe(abandoned_root)
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, sr._LeafObservationDiagnostic)
    assert len(calls) == 1
    assert _open_fd_count() == before


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 18: open_existing_state_root never creates anything
# ---------------------------------------------------------------------------


def _context_elsewhere(tmp_path):
    return sr.TrustedRepositoryContext(working_tree_root=str(tmp_path / "repo"), common_dir=str(tmp_path / "repo" / ".git"))


def test_open_existing_state_root_absent_creates_nothing(tmp_path):
    location = _explicit_location(tmp_path / "missing" / "root")
    assert sr.open_existing_state_root(location, _context_elsewhere(tmp_path)) is None
    assert not (tmp_path / "missing").exists()


def test_open_existing_state_root_empty_creates_no_identity_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    assert sr.open_existing_state_root(_explicit_location(root), _context_elsewhere(tmp_path)) is None
    assert os.listdir(root) == []


def test_open_existing_state_root_content_without_identity_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "repos").mkdir(mode=0o700)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        sr.open_existing_state_root(_explicit_location(root), _context_elsewhere(tmp_path))
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    assert sorted(os.listdir(root)) == ["repos"]


def test_open_existing_state_root_partial_identity_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / sr.STATE_ROOT_JSON_FILENAME).write_bytes(b"")
    (root / sr.STATE_ROOT_JSON_FILENAME).chmod(0o600)
    with pytest.raises(lf.LifecycleFsError):
        sr.open_existing_state_root(_explicit_location(root), _context_elsewhere(tmp_path))


def test_open_existing_state_root_valid_returns_owned_root(tmp_path):
    root = tmp_path / "root"
    fd, canonical = sr.open_or_create_canonical_root(_explicit_location(root))
    created = sr.init_state_root(fd, canonical)
    created.close()
    opened = sr.open_existing_state_root(_explicit_location(root), _context_elsewhere(tmp_path))
    try:
        assert opened.state_root_id == created.state_root_id
    finally:
        opened.close()


def test_open_existing_state_root_containment_refused(tmp_path):
    root = tmp_path / "repo" / "root"
    root.mkdir(parents=True, mode=0o700)
    with pytest.raises(lf.LifecycleFsError):
        sr.open_existing_state_root(_explicit_location(root), _context_elsewhere(tmp_path))


def test_if_present_openers_never_create(tmp_path):
    state_root = _state_root_for_worktree_tests(tmp_path)
    try:
        assert state_root.open_repo_locks_dir_if_present() is None
        assert state_root.open_repo_dir_if_present("a" * 32) is None
        assert sorted(os.listdir(tmp_path / "root")) == [sr.STATE_ROOT_JSON_FILENAME]
    finally:
        state_root.close()
