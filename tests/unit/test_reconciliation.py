"""Tests for `codeagent.reconciliation` (Milestone 3 Slices 3B-1 and
3B-5, ADR 0004 Amendment 1 section 10, Amendment 2, Amendment 5)."""

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

from codeagent._bounded_subprocess import BoundedProcessError, BoundedProcessFailure, BoundedProcessResult

from codeagent import _lifecycle_fs as lf
from codeagent import checkpoint_ref as cr
from codeagent import checkpoint_session as cs
from codeagent import executor as ex
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


def _empty_listing():
    return {}, {}


def _listing(name_to_id: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    return dict(name_to_id), {v: k for k, v in name_to_id.items()}


def _owned_labels(*, state_root_id: str, lifecycle_id: str, role: str) -> dict[str, str]:
    return {
        rc.CONTAINER_LABEL_SCHEMA: "1",
        rc.CONTAINER_LABEL_STATE_ROOT_ID: state_root_id,
        rc.CONTAINER_LABEL_ID: lifecycle_id,
        rc.CONTAINER_LABEL_ROLE: role,
    }


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

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _boom)
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

    monkeypatch.setattr(
        rc, "_docker_ps_all_id_name_pairs", lambda: _listing({f"codeagent-baseline-{lifecycle_id}": "0" * 64})
    )

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
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
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

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
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

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _boom)

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

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
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

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _boom)
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
#
# Slice 3B-4: the bounded-drain/kill-and-confirm primitives this
# section used to test directly (`rc._drain_bounded`/`rc._kill_and_
# confirm`) were extracted into the shared `codeagent._bounded_
# subprocess` module and are now tested directly there
# (`tests/unit/test_bounded_subprocess.py`) — this module no longer
# has a private copy to test. `_docker_ps_all_id_name_pairs()`'s own
# launch-failure behavior is still covered below, now through the
# shared module's `subprocess.Popen`.
# ---------------------------------------------------------------------------


def test_docker_ps_all_id_name_pairs_launch_failure_is_docker_listing_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no docker")

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", _boom)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


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
# Static proof: no *filesystem* removal call reachable from this module
#
# Slice 3B-5 deliberately introduces one sanctioned Docker container
# removal path (`_remove_and_confirm_absent`'s own bounded `docker rm
# --force` subprocess call) -- the original 3B-1 test asserting *zero*
# removal-shaped calls or literals anywhere in the module is therefore
# obsolete by design, not a regression; see ENGINEERING_LOG.md's Slice
# 3B-5 entry. This narrower replacement keeps the real invariant that
# still holds: worktree and checkpoint-ref removal remain completely
# out of scope, so no filesystem-removal primitive (`shutil.rmtree`,
# `os.remove`, `os.unlink`, `os.rmdir`) may ever be reachable from this
# module, and the only permitted removal-shaped subprocess argv is the
# exact `["docker", "rm", "--force", ...]` invocation inside
# `_remove_and_confirm_absent`.
# ---------------------------------------------------------------------------

_FORBIDDEN_FS_REMOVAL_CALL_NAMES = {"rmtree", "unlink", "rmdir", "remove"}


def test_no_filesystem_removal_call_reachable_from_reconciliation_module():
    import codeagent.reconciliation as module

    source = open(module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _FORBIDDEN_FS_REMOVAL_CALL_NAMES:
            raise AssertionError(f"forbidden filesystem-removal call reachable: {name}")


def test_the_only_rm_shaped_argv_is_the_sanctioned_docker_rm_call():
    import codeagent.reconciliation as module

    source = open(module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    matches = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 3:
            continue
        leading = [elt.value for elt in node.elts[:3] if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        if leading == ["docker", "rm", "--force"]:
            matches += 1
    assert matches == 1


# ---------------------------------------------------------------------------
# Slice 3B-5: container reconciliation and removal (ADR 0004 Amendment 5)
# ---------------------------------------------------------------------------


def _with_container(projection, *, role: str, intent, id):
    attr = ls.ContainerAttribution(intent=intent, id=id)
    if role == "baseline":
        return dataclasses.replace(projection, baseline=attr)
    return dataclasses.replace(projection, verification=attr)


def test_creating_container_name_absent_reconciles_directly_to_absent(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.baseline_id is None  # never a live candidate observed
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}


def test_creating_container_owned_is_removed_and_reconciled(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    name = f"codeagent-baseline-{lifecycle_id}"
    live_id = "1" * 64
    listing_calls = {"n": 0}
    rm_calls: list[list[str]] = []

    def _fake_listing():
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({name: live_id})
        return _empty_listing()  # confirmed absent after removal

    def _fake_inspect(candidate_id):
        assert candidate_id == live_id
        return rc._InspectOwnership(
            id=live_id,
            name=name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"),
        )

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            rm_calls.append(argv)
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.baseline_id == live_id
    assert rm_calls == [["docker", "rm", "--force", live_id]]
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}


def test_present_container_owned_goes_through_removing_then_absent(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    persisted_id = "2" * 64
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    name = f"codeagent-verification-{lifecycle_id}"
    listing_calls = {"n": 0}

    def _fake_listing():
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({name: persisted_id})
        return _empty_listing()

    def _fake_inspect(candidate_id):
        assert candidate_id == persisted_id
        return rc._InspectOwnership(
            id=persisted_id,
            name=name,
            labels=_owned_labels(
                state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="verification"
            ),
        )

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.verification_id == persisted_id
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["verification"] == {"intent": "absent", "id": None}


def test_present_container_already_confirmed_absent_needs_no_actual_rm_call(harness, monkeypatch):
    """A dead owner crashed after `docker rm` genuinely succeeded but
    before ever writing `removing`/`absent`. Reconciliation must still
    route through the write-ahead `removing` edge (no direct
    PRESENT->ABSENT edge exists) but must never issue a real `docker
    rm` for a container it never observed live."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    persisted_id = "3" * 64
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    rm_calls: list[list[str]] = []
    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            rm_calls.append(argv)
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    # This role was never positively observed live at any point (the
    # persisted id was already confirmed absent by the very first
    # listing), so the retrospective trace correctly reports no
    # observed id -- distinct from `removal_id` (the persisted_id),
    # which is still what the write-ahead path targets (correction
    # pass finding 1: `observed_id` vs `removal_id` are not the same).
    assert entry.baseline_id is None
    assert rm_calls == []  # never actually called docker rm
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}


def test_missing_labels_is_ownership_conflict_refused_zero_mutation(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    name = f"codeagent-baseline-{lifecycle_id}"
    live_id = "4" * 64

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _listing({name: live_id}))
    monkeypatch.setattr(
        rc, "_docker_inspect_ownership", lambda candidate_id: rc._InspectOwnership(id=live_id, name=name, labels={})
    )

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value  # completely untouched


def test_id_under_different_name_is_conflict_refused(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    persisted_id = "5" * 64
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    # The persisted id is live, but registered under a different name.
    monkeypatch.setattr(
        rc, "_docker_ps_all_id_name_pairs", lambda: _listing({"some-other-container": persisted_id})
    )

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_one_role_conflict_leaves_the_other_roles_writes_untouched(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    verification_live_id = "6" * 64

    # baseline: confirmed absent (would resolve cleanly on its own).
    # verification: a conflicting, unlabeled live container.
    monkeypatch.setattr(
        rc,
        "_docker_ps_all_id_name_pairs",
        lambda: _listing({verification_name: verification_live_id}),
    )
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda candidate_id: rc._InspectOwnership(id=verification_live_id, name=verification_name, labels={}),
    )

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    payload = _read_projection_dict(run_dir)
    # Neither role was mutated, even though baseline's own decision was clean.
    assert payload["containers"]["baseline"] == {"intent": "creating", "id": None}
    assert payload["containers"]["verification"] == {"intent": "creating", "id": None}
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_one_role_conflict_causes_zero_removal_calls_for_either_role(harness, monkeypatch):
    """Extends the zero-mutation test above: a conflict on one role must
    also never issue a `docker rm` for the *other*, clean role."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    baseline_live_id = "d" * 64
    verification_live_id = "6" * 64
    rm_calls: list[list[str]] = []

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            rm_calls.append(argv)
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    # baseline: a genuinely owned, removable container (would be
    # legitimately removed on its own). verification: an unlabeled
    # conflicting container.
    monkeypatch.setattr(
        rc,
        "_docker_ps_all_id_name_pairs",
        lambda: _listing({baseline_name: baseline_live_id, verification_name: verification_live_id}),
    )

    def _fake_inspect(candidate_id):
        if candidate_id == baseline_live_id:
            return rc._InspectOwnership(
                id=baseline_live_id,
                name=baseline_name,
                labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"),
            )
        return rc._InspectOwnership(id=verification_live_id, name=verification_name, labels={})

    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert rm_calls == []  # baseline's own clean removal never happened


def test_call_order_both_decisions_before_any_write_ahead_publish(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    baseline_live_id = "1" * 64
    verification_live_id = "2" * 64
    call_log: list[str] = []
    listing_calls = {"n": 0}

    def _fake_listing():
        call_log.append("listing")
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({baseline_name: baseline_live_id, verification_name: verification_live_id})
        return _empty_listing()  # every post-removal re-observation confirms absence

    def _fake_inspect(candidate_id):
        call_log.append(f"inspect:{candidate_id}")
        role = "baseline" if candidate_id == baseline_live_id else "verification"
        name = baseline_name if candidate_id == baseline_live_id else verification_name
        return rc._InspectOwnership(
            id=candidate_id,
            name=name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role=role),
        )

    real_publish = ls.publish_private_file_atomically_at

    def _logging_publish(*args, **kwargs):
        call_log.append("publish")
        return real_publish(*args, **kwargs)

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            call_log.append(f"rm:{argv[3]}")
            return BoundedProcessResult(returncode=0, stdout=b"")
        if argv[:2] == ["docker", "ps"]:
            return real_run_bounded_stdout(argv, **kwargs)
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _logging_publish)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.RECONCILED

    # Both roles' candidates are inspected (decisions fully computed)
    # before either role's first write-ahead publish; both roles'
    # write-ahead publishes precede baseline's own removal; baseline's
    # own `docker rm` precedes verification's.
    first_publish_index = call_log.index("publish")
    inspect_indices = [i for i, c in enumerate(call_log) if c.startswith("inspect:")]
    assert len(inspect_indices) == 2
    assert max(inspect_indices) < first_publish_index

    rm_indices = [i for i, c in enumerate(call_log) if c.startswith("rm:")]
    assert len(rm_indices) == 2
    assert call_log[rm_indices[0]] == f"rm:{baseline_live_id}"
    assert call_log[rm_indices[1]] == f"rm:{verification_live_id}"
    # Every write-ahead publish (2, one per role's REMOVING transition)
    # happens before the first removal.
    publish_indices = [i for i, c in enumerate(call_log) if c == "publish"]
    assert len([p for p in publish_indices if p < rm_indices[0]]) >= 2


def test_unresolved_baseline_removal_blocks_verification_removal(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    baseline_live_id = "3" * 64
    verification_live_id = "4" * 64
    rm_calls: list[str] = []

    def _fake_inspect(candidate_id):
        role = "baseline" if candidate_id == baseline_live_id else "verification"
        name = baseline_name if candidate_id == baseline_live_id else verification_name
        return rc._InspectOwnership(
            id=candidate_id,
            name=name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role=role),
        )

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            rm_calls.append(argv[3])
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    # The post-removal listing always reports baseline's container as
    # still present (removal never actually confirms), so baseline's
    # own resolution never reaches absence this pass.
    monkeypatch.setattr(
        rc,
        "_docker_ps_all_id_name_pairs",
        lambda: _listing({baseline_name: baseline_live_id, verification_name: verification_live_id}),
    )
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert rm_calls == [baseline_live_id]  # verification's own removal never attempted
    # "baseline removal failure after both write-ahead publications"
    # (correction pass finding 2): both roles' already-observed ids
    # must be reported, not just baseline's own failing one.
    assert entry.baseline_id == baseline_live_id
    assert entry.verification_id == verification_live_id
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "removing", "id": baseline_live_id}
    # verification's own write-ahead did happen (published before any rm).
    assert payload["containers"]["verification"] == {"intent": "removing", "id": verification_live_id}


# ---------------------------------------------------------------------------
# Correction pass finding 1: `_ContainerDecision.observed_id` (retrospective
# trace evidence) vs `removal_id` (write authorization) are separate
# fields with deliberately distinct semantics -- direct, fast unit tests
# against `_classify_container` itself, independent of the full harness.
# ---------------------------------------------------------------------------

_SRID = "s" * 16
_LCID = "l" * 32


def test_classify_different_live_id_at_expected_name_reports_it_as_observed(monkeypatch):
    persisted_id = "1" * 64
    live_id = "2" * 64
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": live_id},
        id_to_name={live_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.REFUSED
    assert decision.observed_id == live_id  # the id genuinely occupying the name
    assert decision.removal_id is None  # never authorized


def test_classify_persisted_id_live_under_a_different_name_reports_it_as_observed(monkeypatch):
    persisted_id = "3" * 64
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.REMOVING, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"some-other-name": persisted_id},
        id_to_name={persisted_id: "some-other-name"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.REFUSED
    assert decision.observed_id == persisted_id  # confirmed live, just misplaced
    assert decision.removal_id is None


def test_classify_both_a_different_id_at_name_and_persisted_id_elsewhere(monkeypatch):
    persisted_id = "4" * 64
    different_live_id = "5" * 64
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": different_live_id, "some-other-name": persisted_id},
        id_to_name={different_live_id: "codeagent-baseline-x", persisted_id: "some-other-name"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.REFUSED
    # Precedence: whatever occupies the recomputed name is reported,
    # never a fabricated or unobserved value either way.
    assert decision.observed_id == different_live_id
    assert decision.removal_id is None


def test_classify_missing_labels_creating_role(monkeypatch):
    live_id = "6" * 64
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.CREATING, id=None)
    monkeypatch.setattr(
        rc, "_docker_inspect_ownership", lambda cid: rc._InspectOwnership(id=live_id, name="codeagent-baseline-x", labels={})
    )
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": live_id},
        id_to_name={live_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.REFUSED
    assert decision.observed_id == live_id
    assert decision.removal_id is None


def test_classify_wrong_labels_present_role(monkeypatch):
    persisted_id = "7" * 64
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=persisted_id, name="codeagent-baseline-x", labels={"codeagent.lifecycle.schema": "wrong"}
        ),
    )
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": persisted_id},
        id_to_name={persisted_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.REFUSED
    assert decision.observed_id == persisted_id
    assert decision.removal_id is None


def test_classify_inspect_failure_after_valid_listing_creating_role(monkeypatch):
    live_id = "8" * 64

    def _boom(cid):
        raise rc._DockerInspectError("boom")

    monkeypatch.setattr(rc, "_docker_inspect_ownership", _boom)
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.CREATING, id=None)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": live_id},
        id_to_name={live_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.SUBSTRATE_UNAVAILABLE
    assert decision.observed_id == live_id
    assert decision.removal_id is None


def test_classify_inspect_failure_after_valid_listing_present_role(monkeypatch):
    persisted_id = "9" * 64

    def _boom(cid):
        raise rc._DockerInspectError("boom")

    monkeypatch.setattr(rc, "_docker_inspect_ownership", _boom)
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": persisted_id},
        id_to_name={persisted_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.SUBSTRATE_UNAVAILABLE
    assert decision.observed_id == persisted_id
    assert decision.removal_id is None


def test_classify_already_absent_noop_reports_no_observed_and_no_removal_id():
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.ABSENT, id=None)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={},
        id_to_name={},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.NOOP
    assert decision.observed_id is None
    assert decision.removal_id is None


def test_classify_persisted_but_now_absent_reports_no_observed_id_but_sets_removal_id():
    persisted_id = "a" * 64
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id=persisted_id)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={},
        id_to_name={},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.CONFIRMED_ABSENT
    # Never positively observed live -- but the write-ahead path still
    # needs the persisted id to reach `removing` before `absent` (no
    # direct present->absent edge exists).
    assert decision.observed_id is None
    assert decision.removal_id == persisted_id


def test_classify_owned_remove_sets_both_ids_equal(monkeypatch):
    live_id = "b" * 64
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=live_id,
            name="codeagent-baseline-x",
            labels=_owned_labels(state_root_id=_SRID, lifecycle_id=_LCID, role="baseline"),
        ),
    )
    attribution = ls.ContainerAttribution(intent=ls.ContainerIntent.CREATING, id=None)
    decision = rc._classify_container(
        attribution=attribution,
        role="baseline",
        expected_name="codeagent-baseline-x",
        name_to_id={"codeagent-baseline-x": live_id},
        id_to_name={live_id: "codeagent-baseline-x"},
        state_root_id=_SRID,
        lifecycle_id=_LCID,
    )
    assert decision.outcome is rc._ContainerDecisionOutcome.OWNED_REMOVE
    assert decision.observed_id == live_id
    assert decision.removal_id == live_id


# ---------------------------------------------------------------------------
# Correction pass finding 2: both retrospective ids must be derived from
# both completed decisions immediately, before the first publication --
# these fail under the pre-correction implementation (which populated
# `observed_ids` progressively, inside the write-ahead loop, so a
# baseline-side failure before verification's own turn silently omitted
# verification's already-known id).
# ---------------------------------------------------------------------------


def test_verification_id_preserved_when_baseline_direct_absence_write_fails(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    verification_live_id = "c" * 64
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    verification_name = f"codeagent-verification-{lifecycle_id}"
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _listing({verification_name: verification_live_id}))
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=verification_live_id,
            name=verification_name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="verification"),
        ),
    )
    # baseline resolves via the direct-to-absent path (nothing live at
    # its name); force *that* specific write to fail.
    real_publish = ls.publish_private_file_atomically_at
    call_count = {"n": 0}

    def _fail_first(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _fail_first)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    # verification's own id, already known from classification, must
    # not be silently omitted just because baseline's write failed
    # first.
    assert entry.baseline_id is None  # baseline itself had nothing observed
    assert entry.verification_id == verification_live_id


def test_baseline_id_preserved_when_verification_write_ahead_fails_after_baseline_publishes(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    baseline_live_id = "d" * 64
    verification_live_id = "e" * 64
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    monkeypatch.setattr(
        rc,
        "_docker_ps_all_id_name_pairs",
        lambda: _listing({baseline_name: baseline_live_id, verification_name: verification_live_id}),
    )

    def _fake_inspect(cid):
        role = "baseline" if cid == baseline_live_id else "verification"
        name = baseline_name if cid == baseline_live_id else verification_name
        return rc._InspectOwnership(
            id=cid, name=name, labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role=role)
        )

    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)

    # baseline's own write-ahead (the 1st publish) succeeds;
    # verification's own write-ahead (the 2nd publish) fails.
    real_publish = ls.publish_private_file_atomically_at
    call_count = {"n": 0}

    def _fail_second(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _fail_second)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert entry.baseline_id == baseline_live_id
    assert entry.verification_id == verification_live_id


def test_ids_preserved_through_absent_collapse_failure(harness, monkeypatch):
    """"absent-collapse failure" (correction pass finding 2): the write
    that collapses a role's own `removing(id) -> absent` after a
    confirmed removal fails."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    verification_live_id = "f" * 64
    projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    verification_name = f"codeagent-verification-{lifecycle_id}"
    listing_calls = {"n": 0}

    def _fake_listing():
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({verification_name: verification_live_id})
        return _empty_listing()  # post-removal: confirmed absent

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=verification_live_id,
            name=verification_name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="verification"),
        ),
    )
    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    # baseline's own write-ahead is a direct-to-absent NOOP write (1st
    # publish); verification's own write-ahead REMOVING write is the
    # 2nd; verification's own confirmed-absent collapse write is the
    # 3rd -- fail exactly that one.
    real_publish = ls.publish_private_file_atomically_at
    call_count = {"n": 0}

    def _fail_third(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _fail_third)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert entry.baseline_id is None  # baseline was never live
    assert entry.verification_id == verification_live_id


def test_ids_preserved_through_final_state_collapse_failure(harness, monkeypatch):
    """"final state-collapse failure" (correction pass finding 2): the
    terminal `RECONCILING -> RECONCILED` write itself fails after every
    container is already durably absent."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    baseline_live_id = "1" * 64
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    listing_calls = {"n": 0}

    def _fake_listing():
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({baseline_name: baseline_live_id})
        return _empty_listing()

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=baseline_live_id,
            name=baseline_name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"),
        ),
    )
    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    real_publish = ls.publish_private_file_atomically_at
    call_count = {"n": 0}

    def _fail_last(*args, **kwargs):
        call_count["n"] += 1
        # 1: REMOVING write-ahead, 2: absent collapse, 3: final RECONCILED.
        if call_count["n"] == 3:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _fail_last)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert entry.baseline_id == baseline_live_id
    assert entry.verification_id is None
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILING.value  # never reached RECONCILED


def test_resumed_reconciling_with_matching_container_attribution_is_a_true_noop(harness, monkeypatch):
    """A dead owner already durably wrote `RECONCILING` with baseline
    already `removing(id)`, matching what this pass would compute
    anyway. The reconciler-owned writer must not re-publish (no-op),
    but must still finish confirming absence and collapsing to
    RECONCILED."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    persisted_id = "7" * 64
    projection = dataclasses.replace(
        projection,
        state=ls.LifecycleState.RECONCILING,
        reconciliation=ls.ReconciliationSummary(attempts_total=1, recent_failures=()),
    )
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.REMOVING, id=persisted_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    write_calls = {"n": 0}
    real_publish = ls.publish_private_file_atomically_at

    def _counting_publish(*args, **kwargs):
        write_calls["n"] += 1
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _counting_publish)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.attempt_number == 1  # never re-incremented
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILED.value
    assert payload["reconciliation"]["attempts_total"] == 1
    # Exactly one write for baseline's REMOVING->ABSENT collapse, plus
    # one for the final RECONCILED collapse -- the already-matching
    # REMOVING(id) write-ahead step itself was skipped as a true no-op.
    assert write_calls["n"] == 2


@pytest.mark.parametrize("state", [ls.LifecycleState.ACTIVE, ls.LifecycleState.CLEANING])
def test_active_and_cleaning_states_are_reconciliation_eligible(harness, monkeypatch, state):
    lifecycle_id = "a" * 32
    projection = dataclasses.replace(_initial_projection(harness, lifecycle_id), state=state)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.RECONCILED.value


def test_trace_records_container_id_and_retains_it_after_removal(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    _seed_run_dir(harness, lifecycle_id, projection)

    name = f"codeagent-baseline-{lifecycle_id}"
    live_id = "8" * 64
    listing_calls = {"n": 0}

    def _fake_listing():
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return _listing({name: live_id})
        return _empty_listing()

    def _fake_inspect(candidate_id):
        return rc._InspectOwnership(
            id=live_id,
            name=name,
            labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"),
        )

    real_run_bounded_stdout = rc.run_bounded_stdout

    def _fake_run_bounded_stdout(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            return BoundedProcessResult(returncode=0, stdout=b"")
        return real_run_bounded_stdout(argv, **kwargs)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _fake_listing)
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _fake_inspect)
    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run_bounded_stdout)

    result = harness.reconcile()
    trace_path = harness.repo_dir() / "maintenance" / f"{result.maintenance_id}.jsonl"
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    entry_events = [e for e in events if e["event_type"] == "ReconciliationEntryRecorded"]
    assert len(entry_events) == 1
    assert entry_events[0]["containers"]["baseline"]["id"] == live_id
    assert entry_events[0]["containers"]["baseline"]["confirmed_absent"] is True
    assert entry_events[0]["containers"]["verification"]["id"] is None


def test_malformed_listing_row_is_substrate_unavailable(harness, monkeypatch):
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    def _boom():
        raise rc._DockerListingError("malformed row")

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _boom)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == ls.LifecycleState.PREPARING.value


def test_docker_ps_all_id_name_pairs_parses_strict_two_field_rows(monkeypatch):
    live_id = "9" * 64
    text = f"{live_id}\tsome-name\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())

    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    name_to_id, id_to_name = rc._docker_ps_all_id_name_pairs()
    assert name_to_id == {"some-name": live_id}
    assert id_to_name == {live_id: "some-name"}


def test_docker_ps_all_id_name_pairs_rejects_duplicate_id(monkeypatch):
    live_id = "a" * 64
    text = f"{live_id}\tname-one\n{live_id}\tname-two\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())

    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_inspect_ownership_parses_strict_three_field_row(monkeypatch):
    candidate_id = "b" * 64
    text = f'{candidate_id}\t/codeagent-baseline-{"c" * 32}\t{{"codeagent.lifecycle.schema":"1"}}\n'
    result = BoundedProcessResult(returncode=0, stdout=text.encode())

    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    proof = rc._docker_inspect_ownership(candidate_id)
    assert proof.id == candidate_id
    assert proof.name == f"codeagent-baseline-{'c' * 32}"  # leading '/' stripped
    assert proof.labels == {"codeagent.lifecycle.schema": "1"}


def test_docker_inspect_ownership_nonzero_exit_is_never_confirmed_absence(monkeypatch):
    result = BoundedProcessResult(returncode=1, stdout=b"")

    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership("d" * 64)


# ---------------------------------------------------------------------------
# Correction pass finding 3: explicit regression tests for the inspect
# name grammar (exactly one leading '/') and the strict LF-only listing
# split, both fixed in the prior correction pass but previously only
# implicitly covered by the "happy path" tests above.
# ---------------------------------------------------------------------------


def test_docker_inspect_ownership_rejects_name_with_no_leading_slash(monkeypatch):
    candidate_id = "1" * 64
    text = f"{candidate_id}\tcodeagent-baseline-x\t{{}}\n"  # missing the leading '/'
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_inspect_ownership_rejects_name_with_two_leading_slashes(monkeypatch):
    candidate_id = "2" * 64
    text = f"{candidate_id}\t//codeagent-baseline-x\t{{}}\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_inspect_ownership_rejects_crlf(monkeypatch):
    candidate_id = "3" * 64
    text = f"{candidate_id}\t/codeagent-baseline-x\t{{}}\r\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_inspect_ownership_rejects_extra_trailing_line(monkeypatch):
    candidate_id = "4" * 64
    text = f"{candidate_id}\t/codeagent-baseline-x\t{{}}\nextra-line\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_inspect_ownership_rejects_missing_field(monkeypatch):
    candidate_id = "5" * 64
    text = f"{candidate_id}\t/codeagent-baseline-x\n"  # only two fields
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_inspect_ownership_rejects_extra_field(monkeypatch):
    candidate_id = "6" * 64
    text = f"{candidate_id}\t/codeagent-baseline-x\t{{}}\textra\n"  # four fields
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerInspectError):
        rc._docker_inspect_ownership(candidate_id)


def test_docker_ps_all_id_name_pairs_rejects_crlf_row(monkeypatch):
    live_id = "7" * 64
    text = f"{live_id}\tsome-name\r\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_rejects_missing_final_lf(monkeypatch):
    live_id = "8" * 64
    text = f"{live_id}\tsome-name"  # no trailing newline at all
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


# ---------------------------------------------------------------------------
# Third correction pass finding 1: fail-closed blank-row behavior. Empty
# output is valid (zero containers); once output is nonempty, every row
# must be a genuine id/name record -- a leading, internal, or extra
# trailing blank row is malformed and rejected, never silently skipped,
# matching ADR 0004 Amendment 5's own "each row is exactly one id/name
# record" statement. This reverses the prior correction pass's own
# "deliberately skipped" decision, made before this stricter reading of
# the ADR's own text was reconciled against the code.
# ---------------------------------------------------------------------------


def test_docker_ps_all_id_name_pairs_empty_output_is_valid(monkeypatch):
    result = BoundedProcessResult(returncode=0, stdout=b"")
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    name_to_id, id_to_name = rc._docker_ps_all_id_name_pairs()
    assert name_to_id == {}
    assert id_to_name == {}


def test_docker_ps_all_id_name_pairs_rejects_leading_blank_row(monkeypatch):
    live_id = "9" * 64
    text = f"\n{live_id}\tsome-name\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_rejects_internal_blank_row(monkeypatch):
    id_one = "a" * 64
    id_two = "b" * 64
    text = f"{id_one}\tname-one\n\n{id_two}\tname-two\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_rejects_extra_trailing_blank_row(monkeypatch):
    live_id = "c" * 64
    text = f"{live_id}\tsome-name\n\n"  # a second, extra blank row beyond the one required final LF
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_rejects_duplicate_name(monkeypatch):
    id_one = "a" * 64
    id_two = "b" * 64
    text = f"{id_one}\tsame-name\n{id_two}\tsame-name\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_rejects_extra_tab_field(monkeypatch):
    live_id = "c" * 64
    text = f"{live_id}\tsome-name\textra\n"
    result = BoundedProcessResult(returncode=0, stdout=text.encode())
    monkeypatch.setattr(rc, "run_bounded_stdout", lambda argv, **kwargs: result)
    with pytest.raises(rc._DockerListingError):
        rc._docker_ps_all_id_name_pairs()


# ---------------------------------------------------------------------------
# Correction pass finding 4: direct, fast unit tests for
# `_remove_and_confirm_absent`'s complete post-removal classification,
# independent of the full harness/pipeline.
# ---------------------------------------------------------------------------

_EXPECTED_NAME = "codeagent-baseline-x"


def test_remove_and_confirm_still_present_owned_is_failed(monkeypatch):
    removal_id = "1" * 64
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({_EXPECTED_NAME: removal_id}, {removal_id: _EXPECTED_NAME}))
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=removal_id, name=_EXPECTED_NAME, labels=_owned_labels(state_root_id=_SRID, lifecycle_id=_LCID, role="baseline")
        ),
    )
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.STILL_PRESENT
    assert removal_id not in detail  # no raw id/payload leaked into the diagnostic detail


def test_remove_and_confirm_reinspect_wrong_identity_is_conflict(monkeypatch):
    removal_id = "2" * 64
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({_EXPECTED_NAME: removal_id}, {removal_id: _EXPECTED_NAME}))
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(id="9" * 64, name=_EXPECTED_NAME, labels={}),  # inspect disagrees on id
    )
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFLICT
    assert "9" * 64 not in detail


def test_remove_and_confirm_reinspect_wrong_labels_is_conflict(monkeypatch):
    removal_id = "3" * 64
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({_EXPECTED_NAME: removal_id}, {removal_id: _EXPECTED_NAME}))
    monkeypatch.setattr(
        rc, "_docker_inspect_ownership", lambda cid: rc._InspectOwnership(id=removal_id, name=_EXPECTED_NAME, labels={})
    )
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFLICT


def test_remove_and_confirm_reinspect_failure_is_substrate_unavailable(monkeypatch):
    removal_id = "4" * 64
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({_EXPECTED_NAME: removal_id}, {removal_id: _EXPECTED_NAME}))

    def _boom(cid):
        raise rc._DockerInspectError("boom")

    monkeypatch.setattr(rc, "_docker_inspect_ownership", _boom)
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.SUBSTRATE_UNAVAILABLE


def test_remove_and_confirm_listing_id_name_conflict_is_refused_without_inspect(monkeypatch):
    removal_id = "5" * 64
    inspect_called = {"n": 0}

    def _boom(cid):
        inspect_called["n"] += 1
        raise AssertionError("must not inspect on a listing-level pairing disagreement")

    # A different id occupies the expected name; the persisted id
    # doesn't appear anywhere else.
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({_EXPECTED_NAME: "6" * 64}, {"6" * 64: _EXPECTED_NAME}))
    monkeypatch.setattr(rc, "_docker_inspect_ownership", _boom)
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFLICT
    assert inspect_called["n"] == 0  # listing disagreement alone is conclusive


def test_remove_and_confirm_rm_launch_failure_then_confirmed_absent_still_succeeds(monkeypatch):
    removal_id = "7" * 64

    def _boom(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            raise BoundedProcessError(BoundedProcessFailure.LAUNCH_FAILED, "forced launch failure")
        raise AssertionError(f"unexpected call: {argv}")

    monkeypatch.setattr(rc, "run_bounded_stdout", _boom)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFIRMED_ABSENT


def test_remove_and_confirm_rm_nonzero_then_confirmed_absent_still_succeeds(monkeypatch):
    removal_id = "8" * 64

    def _fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            return BoundedProcessResult(returncode=1, stdout=b"Error: no such container")
        raise AssertionError(f"unexpected call: {argv}")

    monkeypatch.setattr(rc, "run_bounded_stdout", _fake_run)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFIRMED_ABSENT


def test_remove_and_confirm_rm_timeout_then_confirmed_absent_still_succeeds(monkeypatch):
    removal_id = "9" * 64

    def _boom(argv, **kwargs):
        if argv[:2] == ["docker", "rm"]:
            raise BoundedProcessError(BoundedProcessFailure.TIMED_OUT, "forced timeout")
        raise AssertionError(f"unexpected call: {argv}")

    monkeypatch.setattr(rc, "run_bounded_stdout", _boom)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    outcome, detail = rc._remove_and_confirm_absent(
        removal_id=removal_id, expected_name=_EXPECTED_NAME, role="baseline", state_root_id=_SRID, lifecycle_id=_LCID, attempt_rm=True
    )
    assert outcome is rc._DockerRemovalOutcome.CONFIRMED_ABSENT


@pytest.mark.parametrize(
    "outcome_name",
    ["STILL_PRESENT", "CONFLICT", "SUBSTRATE_UNAVAILABLE"],
)
def test_all_non_absent_removal_outcomes_retain_removing_through_the_pipeline(harness, monkeypatch, outcome_name):
    """Every non-`CONFIRMED_ABSENT` removal outcome must leave the
    role's own on-disk attribution at exactly the `removing(id)` its
    own write-ahead phase already durably published -- never touched
    again, never silently advanced or reverted."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    live_id = "1" * 64

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _listing({baseline_name: live_id}))
    monkeypatch.setattr(
        rc,
        "_docker_inspect_ownership",
        lambda cid: rc._InspectOwnership(
            id=live_id, name=baseline_name, labels=_owned_labels(state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline")
        ),
    )

    real_remove = rc._remove_and_confirm_absent
    outcome = getattr(rc._DockerRemovalOutcome, outcome_name)

    def _fake_remove(**kwargs):
        return outcome, "forced"

    monkeypatch.setattr(rc, "_remove_and_confirm_absent", _fake_remove)

    result = harness.reconcile()
    entry = result.entries[0]
    expected_entry_outcome = {
        "STILL_PRESENT": rc.ReconciliationEntryOutcome.FAILED,
        "CONFLICT": rc.ReconciliationEntryOutcome.REFUSED,
        "SUBSTRATE_UNAVAILABLE": rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
    }[outcome_name]
    assert entry.outcome is expected_entry_outcome
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "removing", "id": live_id}


# ---------------------------------------------------------------------------
# Slice 3B-5: real Docker end-to-end container reconciliation and removal
# ---------------------------------------------------------------------------


# Reuses this repository's own pinned, digest-verified verification
# image (`executor.DEFAULT_IMAGE`) rather than a floating, unapproved
# `alpine:latest` that Linux CI would otherwise have to pull fresh and
# unverified (correction pass finding 7). `python3 -c pass` is a
# harmless, always-present command for this image.
_REAL_CONTAINER_ARGV_TAIL = [ex.DEFAULT_IMAGE, "python3", "-c", "pass"]


def _create_owned_container(*, name: str, state_root_id: str, lifecycle_id: str, role: str) -> str:
    labels = _owned_labels(state_root_id=state_root_id, lifecycle_id=lifecycle_id, role=role)
    label_args = []
    for key, value in labels.items():
        label_args += ["--label", f"{key}={value}"]
    result = subprocess.run(
        ["docker", "create", "--name", name, *label_args, *_REAL_CONTAINER_ARGV_TAIL],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _create_unlabeled_container(*, name: str) -> str:
    result = subprocess.run(
        ["docker", "create", "--name", name, *_REAL_CONTAINER_ARGV_TAIL],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _force_remove_container(container_id: str) -> None:
    """Test-fixture teardown only -- load-bearing: a failure here fails
    the test rather than silently leaking a container (correction pass
    finding 7)."""
    subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, text=True, check=True)


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_owned_container_is_observed_and_removed(harness):
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "e" * 32
    projection = _initial_projection(harness, lifecycle_id)
    name = f"codeagent-baseline-{lifecycle_id}"
    container_id = None
    try:
        container_id = _create_owned_container(
            name=name, state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"
        )
        projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        run_dir = _seed_run_dir(harness, lifecycle_id, projection)

        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.baseline_id == container_id
        payload = _read_projection_dict(run_dir)
        assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode != 0  # genuinely gone
        container_id = None
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_unlabeled_container_at_owned_name_is_refused_and_never_removed(harness):
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "f" * 32
    projection = _initial_projection(harness, lifecycle_id)
    name = f"codeagent-baseline-{lifecycle_id}"
    container_id = None
    try:
        container_id = _create_unlabeled_container(name=name)

        projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        run_dir = _seed_run_dir(harness, lifecycle_id, projection)

        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.PREPARING.value  # untouched

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode == 0  # never removed
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_present_owned_container_verification_role_is_removed(harness):
    """ADR 0004 section 7's table, row 2 (`ID set, present/removing` +
    owned) -- exercised with a real container and the *verification*
    role, independent of the CREATING-role baseline coverage above."""
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "1" * 32
    name = f"codeagent-verification-{lifecycle_id}"
    container_id = None
    try:
        container_id = _create_owned_container(
            name=name, state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="verification"
        )
        projection = _initial_projection(harness, lifecycle_id)
        projection = _with_container(
            projection, role="verification", intent=ls.ContainerIntent.PRESENT, id=container_id
        )
        run_dir = _seed_run_dir(harness, lifecycle_id, projection)

        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.verification_id == container_id
        payload = _read_projection_dict(run_dir)
        assert payload["containers"]["verification"] == {"intent": "absent", "id": None}

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode != 0
        container_id = None
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_present_persisted_id_conflict_with_different_live_id_is_refused(harness):
    """ADR 0004 section 7's table, row 3 (`ID set` + name maps to a
    different id) -- exercised with a real container at the recomputed
    name whose real id does not match the persisted id."""
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "3" * 32
    name = f"codeagent-baseline-{lifecycle_id}"
    container_id = None
    try:
        container_id = _create_owned_container(
            name=name, state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"
        )
        wrong_persisted_id = "0" * 64
        assert wrong_persisted_id != container_id
        projection = _initial_projection(harness, lifecycle_id)
        projection = _with_container(
            projection, role="baseline", intent=ls.ContainerIntent.PRESENT, id=wrong_persisted_id
        )
        run_dir = _seed_run_dir(harness, lifecycle_id, projection)

        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.PREPARING.value  # untouched

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode == 0  # never removed
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_present_persisted_id_now_genuinely_absent_reconciles(harness):
    """ADR 0004 section 7's table, row 1 (`ID set, present/removing` +
    no container has that name or id -> confirmed absent) -- exercised
    with a real container that genuinely existed and was genuinely
    removed out of band (simulating a crash after real removal but
    before the projection was ever updated to reflect it), never a
    fabricated id that was never real."""
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "6" * 32
    name = f"codeagent-baseline-{lifecycle_id}"
    container_id = _create_owned_container(
        name=name, state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"
    )
    _force_remove_container(container_id)  # genuinely gone before reconciliation ever runs
    inspect_gone = subprocess.run(["docker", "inspect", container_id], capture_output=True)
    assert inspect_gone.returncode != 0  # precondition: really gone

    projection = _initial_projection(harness, lifecycle_id)
    projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.PRESENT, id=container_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    result = harness.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.baseline_id is None  # never positively observed live this pass
    payload = _read_projection_dict(run_dir)
    assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_absent_persisted_but_name_present_is_refused(harness):
    """ADR 0004 section 7's table, row 8 (`ID null, absent` + name
    present -> Ambiguity: REFUSED, regardless of labels) -- exercised
    with a real, genuinely live container occupying the deterministic
    name while the persisted attribution claims absence."""
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    lifecycle_id = "7" * 32
    name = f"codeagent-baseline-{lifecycle_id}"
    container_id = None
    try:
        # Even an *owned*, correctly labeled container is still an
        # ambiguity here -- the persisted shape itself (absent) admits
        # no legal explanation for anything occupying the name.
        container_id = _create_owned_container(
            name=name, state_root_id=harness.state_root.state_root_id, lifecycle_id=lifecycle_id, role="baseline"
        )
        projection = _initial_projection(harness, lifecycle_id)  # baseline already ABSENT
        run_dir = _seed_run_dir(harness, lifecycle_id, projection)

        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.PREPARING.value  # untouched

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode == 0  # never removed
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


def _reconcile_and_sigkill_after_n_writes(repo_path: str, state_dir: str, n: int) -> None:
    """Module-level (picklable) child-process target: independently
    re-derives the trusted substrate for `repo_path`/`state_dir` (never
    reusing a lock the parent already released) and runs exactly one
    `reconcile_repository()` pass against the run directory the parent
    already seeded, self-SIGKILLing immediately after the n-th
    successful reconciler-owned container write completes -- simulating
    a real crash mid-pass, after some but not all of this pass's
    intended writes have durably landed."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent._lifecycle_fs as lf_child
    import codeagent.lifecycle_store as ls_child
    import codeagent.reconciliation as rc_child
    import codeagent.repo_identity as ri_child
    import codeagent.state_locks as sl_child
    import codeagent.state_root as sr_child

    identity, context = ri_child.discover_repository_identity_and_context(repo_path)
    location = lf_child.resolve_state_root_path()
    root_fd, canonical_root_path = sr_child.open_or_create_canonical_root(location)
    sr_child.validate_state_root_containment(canonical_root_path, context)
    state_root = sr_child.init_state_root(root_fd, canonical_root_path)
    repository_lock = sl_child.acquire_repository_lock(state_root, identity.repo_key)
    identity = ri_child.load_or_create_repo_json(state_root, identity, repository_lock)

    real_publish = ls_child._publish_reconciler_container_transition
    calls = {"n": 0}

    def _counting_publish(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] >= n:
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    rc_child._publish_reconciler_container_transition = _counting_publish
    rc_child.reconcile_repository(
        state_root=state_root, identity=identity, context=context, repository_lock=repository_lock
    )


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_sigkill_after_single_role_write_ahead_then_resumes_without_reincrement(tmp_path):
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    repo = _make_repo(tmp_path)
    state_dir = tmp_path / "state-root"
    lifecycle_id = "4" * 32
    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    container_id = None
    os.environ["CODEAGENT_STATE_DIR"] = str(state_dir)
    try:
        h = _Harness(repo, state_dir)
        try:
            container_id = _create_owned_container(
                name=baseline_name,
                state_root_id=h.state_root.state_root_id,
                lifecycle_id=lifecycle_id,
                role="baseline",
            )
            projection = _initial_projection(h, lifecycle_id)
            projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
            run_dir = _seed_run_dir(h, lifecycle_id, projection)
        finally:
            h.close()

        ctx = multiprocessing.get_context("spawn")
        proc = ctx.Process(target=_reconcile_and_sigkill_after_n_writes, args=(str(repo), str(state_dir), 1))
        proc.start()
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=10)
            pytest.fail("child process did not self-SIGKILL within the timeout; killed and cleaned up")
        assert not proc.is_alive()
        # A real SIGKILL delivered to a still-running child reports as
        # exitcode -SIGKILL on POSIX -- load-bearing evidence that the
        # process actually crashed via signal, not merely exited.
        assert proc.exitcode == -signal.SIGKILL

        # Crash artifact: baseline's own write-ahead landed (REMOVING,
        # durably holding the real container's id); the container
        # itself was never actually removed (the crash happened before
        # `docker rm` was ever issued).
        mid_crash_payload = _read_projection_dict(run_dir)
        assert mid_crash_payload["state"] == ls.LifecycleState.RECONCILING.value
        assert mid_crash_payload["reconciliation"]["attempts_total"] == 1
        assert mid_crash_payload["containers"]["baseline"] == {"intent": "removing", "id": container_id}
        inspect_mid = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect_mid.returncode == 0  # still there -- crash was before removal

        h2 = _Harness(repo, state_dir)
        try:
            result = h2.reconcile()
        finally:
            h2.close()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.attempt_number == 1  # never re-incremented across the crash
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.RECONCILED.value
        assert payload["reconciliation"]["attempts_total"] == 1
        assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}

        inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert inspect.returncode != 0  # genuinely gone now
        container_id = None
    finally:
        if container_id is not None:
            _force_remove_container(container_id)
        del os.environ["CODEAGENT_STATE_DIR"]


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_real_sigkill_after_both_roles_write_ahead_then_resumes_without_reincrement(tmp_path):
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    repo = _make_repo(tmp_path)
    state_dir = tmp_path / "state-root"
    lifecycle_id = "5" * 32
    baseline_name = f"codeagent-baseline-{lifecycle_id}"
    verification_name = f"codeagent-verification-{lifecycle_id}"
    baseline_id = None
    verification_id = None
    os.environ["CODEAGENT_STATE_DIR"] = str(state_dir)
    try:
        h = _Harness(repo, state_dir)
        try:
            baseline_id = _create_owned_container(
                name=baseline_name,
                state_root_id=h.state_root.state_root_id,
                lifecycle_id=lifecycle_id,
                role="baseline",
            )
            verification_id = _create_owned_container(
                name=verification_name,
                state_root_id=h.state_root.state_root_id,
                lifecycle_id=lifecycle_id,
                role="verification",
            )
            projection = _initial_projection(h, lifecycle_id)
            projection = _with_container(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
            projection = _with_container(projection, role="verification", intent=ls.ContainerIntent.CREATING, id=None)
            run_dir = _seed_run_dir(h, lifecycle_id, projection)
        finally:
            h.close()

        ctx = multiprocessing.get_context("spawn")
        # Both roles' write-ahead writes must complete (2 publishes)
        # before either role's own removal is ever attempted -- so
        # crashing right after the 2nd publish leaves both containers
        # still genuinely present, with both roles durably `removing`.
        proc = ctx.Process(target=_reconcile_and_sigkill_after_n_writes, args=(str(repo), str(state_dir), 2))
        proc.start()
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=10)
            pytest.fail("child process did not self-SIGKILL within the timeout; killed and cleaned up")
        assert not proc.is_alive()
        assert proc.exitcode == -signal.SIGKILL

        mid_crash_payload = _read_projection_dict(run_dir)
        assert mid_crash_payload["state"] == ls.LifecycleState.RECONCILING.value
        assert mid_crash_payload["reconciliation"]["attempts_total"] == 1
        assert mid_crash_payload["containers"]["baseline"] == {"intent": "removing", "id": baseline_id}
        assert mid_crash_payload["containers"]["verification"] == {"intent": "removing", "id": verification_id}
        for cid in (baseline_id, verification_id):
            inspect_mid = subprocess.run(["docker", "inspect", cid], capture_output=True)
            assert inspect_mid.returncode == 0  # both still there

        h2 = _Harness(repo, state_dir)
        try:
            result = h2.reconcile()
        finally:
            h2.close()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.attempt_number == 1  # never re-incremented across the crash
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == ls.LifecycleState.RECONCILED.value
        assert payload["reconciliation"]["attempts_total"] == 1
        assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}
        assert payload["containers"]["verification"] == {"intent": "absent", "id": None}

        for cid in (baseline_id, verification_id):
            inspect = subprocess.run(["docker", "inspect", cid], capture_output=True)
            assert inspect.returncode != 0
        baseline_id = None
        verification_id = None
    finally:
        for cid in (baseline_id, verification_id):
            if cid is not None:
                _force_remove_container(cid)
        del os.environ["CODEAGENT_STATE_DIR"]


@pytest.mark.skipif(not (_docker_available() or REQUIRE_DOCKER), reason="real Docker daemon not available")
def test_final_cleanup_no_leftover_slice_3b5_containers():
    """Load-bearing whole-family cleanup evidence (correction pass
    finding 7): queries every Slice 3B-5 container-name family, not
    only `codeagent-verify` (Milestone 1's own unrelated family).

    Two independent queries, deliberately not relying on either alone:
    Docker's own `--filter name=` regex (`^codeagent-(baseline|
    verification)-`, confirmed against a real Docker Desktop container
    to actually anchor and alternate as intended, not merely substring-
    match) as the primary, fast query; and a fully parser/filter-
    independent fallback -- one unfiltered `docker ps -a` listing,
    matched client-side in plain Python string logic that depends on
    nothing Docker-version- or platform-specific. Both are asserted, so
    a regression in either the filter syntax's own cross-platform
    behavior or this module's own listing helper would still be caught
    by the other. A failure lists the actual leftover names so it is
    never a bare boolean, and a failure here reports itself distinctly
    rather than silently masking whatever real test failure upstream
    actually caused the leak."""
    if REQUIRE_DOCKER and not _docker_available():
        pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but a real Docker daemon is not available")

    filtered = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=^codeagent-(baseline|verification)-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    filtered_leftover = [name for name in filtered.stdout.splitlines() if name]

    unfiltered = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True
    )
    unfiltered_leftover = [
        name
        for name in unfiltered.stdout.splitlines()
        if name and (name.startswith("codeagent-baseline-") or name.startswith("codeagent-verification-"))
    ]

    assert filtered_leftover == [], f"leftover (via docker's own filter): {filtered_leftover}"
    assert unfiltered_leftover == [], f"leftover (via parser-independent client-side match): {unfiltered_leftover}"
