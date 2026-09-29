"""Tests for `codeagent.state_root` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1 sections 2, 6, 7, 15; Slice 3C-2's `reserve_worktree_leaf`/
`_WorktreeLeafReservation` additions, ADR 0004 sections 8/16)."""

from __future__ import annotations

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
