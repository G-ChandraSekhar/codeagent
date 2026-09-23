"""Tests for `codeagent.reconciliation` (Milestone 3 Slice 3B-1, ADR
0004 Amendment 1 section 10, Amendment 2)."""

from __future__ import annotations

import ast
import dataclasses
import errno
import json
import multiprocessing
import os
import signal
import stat
import subprocess
import time

import pytest

from codeagent import _lifecycle_fs as lf
from codeagent import checkpoint_ref as cr
from codeagent import checkpoint_session as cs
from codeagent import lifecycle_store as ls
from codeagent import reconciliation as rc
from codeagent import repo_identity as ri
from codeagent import state_locks as sl
from codeagent import state_root as sr


def _run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _run("git", "init", "-q", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    return repo


def _set_state_dir(monkeypatch, tmp_path, name="state-root"):
    state_dir = tmp_path / name
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state_dir))
    return state_dir


class _Harness:
    """Opens exactly the substrate `reconcile_repository` needs
    (state root, trusted identity/context, repository lock) without
    minting a lifecycle_id or creating a run directory -- the same
    prefix `prepare_lifecycle` itself runs before its own reconciliation
    call. Lets tests pre-seed `runs/` with hand-built fixtures before
    calling `reconcile_repository` directly."""

    def __init__(self, repo, state_dir):
        self.repo = repo
        self.state_dir = state_dir
        identity, context = ri.discover_repository_identity_and_context(str(repo))
        location = lf.resolve_state_root_path()
        root_fd, canonical_root_path = sr.open_or_create_canonical_root(location)
        sr.validate_state_root_containment(canonical_root_path, context)
        self.state_root = sr.init_state_root(root_fd, canonical_root_path)
        self.repository_lock = sl.acquire_repository_lock(self.state_root, identity.repo_key)
        self.identity = ri.load_or_create_repo_json(self.state_root, identity, self.repository_lock)
        self.context = context

    def repo_dir(self):
        return self.state_dir / "repos" / self.identity.repo_key

    def runs_dir(self):
        d = self.repo_dir() / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reconcile(self) -> rc.ReconciliationPassResult:
        return rc.reconcile_repository(
            state_root=self.state_root,
            identity=self.identity,
            context=self.context,
            repository_lock=self.repository_lock,
        )

    def close(self):
        self.repository_lock.release()
        self.state_root.close()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    h = _Harness(repo, state_dir)
    try:
        yield h
    finally:
        h.close()


def _initial_projection(h: _Harness, lifecycle_id: str, *, run_id="run-1"):
    return ls.build_initial_preparing_projection(
        lifecycle_id=lifecycle_id,
        state_root_id=h.state_root.state_root_id,
        repo_key=h.identity.repo_key,
        run_id=run_id,
        source_repo_path=h.context.working_tree_root,
    )


def _seed_run_dir(h: _Harness, lifecycle_id: str, projection=None, *, extra_files: dict[str, bytes] | None = None):
    run_dir = h.runs_dir() / lifecycle_id
    run_dir.mkdir(parents=True)
    if projection is not None:
        data = lf.canonical_json_dumps(ls.projection_to_dict(projection))
        (run_dir / ls.LIFECYCLE_JSON_FILENAME).write_bytes(data)
        (run_dir / ls.LIFECYCLE_JSON_FILENAME).chmod(0o600)
    for name, content in (extra_files or {}).items():
        (run_dir / name).write_bytes(content)
    return run_dir


def _read_projection_dict(run_dir):
    data = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    return lf.canonical_json_loads_strict(data, max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)


# ---------------------------------------------------------------------------
# _is_absent_shape / clean-final cross-field invariant
# ---------------------------------------------------------------------------


def test_is_absent_shape_true_for_initial_projection(harness):
    projection = _initial_projection(harness, "a" * 32)
    assert ls.is_projection_fully_absent_shape(projection)


def test_is_absent_shape_false_when_container_non_absent(harness):
    projection = _initial_projection(harness, "a" * 32)
    tampered = dataclasses.replace(
        projection, baseline=dataclasses.replace(projection.baseline, intent=ls.ContainerIntent.PRESENT, id="x" * 64)
    )
    assert not ls.is_projection_fully_absent_shape(tampered)


def test_is_absent_shape_false_when_failure_populated(harness):
    projection = _initial_projection(harness, "a" * 32)
    tampered = dataclasses.replace(projection, failure=ls.FailureDetail(phase="p", detail="d"))
    assert not ls.is_projection_fully_absent_shape(tampered)


# ---------------------------------------------------------------------------
# Terminal recognition
# ---------------------------------------------------------------------------


def test_terminal_absent_shape_is_skipped_with_zero_calls(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = dataclasses.replace(_initial_projection(harness, lifecycle_id), state=ls.LifecycleState.RECONCILED)
    _seed_run_dir(harness, lifecycle_id, projection)

    def _boom(*a, **k):
        raise AssertionError("must not be called for a terminal entry")

    monkeypatch.setattr(rc, "_docker_ps_all_names", _boom)
    monkeypatch.setattr(rc, "_worktree_registered_paths", _boom)
    monkeypatch.setattr(rc.CheckpointRef, "observe", _boom)
    monkeypatch.setattr(sl, "acquire_lock_nonblocking_at", _boom)

    result = harness.reconcile()
    assert len(result.entries) == 1
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.SKIPPED_TERMINAL
    assert not result.blocked


def test_terminal_with_non_absent_attribution_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = dataclasses.replace(
        projection,
        state=ls.LifecycleState.COMPLETE,
        baseline=dataclasses.replace(projection.baseline, intent=ls.ContainerIntent.PRESENT, id="0" * 64),
    )
    _seed_run_dir(harness, lifecycle_id, projection)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


def test_terminal_with_populated_failure_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    # A populated failure is refused by the schema validator itself
    # (Slice 3A-2's own narrowing), so this must surface as REFUSED via
    # the loader, not a crash.
    payload = ls.projection_to_dict(projection)
    payload["state"] = ls.LifecycleState.RECONCILED.value
    payload["failure"] = {"phase": "p", "detail": "d"}
    run_dir = harness.runs_dir() / lifecycle_id
    run_dir.mkdir(parents=True)
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).write_bytes(lf.canonical_json_dumps(payload))
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).chmod(0o600)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


# ---------------------------------------------------------------------------
# Nonterminal absent-shape reconciliation: RECONCILED
# ---------------------------------------------------------------------------


def test_nonterminal_absent_shape_reconciles_to_reconciled(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    result = harness.reconcile()
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.attempt_number == 1
    assert not result.blocked

    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILED.value
    assert payload["reconciliation"]["attempts_total"] == 1
    assert payload["run_id"] == "run-1"


def test_resumed_reconciling_does_not_double_increment(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = dataclasses.replace(
        projection,
        state=ls.LifecycleState.RECONCILING,
        reconciliation=ls.ReconciliationSummary(attempts_total=1, recent_failures=()),
    )
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.attempt_number == 1  # carried forward, never incremented again

    payload = _read_projection_dict(run_dir)
    assert payload["reconciliation"]["attempts_total"] == 1


def test_fresh_preparing_increments_exactly_once(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    harness.reconcile()
    payload = _read_projection_dict(run_dir)
    assert payload["reconciliation"]["attempts_total"] == 1


# ---------------------------------------------------------------------------
# Resource-present -> REFUSED (no mutation)
# ---------------------------------------------------------------------------


def test_present_container_causes_refused_no_projection_write(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    monkeypatch.setattr(rc, "_docker_ps_all_names", lambda: {f"codeagent-baseline-{lifecycle_id}"})

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value  # untouched


def test_present_worktree_causes_refused_no_projection_write(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    expected_path = os.path.normpath(
        os.path.join(harness.state_root.path, "worktrees", harness.identity.repo_key, lifecycle_id)
    )
    monkeypatch.setattr(rc, "_docker_ps_all_names", lambda: set())
    monkeypatch.setattr(rc, "_worktree_registered_paths", lambda root: {expected_path})

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_present_checkpoint_ref_causes_refused_no_projection_write(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    monkeypatch.setattr(rc, "_docker_ps_all_names", lambda: set())
    monkeypatch.setattr(rc, "_worktree_registered_paths", lambda root: set())
    monkeypatch.setattr(rc.CheckpointRef, "observe", lambda self: cr.RefObservation(present=True, oid="0" * 40))

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_inspection_failure_produces_substrate_unavailable_no_write(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    def _boom():
        raise rc._DockerListingError("boom")

    monkeypatch.setattr(rc, "_docker_ps_all_names", _boom)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert result.blocked
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_checkpoint_ref_object_format_disagreement_is_refused(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    monkeypatch.setattr(rc, "_docker_ps_all_names", lambda: set())
    monkeypatch.setattr(rc, "_worktree_registered_paths", lambda root: set())
    monkeypatch.setattr(
        rc.CheckpointRef, "object_format", property(lambda self: cr.ObjectFormat.SHA256)
    )

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


# ---------------------------------------------------------------------------
# Loader / identity-mismatch / corruption -> REFUSED (or SUBSTRATE_UNAVAILABLE)
# ---------------------------------------------------------------------------


def test_missing_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    _seed_run_dir(harness, lifecycle_id, projection=None)  # directory only, no file

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


def test_symlinked_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    run_dir = _seed_run_dir(harness, lifecycle_id, projection=None)
    target = run_dir.parent.parent / "symlink-target.json"
    target.write_bytes(b"{}")
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).symlink_to(target)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_hard_linked_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    os.link(run_dir / ls.LIFECYCLE_JSON_FILENAME, run_dir / "extra-hardlink.json")

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_unsafe_mode_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).chmod(0o644)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_oversized_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    fd = os.open(str(run_dir), os.O_RDONLY | os.O_DIRECTORY)
    orig_bound = ls.LIFECYCLE_JSON_MAX_BYTES
    ls.LIFECYCLE_JSON_MAX_BYTES = 10
    try:
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls.load_lifecycle_projection(
                fd,
                object_format=harness.identity.object_format,
                expected_lifecycle_id=lifecycle_id,
                expected_repo_key=harness.identity.repo_key,
                expected_state_root_id=harness.state_root.state_root_id,
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID
    finally:
        ls.LIFECYCLE_JSON_MAX_BYTES = orig_bound
        os.close(fd)


def test_malformed_json_is_refused(harness):
    lifecycle_id = "a" * 32
    run_dir = _seed_run_dir(harness, lifecycle_id, projection=None)
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).write_bytes(b"{not json")
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).chmod(0o600)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_duplicate_key_json_is_refused(harness):
    lifecycle_id = "a" * 32
    run_dir = _seed_run_dir(harness, lifecycle_id, projection=None)
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).write_bytes(b'{"schema_version":1,"schema_version":2}')
    (run_dir / ls.LIFECYCLE_JSON_FILENAME).chmod(0o600)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_trailing_data_json_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    with open(run_dir / ls.LIFECYCLE_JSON_FILENAME, "ab") as f:
        f.write(b"\ntrailing-garbage")

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_identity_mismatched_lifecycle_json_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    tampered = dataclasses.replace(projection, repo_key="f" * 32)
    _seed_run_dir(harness, lifecycle_id, tampered)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


# ---------------------------------------------------------------------------
# Level-1 namespace prevalidation
# ---------------------------------------------------------------------------


def test_malformed_shared_entry_aborts_pass_before_legitimate_entry_touched(harness, monkeypatch):
    legitimate_id = "d" * 32
    projection = _initial_projection(harness, legitimate_id)
    _seed_run_dir(harness, legitimate_id, projection)

    def _boom(*a, **k):
        raise AssertionError("a legitimate entry must never be touched once a malformed entry is found")

    monkeypatch.setattr(rc, "_docker_ps_all_names", _boom)
    monkeypatch.setattr(rc, "_worktree_registered_paths", _boom)

    (harness.runs_dir() / "not-a-lifecycle-id").mkdir()

    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.REFUSED

    # The legitimate entry's projection is untouched.
    payload = _read_projection_dict(harness.runs_dir() / legitimate_id)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_symlink_directly_under_runs_aborts_pass(harness, tmp_path):
    target = tmp_path / "symlink-target"
    target.mkdir()
    (harness.runs_dir() / ("a" * 32)).symlink_to(target)

    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.REFUSED


def test_non_directory_under_runs_aborts_pass(harness):
    (harness.runs_dir() / ("a" * 32)).write_text("not a directory")

    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.REFUSED


def test_unsafe_permissions_under_runs_aborts_pass(harness):
    d = harness.runs_dir() / ("a" * 32)
    d.mkdir()
    d.chmod(0o777)
    try:
        with pytest.raises(rc.ReconciliationError) as excinfo:
            harness.reconcile()
        assert excinfo.value.reason is rc.ReconciliationFailure.REFUSED
    finally:
        d.chmod(0o700)


def test_inspection_failure_at_runs_level_is_substrate_unavailable(harness, monkeypatch):
    def _boom(fd):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced listing failure")

    monkeypatch.setattr(rc, "list_directory_entries", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Inner-entry recognition
# ---------------------------------------------------------------------------


def test_unknown_inner_entry_refuses_only_that_entry(harness):
    legitimate_id = "d" * 32
    projection = _initial_projection(harness, legitimate_id)
    run_dir = _seed_run_dir(harness, legitimate_id, projection)
    (run_dir / "unexpected-file").write_text("x")

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


def test_recognized_temp_leftover_is_not_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    leftover = run_dir / f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-0123456789abcdef"
    leftover.write_bytes(b"partial")
    leftover.chmod(0o600)  # the real primitive always creates it private

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert not result.blocked
    assert entry.has_temp_leftover
    # Never opened, never deleted.
    assert leftover.read_bytes() == b"partial"


# ---------------------------------------------------------------------------
# Lock-scope precondition
# ---------------------------------------------------------------------------


def test_wrong_repository_lock_scope_raises(harness):
    other_scope_lock_dir = None
    fake_scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key="f" * 32)
    fake_lock = object.__new__(sl.LockHandle)
    fake_lock._fd = -1
    fake_lock.scope = fake_scope
    fake_lock._held = True
    fake_lock.diagnostic_path = "<fake>"

    with pytest.raises(rc.ReconciliationError) as excinfo:
        rc.reconcile_repository(
            state_root=harness.state_root,
            identity=harness.identity,
            context=harness.context,
            repository_lock=fake_lock,
        )
    assert excinfo.value.reason is rc.ReconciliationFailure.WRONG_LOCK_SCOPE


def test_released_repository_lock_raises(harness):
    correct_scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key=harness.identity.repo_key)
    fake_lock = object.__new__(sl.LockHandle)
    fake_lock._fd = -1
    fake_lock.scope = correct_scope
    fake_lock._held = False
    fake_lock.diagnostic_path = "<fake>"

    with pytest.raises(rc.ReconciliationError) as excinfo:
        rc.reconcile_repository(
            state_root=harness.state_root,
            identity=harness.identity,
            context=harness.context,
            repository_lock=fake_lock,
        )
    assert excinfo.value.reason is rc.ReconciliationFailure.WRONG_LOCK_SCOPE


# ---------------------------------------------------------------------------
# SKIPPED_ACTIVE: a genuinely inconsistent lifecycle-lock holder
# ---------------------------------------------------------------------------


def _hold_lifecycle_lock_only(run_dir: str, repo_key: str, lifecycle_id: str, ready_evt, release_evt) -> None:
    fd = os.open(os.path.join(run_dir, "lifecycle.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready_evt.set()
    release_evt.wait(timeout=30)
    os.close(fd)


def test_skipped_active_via_inconsistent_lock_holder(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_hold_lifecycle_lock_only,
        args=(str(run_dir), harness.identity.repo_key, lifecycle_id, ready_evt, release_evt),
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.SKIPPED_ACTIVE
        assert result.blocked
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.PREPARING.value
    finally:
        release_evt.set()
        proc.join(timeout=10)


# ---------------------------------------------------------------------------
# Publication-failure injection: exact surviving state
# ---------------------------------------------------------------------------


def test_reconciling_write_failure_before_install_is_failed_and_leaves_preparing(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    def _boom(fd, data):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

    monkeypatch.setattr(lf, "write_all_eintr_safe", _boom)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert result.blocked
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value
    assert payload["reconciliation"]["attempts_total"] == 0
    entries = os.listdir(run_dir)
    assert not any(name.startswith(f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-") for name in entries)


def test_reconciling_write_durability_unconfirmed_is_failed_and_leaves_installed_value(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    real_fsync_fd = lf.fsync_fd
    call_count = {"n": 0}

    def _fail_last_fsync(fd):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
        return real_fsync_fd(fd)

    monkeypatch.setattr(lf, "fsync_fd", _fail_last_fsync)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert result.blocked
    # Installed: the RECONCILING content really is on disk, with the
    # increment already durable.
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILING.value
    assert payload["reconciliation"]["attempts_total"] == 1


def test_recovery_after_durability_unconfirmed_reconciling_completes_without_double_increment(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    real_fsync_fd = lf.fsync_fd
    call_count = {"n": 0}

    def _fail_last_fsync(fd):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
        return real_fsync_fd(fd)

    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
        first = harness.reconcile()
    assert first.entries[0].outcome is rc.ReconciliationEntryOutcome.FAILED

    second = harness.reconcile()
    entry = second.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.attempt_number == 1  # not incremented a second time
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILED.value
    assert payload["reconciliation"]["attempts_total"] == 1


def test_reconciled_write_failure_before_install_is_failed_and_leaves_reconciling(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    real_publish = ls.publish_private_file_atomically_at
    call_count = {"n": 0}

    def _fail_second_publish(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced second write failure")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _fail_second_publish)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILING.value
    assert payload["reconciliation"]["attempts_total"] == 1


# ---------------------------------------------------------------------------
# Maintenance-trace failures: all block
# ---------------------------------------------------------------------------


def test_maintenance_directory_uncreatable_blocks(harness, monkeypatch):
    def _boom(parent_fd, components):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

    monkeypatch.setattr(rc, "open_managed_directory_chain", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_maintenance_file_uncreatable_blocks(harness, monkeypatch):
    def _boom(parent_fd, basename, mode):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

    monkeypatch.setattr(rc, "open_private_create_exclusive_at", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_maintenance_directory_fsync_after_creation_blocks(harness, monkeypatch):
    real_fsync = rc.fsync_fd
    call_count = {"n": 0}

    def _fail_first(fd):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced")
        return real_fsync(fd)

    monkeypatch.setattr(rc, "fsync_fd", _fail_first)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_maintenance_event_write_failure_blocks(harness, monkeypatch):
    def _boom(fd, data):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")

    monkeypatch.setattr(rc, "write_all_eintr_safe", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_maintenance_close_failure_blocks(harness, monkeypatch):
    def _boom(fds):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

    # Only fail the maintenance-file close; identified by call count
    # since close_confirmed is shared -- instead patch the writer's own
    # close to force the failure deterministically.
    monkeypatch.setattr(rc._MaintenanceTraceWriter, "close", lambda self: (_ for _ in ()).throw(
        rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "forced close failure")
    ))
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_entry_reconciled_before_trace_finalization_failure_still_blocks_this_pass(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    real_finished = rc._MaintenanceTraceWriter.finished

    def _fail_finished(self, **kwargs):
        raise rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "forced finished failure")

    monkeypatch.setattr(rc._MaintenanceTraceWriter, "finished", _fail_finished)

    with pytest.raises(rc.ReconciliationError):
        harness.reconcile()

    # The entry's own projection already reached RECONCILED durably.
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILED.value

    monkeypatch.setattr(rc._MaintenanceTraceWriter, "finished", real_finished)
    second = harness.reconcile()
    assert second.entries[0].outcome is rc.ReconciliationEntryOutcome.SKIPPED_TERMINAL
    assert not second.blocked


# ---------------------------------------------------------------------------
# Lock / descriptor cleanup failures: blocking, causal chaining preserved
# ---------------------------------------------------------------------------


def test_lifecycle_lock_release_failure_blocks_and_chains(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    real_release = sl.LockHandle.release
    calls = {"n": 0}

    def _fail_lifecycle_release(self):
        calls["n"] += 1
        if self.scope.kind is sl.LockKind.LIFECYCLE:
            raise sl.LockError(sl.LockFailure.RELEASE_UNCONFIRMED, "forced release failure")
        return real_release(self)

    monkeypatch.setattr(sl.LockHandle, "release", _fail_lifecycle_release)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE
    assert excinfo.value.__cause__ is not None


def test_run_directory_close_failure_blocks_and_chains(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    def _boom(fds):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced close failure")

    monkeypatch.setattr(rc, "close_confirmed", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# End-to-end via prepare_lifecycle(): empty repo passes cleanly
# ---------------------------------------------------------------------------


def test_prepare_lifecycle_on_fresh_repo_reconciles_trivially(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    lease.close()


def test_prepare_lifecycle_reconciles_prior_closed_run_then_proceeds(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease1 = ls.prepare_lifecycle(str(repo), run_id="run-1")
    prior_id = lease1.lifecycle_id
    repo_key = lease1.repo_key
    lease1.close()  # projection remains durably PREPARING, absent-shape

    lease2 = ls.prepare_lifecycle(str(repo), run_id="run-2")
    try:
        assert lease2.lifecycle_id != prior_id
    finally:
        lease2.close()

    state_dir = tmp_path / "state-root"
    prior_payload = lf.canonical_json_loads_strict(
        (state_dir / "repos" / repo_key / "runs" / prior_id / ls.LIFECYCLE_JSON_FILENAME).read_bytes(),
        max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES,
    )
    assert prior_payload["state"] == ls.LifecycleState.RECONCILED.value


# ---------------------------------------------------------------------------
# Real SIGKILL + real Docker/Git/ref inspection
# ---------------------------------------------------------------------------

REQUIRE_DOCKER = os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1"


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _prepare_and_sigkill(repo_path: str, state_dir: str, run_id: str, out_path: str) -> None:
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_child

    lease = ls_child.prepare_lifecycle(repo_path, run_id=run_id)
    with open(out_path, "w") as f:
        f.write(lease.lifecycle_id)
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.skipif(
    not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available"
)
def test_real_sigkill_then_reconcile_with_real_docker_git_ref_inspection(tmp_path):
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    repo = _make_repo(tmp_path)
    state_dir = tmp_path / "state-root"
    out_path = tmp_path / "lifecycle_id.txt"

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_prepare_and_sigkill, args=(str(repo), str(state_dir), "victim", str(out_path))
    )
    proc.start()
    proc.join(timeout=30)
    assert not proc.is_alive()
    for _ in range(100):
        if out_path.exists():
            break
        time.sleep(0.05)
    assert out_path.exists()
    victim_id = out_path.read_text()

    os.environ["CODEAGENT_STATE_DIR"] = str(state_dir)
    # Deliberately no `importlib.reload` here: reloading `lifecycle_store`
    # in this process while `reconciliation` (already imported at this
    # test module's top level) keeps its own old-bound references would
    # desynchronize enum class identity across the two modules -- the
    # exact hazard already documented in this project's history for
    # Slice 3A-1. `ls` (imported once, at module load) is used as-is.
    lease = ls.prepare_lifecycle(str(repo), run_id="fresh")
    try:
        assert lease.lifecycle_id != victim_id
    finally:
        lease.close()

    payload = lf.canonical_json_loads_strict(
        (state_dir / "repos" / lease.repo_key / "runs" / victim_id / ls.LIFECYCLE_JSON_FILENAME).read_bytes(),
        max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES,
    )
    assert payload["state"] == ls.LifecycleState.RECONCILED.value
    del os.environ["CODEAGENT_STATE_DIR"]


# ---------------------------------------------------------------------------
# Correction pass, finding 1: bounded external inspection
# ---------------------------------------------------------------------------


def test_drain_bounded_overflow_confirms_termination():
    process = subprocess.Popen(
        ["python3", "-c", "import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(5)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5.0
    with pytest.raises(rc._BoundedReadFailure):
        rc._drain_bounded(process, deadline=deadline, limit=10)
    rc._kill_and_confirm(process, deadline=deadline)
    assert process.poll() is not None


def test_drain_bounded_timeout_confirms_termination():
    process = subprocess.Popen(["sleep", "5"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 0.2
    with pytest.raises(rc._BoundedReadFailure):
        rc._drain_bounded(process, deadline=deadline, limit=1_000_000)
    rc._kill_and_confirm(process, deadline=deadline)
    assert process.poll() is not None


def test_docker_ps_all_names_launch_failure_is_docker_listing_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no docker")

    monkeypatch.setattr(rc.subprocess, "Popen", _boom)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_names()


def test_worktree_registered_paths_bounded_overflow_is_git_listing_error(harness, monkeypatch):
    monkeypatch.setattr(rc, "_WORKTREE_LISTING_MAX_BYTES", 1)
    with pytest.raises(rc._GitWorktreeListingError):
        rc._worktree_registered_paths(harness.context.working_tree_root)


def test_worktree_registered_paths_parses_path_containing_newline(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEAGENT_STATE_DIR", raising=False)
    repo = _make_repo(tmp_path)
    odd_parent = tmp_path / "odd\ndir"
    odd_parent.mkdir()
    wt_path = odd_parent / "wt1"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt_path), "-b", "odd-branch"],
        check=True,
        capture_output=True,
    )
    try:
        paths = rc._worktree_registered_paths(str(repo))
        assert os.path.normpath(str(wt_path)) in paths
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
            check=True,
            capture_output=True,
        )


def test_reconciliation_unaffected_by_unrelated_newline_worktree_path(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    parent = harness.repo.parent / "odd\ndir"
    parent.mkdir()
    wt_path = parent / "wt1"
    subprocess.run(
        ["git", "-C", str(harness.repo), "worktree", "add", str(wt_path), "-b", "odd-branch"],
        check=True,
        capture_output=True,
    )
    try:
        result = harness.reconcile()
        assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.RECONCILED
    finally:
        subprocess.run(
            ["git", "-C", str(harness.repo), "worktree", "remove", "--force", str(wt_path)],
            check=True,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Correction pass, finding 2: inner-entry validation and trace evidence
# ---------------------------------------------------------------------------


def test_symlinked_recognized_lifecycle_lock_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = dataclasses.replace(_initial_projection(harness, lifecycle_id), state=ls.LifecycleState.RECONCILED)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    target = run_dir.parent.parent / "hostile-lock-target"
    target.write_bytes(b"")
    (run_dir / "lifecycle.lock").symlink_to(target)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked


def test_wrong_type_recognized_inner_entry_is_refused(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    (run_dir / "lifecycle.lock").mkdir()

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_inner_entry_inspection_failure_is_substrate_unavailable(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    real_lstat = os.lstat

    def _fake_lstat(name, *, dir_fd=None):
        if name == "lifecycle.json" and dir_fd is not None:
            raise OSError(errno.EIO, "forced")
        return real_lstat(name, dir_fd=dir_fd)

    monkeypatch.setattr(rc.os, "lstat", _fake_lstat)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE


def test_check_inner_entries_listing_failure_is_substrate_unavailable(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    fd = os.open(str(run_dir), os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)  # deliberately closed: any use of this fd now genuinely fails

    result = rc._check_inner_entries(fd)
    assert result.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE


def test_trace_records_recognized_temp_leftover_explicitly(harness):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)
    leftover = run_dir / f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-0123456789abcdef"
    leftover.write_bytes(b"partial")
    leftover.chmod(0o600)

    result = harness.reconcile()
    trace_path = harness.repo_dir() / "maintenance" / f"{result.maintenance_id}.jsonl"
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    entry_events = [e for e in events if e["event_type"] == "ReconciliationEntryRecorded"]
    assert len(entry_events) == 1
    assert entry_events[0]["has_recognized_temp_leftover"] is True


# ---------------------------------------------------------------------------
# Correction pass, finding 3: maintenance-trace descriptor ownership
# ---------------------------------------------------------------------------


def test_maintenance_dir_close_failure_after_success_still_closes_trace_file(harness, monkeypatch):
    repo_dir_fd = harness.state_root.open_repo_dir(harness.identity.repo_key)
    real_close = rc.close_confirmed
    calls: list[list[int]] = []

    def _fake(fds):
        calls.append(list(fds))
        if len(calls) == 1:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced maintenance_dir_fd close failure")
        return real_close(fds)

    monkeypatch.setattr(rc, "close_confirmed", _fake)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        rc._open_maintenance_trace(harness.state_root, repo_dir_fd, harness.identity.repo_key)
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE
    assert len(calls) == 2  # maintenance_dir_fd attempted (failed); the trace file fd attempted next (succeeded)
    real_close([repo_dir_fd])


def test_maintenance_dir_and_trace_file_close_both_fail_reports_compound_failure(harness, monkeypatch):
    repo_dir_fd = harness.state_root.open_repo_dir(harness.identity.repo_key)

    def _always_fail(fds):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

    monkeypatch.setattr(rc, "close_confirmed", _always_fail)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        rc._open_maintenance_trace(harness.state_root, repo_dir_fd, harness.identity.repo_key)
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE
    assert "neither" in excinfo.value.message
    os.close(repo_dir_fd)


def test_trace_file_close_failure_during_directory_fsync_handling(harness, monkeypatch):
    repo_dir_fd = harness.state_root.open_repo_dir(harness.identity.repo_key)
    real_close = rc.close_confirmed
    calls: list[list[int]] = []

    def _fail_fsync(fd):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced")

    def _fake_close(fds):
        calls.append(list(fds))
        if len(calls) == 1:
            # The inner close of the trace file itself, attempted while
            # handling the fsync failure above, also fails.
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")
        return real_close(fds)  # the outer maintenance_dir_fd close succeeds

    monkeypatch.setattr(rc, "fsync_fd", _fail_fsync)
    monkeypatch.setattr(rc, "close_confirmed", _fake_close)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        rc._open_maintenance_trace(harness.state_root, repo_dir_fd, harness.identity.repo_key)
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE
    assert "closed after a directory-fsync failure" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
    assert excinfo.value.__cause__.reason is lf.LifecycleFsFailure.FSYNC_FAILED
    assert len(calls) == 2


def test_repository_directory_open_failure_substrate_unavailable(harness, monkeypatch):
    def _boom(repo_key):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

    monkeypatch.setattr(harness.state_root, "open_repo_dir", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE


def test_repository_directory_open_failure_refused(harness, monkeypatch):
    def _boom(repo_key):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SYMLINK_REFUSED, "forced")

    monkeypatch.setattr(harness.state_root, "open_repo_dir", _boom)
    with pytest.raises(rc.ReconciliationError) as excinfo:
        harness.reconcile()
    assert excinfo.value.reason is rc.ReconciliationFailure.REFUSED


# ---------------------------------------------------------------------------
# Correction pass, finding 4: REFUSED vs SUBSTRATE_UNAVAILABLE
# ---------------------------------------------------------------------------


def test_lifecycle_json_missing_is_refused_not_substrate_unavailable(harness):
    lifecycle_id = "a" * 32
    _seed_run_dir(harness, lifecycle_id, projection=None)
    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.REFUSED


def test_lifecycle_json_open_failure_with_other_errno_is_substrate_unavailable(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    real_open = os.open

    def _fake_open(path, flags, *args, **kwargs):
        if path == ls.LIFECYCLE_JSON_FILENAME and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EIO, "forced I/O error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ls.os, "open", _fake_open)
    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE


def test_process_entry_directory_open_substrate_unavailable(monkeypatch):
    def _boom(runs_fd, components):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

    monkeypatch.setattr(rc, "open_existing_directory_chain_if_present", _boom)
    result = rc._process_entry(
        runs_fd=0, lifecycle_id="a" * 32, state_root=None, identity=None, context=None
    )
    assert result.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE


def test_process_entry_directory_open_refused(monkeypatch):
    def _boom(runs_fd, components):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.SYMLINK_REFUSED, "forced")

    monkeypatch.setattr(rc, "open_existing_directory_chain_if_present", _boom)
    result = rc._process_entry(
        runs_fd=0, lifecycle_id="a" * 32, state_root=None, identity=None, context=None
    )
    assert result.outcome is rc.ReconciliationEntryOutcome.REFUSED


# ---------------------------------------------------------------------------
# Static proof: no removal call reachable from this module
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_NAMES = {"remove", "delete", "rmtree", "unlink", "rmdir"}
_FORBIDDEN_BARE_STRINGS = {"rm", "kill", "stop", "remove", "delete", "rmtree", "unlink", "rmdir"}


def test_no_removal_call_reachable_from_reconciliation_module():
    import codeagent.reconciliation as module

    source = open(module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            assert name not in _FORBIDDEN_CALL_NAMES, f"forbidden removal-like call reachable: {name}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.strip().lower() not in _FORBIDDEN_BARE_STRINGS, (
                f"forbidden removal-like literal reachable: {node.value!r}"
            )
