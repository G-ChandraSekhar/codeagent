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


def test_complete_absent_shape_is_skipped_with_zero_calls(harness, monkeypatch):
    """The owner-state `COMPLETE` state (Milestone 3 Slice 3C-1, ADR
    0004 Amendment 8's `LifecycleOwnerStatePublisher.complete()`) is
    clean-final identically to `RECONCILED` --
    `reconciliation.py`'s own clean-final check
    (`peek.state in (LifecycleState.COMPLETE, LifecycleState.RECONCILED)`)
    treats them via the same code path, but this is the one test that
    exercises `COMPLETE` specifically rather than only `RECONCILED`."""
    lifecycle_id = "a" * 32
    projection = dataclasses.replace(_initial_projection(harness, lifecycle_id), state=ls.LifecycleState.COMPLETE)
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


def test_real_deterministic_worktree_registered_and_present_causes_refused_blocked(harness):
    """Milestone 3 Slice 3C-2's own motivating regression test, using no
    mocking of `_worktree_registered_paths`/Git at all: a *real*
    `state_root.reserve_worktree_leaf()` + real
    `workspace.GitWorktree(reservation=...)` entry (left registered, as a
    crash before disposal would leave it) is placed at exactly the
    deterministic path this module's own `_reconcile_locked_entry` already
    expects. Before Slice 3C-2, no production code ever placed a real
    worktree there, so this exact scenario could not previously be
    constructed with real Git — only simulated via the mock above."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    run_dir = _seed_run_dir(harness, lifecycle_id, projection)

    from codeagent import workspace as ws

    # The reservation's own context manager is still used (so its
    # parent/leaf descriptors are never leaked past this test, matching
    # this slice's own ownership rules), but nothing inside the `with`
    # body disposes the worktree -- preserving the exact simulated-crash
    # condition (a real, registered, materialized worktree left behind)
    # for the `harness.reconcile()` call in the middle of the block.
    with harness.state_root.reserve_worktree_leaf(harness.identity.repo_key, lifecycle_id) as reservation:
        expected_path = reservation.path
        wt = ws.GitWorktree(harness.repo, run_id="crashed-run", reservation=reservation)
        wt.__enter__()  # deliberately never disposed -- simulates a crash before teardown
        try:
            result = harness.reconcile()
            entry = result.entries[0]
            assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
            assert result.blocked
            payload = _read_projection_dict(run_dir)
            assert payload["state"] == ls.LifecycleState.PREPARING.value
            # Reconciliation never mutated the real worktree it found.
            assert expected_path.exists()
        finally:
            wt.dispose()


def test_real_present_but_unregistered_worktree_directory_causes_refused(harness):
    """A directory physically present at the exact deterministic path but
    never registered with Git at all (e.g. a crash between this slice's
    exclusive leaf reservation and `git worktree add` ever running) is
    still refused — `_reconcile_locked_entry`'s own check is `path in
    registered_paths OR os.path.lexists(path)`, so presence alone (with
    no Git registration) is sufficient, proven here with a real,
    unregistered directory rather than a mock."""
    lifecycle_id = "a" * 32
    projection = _initial_projection(harness, lifecycle_id)
    _seed_run_dir(harness, lifecycle_id, projection)

    reservation = harness.state_root.reserve_worktree_leaf(harness.identity.repo_key, lifecycle_id)
    try:
        result = harness.reconcile()
        entry = result.entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
        assert result.blocked
    finally:
        reservation.__exit__(None, None, None)


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


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 12: the narrow `creating`-worktree reconciliation row.
# Real Git and real filesystem throughout; only the Docker listing is
# patched (no container is ever involved in this row).
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

from codeagent import worktree_lifecycle as _wl

_A12_ID = "d" * 32


def _a12_head(h):
    return _run("git", "-C", str(h.repo), "rev-parse", "HEAD").stdout.strip()


def _a12_leaf(h, lifecycle_id=_A12_ID) -> _Path:
    return _Path(h.state_root.path) / "worktrees" / h.identity.repo_key / lifecycle_id


def _a12_seed(h, *, state=ls.LifecycleState.PREPARING, attempts=0, leaf="empty", worktree="creating"):
    projection = _initial_projection(h, _A12_ID)
    wt = (
        _wl.ABSENT_WORKTREE_TRANSITION
        if worktree == "absent"
        else _wl.WorktreeTransition(intent=_wl.WorktreeIntent(worktree), expected_head=_a12_head(h))
    )
    projection = dataclasses.replace(
        projection,
        state=state,
        worktree=wt,
        reconciliation=ls.ReconciliationSummary(attempts_total=attempts, recent_failures=()),
    )
    run_dir = _seed_run_dir(h, _A12_ID, projection)
    path = _a12_leaf(h)
    if leaf is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.parent.chmod(0o700)
        path.parent.chmod(0o700)
    if leaf in ("empty", "nonempty", "mode_0755"):
        path.mkdir(mode=0o700)
        path.chmod(0o755 if leaf == "mode_0755" else 0o700)
        if leaf == "nonempty":
            (path / "f").write_text("x")
    return run_dir, path


def _a12_entry_event(h, result):
    trace = h.repo_dir() / "maintenance" / f"{result.maintenance_id}.jsonl"
    text = trace.read_text()
    events = [json.loads(line) for line in text.splitlines()]
    return [e for e in events if e["event_type"] == "ReconciliationEntryRecorded"][0], text


@pytest.fixture
def a12(harness, monkeypatch):
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    yield harness
    path = _a12_leaf(harness)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        path.chmod(0o700)
        for child in path.iterdir():
            child.unlink()
        path.rmdir()


def _a12_no_rmdir(monkeypatch):
    real = os.rmdir

    def guard(p, *a, dir_fd=None, **k):
        if p == _A12_ID and dir_fd is not None:
            raise AssertionError("rmdir must not be called")
        return real(p, *a, dir_fd=dir_fd, **k)

    monkeypatch.setattr(sr.os, "rmdir", guard)


def test_a12_fresh_creating_empty_leaf_is_reconciled(a12):
    run_dir, path = _a12_seed(a12)
    result = a12.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert not result.blocked
    assert not os.path.lexists(path)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["worktree"] == {"intent": "absent", "expected_head": None}
    assert payload["reconciliation"]["attempts_total"] == 1
    event, _ = _a12_entry_event(a12, result)
    amendment_12_fields = {
        "confirmed_absent": True,
        "initial_persisted_intent": "creating",
        "leaf_outcome": "removed",
        "removal_observation": "post_absent",
        "absent_transition_confirmed_this_pass": True,
    }
    assert {k: event["worktree"][k] for k in amendment_12_fields} == amendment_12_fields
    # Amendment 13 additions: the router's registration analysis is recorded;
    # the materialized-row evidence was never collected on this path.
    assert event["worktree"]["registration_pre"] == {
        "state": "absent", "target_records": 0, "target_bare": None, "locked": None, "prunable": None
    }
    assert event["worktree"]["removal_attempt"] == "not_attempted"
    assert event["worktree"]["leaf_post"] is None


@pytest.mark.parametrize("parent_present", [True, False])
def test_a12_fresh_creating_leaf_absent_never_calls_rmdir(a12, monkeypatch, parent_present):
    run_dir, _ = _a12_seed(a12, leaf="absent" if parent_present else None)
    _a12_no_rmdir(monkeypatch)
    result = a12.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert result.entries[0].worktree_leaf_outcome == "already_absent"
    assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 1


def test_a12_resume_case_a_leaf_present_keeps_attempts(a12):
    run_dir, path = _a12_seed(a12, state=ls.LifecycleState.RECONCILING, attempts=1)
    result = a12.reconcile()
    assert result.entries[0].outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert not os.path.lexists(path)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["reconciliation"]["attempts_total"] == 1


def test_a12_resume_case_b_leaf_absent_no_rmdir_keeps_attempts(a12, monkeypatch):
    run_dir, _ = _a12_seed(a12, state=ls.LifecycleState.RECONCILING, attempts=1, leaf="absent")
    _a12_no_rmdir(monkeypatch)
    result = a12.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.worktree_absent_transition_confirmed_this_pass
    payload = _read_projection_dict(run_dir)
    assert payload["worktree"]["intent"] == "absent"
    assert payload["reconciliation"]["attempts_total"] == 1


def test_a12_resume_case_c_absent_worktree_takes_existing_path(a12, monkeypatch):
    run_dir, _ = _a12_seed(a12, state=ls.LifecycleState.RECONCILING, attempts=1, leaf=None, worktree="absent")

    def boom(*a, **k):
        raise AssertionError("the worktree edge must not be republished in case C")

    monkeypatch.setattr(rc, "_publish_reconciler_worktree_transition", boom)
    result = a12.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.worktree_initial_persisted_intent == "absent"
    assert entry.worktree_leaf_outcome == "not_applicable"
    assert entry.worktree_absent_transition_confirmed_this_pass is False
    assert entry.worktree_confirmed_absent is True
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["reconciliation"]["attempts_total"] == 1


def _a12_git_dir(h) -> _Path:
    return _Path(h.identity.canonical_common_dir)


def _a12_assert_untouched(run_dir, path):
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "PREPARING"
    assert payload["worktree"]["intent"] == "creating"
    assert payload["reconciliation"]["attempts_total"] == 0
    assert os.path.lexists(path)


@pytest.mark.parametrize(
    "setup, outcome",
    [
        ("admin_locked_only", "REFUSED"),
        ("admin_suffix", "REFUSED"),
        ("gitdir_registration", "REFUSED"),
        ("admin_symlink_dir", "REFUSED"),
        ("nonempty_leaf", "REFUSED"),
        ("mode_0755_leaf", "REFUSED"),
        ("container_present", "REFUSED"),
        ("checkpoint_ref_present", "REFUSED"),
        ("container_listing_fails", "SUBSTRATE_UNAVAILABLE"),
        ("git_listing_fails", "SUBSTRATE_UNAVAILABLE"),
        ("leaf_uninspectable", "SUBSTRATE_UNAVAILABLE"),
        ("entry_limit", "SUBSTRATE_UNAVAILABLE"),
        ("byte_limit", "SUBSTRATE_UNAVAILABLE"),
    ],
)
def test_a12_inspection_refusals_mutate_nothing(a12, monkeypatch, tmp_path, setup, outcome):
    leaf_kind = {"nonempty_leaf": "nonempty", "mode_0755_leaf": "mode_0755"}.get(setup, "empty")
    run_dir, path = _a12_seed(a12, leaf=leaf_kind)
    admin = _a12_git_dir(a12) / "worktrees"
    if setup == "admin_locked_only":
        (admin / _A12_ID).mkdir(parents=True)
        (admin / _A12_ID / "locked").write_text("initializing")
    elif setup == "admin_suffix":
        (admin / f"{_A12_ID}42").mkdir(parents=True)
    elif setup == "gitdir_registration":
        (admin / _A12_ID).mkdir(parents=True)
        (admin / _A12_ID / "locked").write_text("initializing")
        (admin / _A12_ID / "gitdir").write_text(os.path.realpath(path) + "/.git\n")
    elif setup == "admin_symlink_dir":
        target = tmp_path / "elsewhere"
        target.mkdir()
        os.symlink(target, admin)
    elif setup == "container_present":
        monkeypatch.setattr(
            rc, "_docker_ps_all_id_name_pairs", lambda: _listing({f"codeagent-baseline-{_A12_ID}": "e" * 64})
        )
    elif setup == "checkpoint_ref_present":
        monkeypatch.setattr(rc.CheckpointRef, "observe", lambda self: cr.RefObservation(present=True, oid="0" * 40))
    elif setup == "container_listing_fails":
        monkeypatch.setattr(
            rc, "_docker_ps_all_id_name_pairs", lambda: (_ for _ in ()).throw(rc._DockerListingError("x"))
        )
    elif setup == "git_listing_fails":
        monkeypatch.setattr(
            rc, "_worktree_registered_paths", lambda root: (_ for _ in ()).throw(rc._GitWorktreeListingError("x"))
        )
    elif setup == "leaf_uninspectable":
        real_stat = os.stat

        def failing_stat(p, *a, **k):
            if p == _A12_ID and k.get("dir_fd") is not None:
                raise PermissionError(13, "denied")
            return real_stat(p, *a, **k)

        monkeypatch.setattr(sr.os, "stat", failing_stat)
    elif setup == "entry_limit":
        for i in range(3):
            (admin / f"unrelated{i}").mkdir(parents=True)
        monkeypatch.setattr(rc, "_ADMIN_SCAN_MAX_ENTRIES", 2)
    elif setup == "byte_limit":
        (admin / "eleven-byte").mkdir(parents=True)
        monkeypatch.setattr(rc, "_ADMIN_SCAN_MAX_NAME_BYTES", 10)
    result = a12.reconcile()
    monkeypatch.undo()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], entry.detail
    assert result.blocked
    _a12_assert_untouched(run_dir, path)
    if os.path.islink(admin):
        os.unlink(admin)


def test_a12_absent_git_worktrees_dir_is_none_found(a12):
    assert not (_a12_git_dir(a12) / "worktrees").exists()
    assert rc._scan_worktree_admin_entries(a12.identity.canonical_common_dir, _A12_ID) is rc._AdminScanResult.NONE_FOUND


@pytest.mark.parametrize(
    "name, matches",
    [
        (_A12_ID, True),
        (f"{_A12_ID}1", True),
        (f"{_A12_ID}11", True),
        (f"{_A12_ID}" + "7" * 25, True),
        (_A12_ID[:31], False),
        (_A12_ID.upper(), False),
        (f"{_A12_ID}x", False),
        ("e" * 32, False),
        (f"{'e' * 32}1", False),
    ],
)
def test_a12_admin_name_grammar(a12, name, matches):
    (_a12_git_dir(a12) / "worktrees" / name).mkdir(parents=True)
    result = rc._scan_worktree_admin_entries(a12.identity.canonical_common_dir, _A12_ID)
    assert result is (rc._AdminScanResult.MATCH_FOUND if matches else rc._AdminScanResult.NONE_FOUND)


def _a12_failing_cleanup(calls):
    def fake(fds, primary):
        calls.append(list(fds))
        lf.close_confirmed(fds)
        err = lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "simulated")
        raise err from primary

    return fake


@pytest.mark.parametrize("setup, opened", [("none_found", 1), ("match_found", 2), ("unexpected_substrate", 1)])
def test_a12_admin_scan_close_failure_dominates_every_result(a12, monkeypatch, tmp_path, setup, opened):
    run_dir, path = _a12_seed(a12)
    admin = _a12_git_dir(a12) / "worktrees"
    if setup == "match_found":
        (admin / f"{_A12_ID}42").mkdir(parents=True)
    elif setup == "unexpected_substrate":
        target = tmp_path / "elsewhere"
        target.mkdir()
        os.symlink(target, admin)
    calls: list = []
    monkeypatch.setattr(rc, "_dominant_cleanup", _a12_failing_cleanup(calls))
    result = a12.reconcile()
    monkeypatch.undo()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert len(calls) == 1 and len(calls[0]) == opened
    _a12_assert_untouched(run_dir, path)
    _, text = _a12_entry_event(a12, result)
    assert f"{_A12_ID}42" not in text
    assert str(tmp_path) not in text and os.path.realpath(tmp_path) not in text
    if os.path.islink(admin):
        os.unlink(admin)


def _a12_patch_rmdir(monkeypatch, behavior):
    real = os.rmdir

    def fake(p, *a, dir_fd=None, **k):
        if p == _A12_ID and dir_fd is not None:
            return behavior(real, p, dir_fd)
        return real(p, *a, dir_fd=dir_fd, **k)

    monkeypatch.setattr(sr.os, "rmdir", fake)


def _a12_fail_nth_leaf_stat(monkeypatch, n):
    real = os.stat
    seen = {"n": 0}

    def fake(p, *a, **k):
        if p == _A12_ID and k.get("dir_fd") is not None:
            seen["n"] += 1
            if seen["n"] == n:
                raise PermissionError(13, "denied")
        return real(p, *a, **k)

    monkeypatch.setattr(sr.os, "stat", fake)


def _a12_eio(*_):
    raise OSError(errno.EIO, "simulated")


def _a12_entries_sequence(monkeypatch, values):
    calls = {"n": 0}
    real = sr._directory_has_any_entry

    def fake(fd):
        calls["n"] += 1
        value = values.get(calls["n"])
        if value is None:
            return real(fd)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(sr, "_directory_has_any_entry", fake)


# I5 calls `os.stat(name)` once and `_directory_has_any_entry` once; M2's
# primitive then calls them again (identity, listing, post-observation).
@pytest.mark.parametrize(
    "row, inject, outcome, leaf_outcome, observation, leaf_remains",
    [
        ("P2", ("entries", {2: True}), "REFUSED", "conflict_not_removed", "pre_conflict", True),
        ("P3", ("entries", {2: OSError(errno.EIO, "x")}), "SUBSTRATE_UNAVAILABLE", "removal_not_attempted", "pre_inspection_failed", True),
        ("P4", ("rmdir", "enotempty"), "REFUSED", "conflict_not_removed", "not_empty_at_rmdir", True),
        ("S2", ("rmdir", "noop"), "FAILED", "original_still_present", "original_still_present", True),
        ("S3", ("rmdir", "replace"), "REFUSED", "replacement_conflict", "replacement_conflict", True),
        ("S4", ("post_stat", 3), "SUBSTRATE_UNAVAILABLE", "post_inspection_failed", "post_inspection_failed", False),
        ("E2", ("rmdir", "eio"), "FAILED", "original_still_present", "original_still_present", True),
        ("E3", ("rmdir", "replace_eio"), "REFUSED", "replacement_conflict", "replacement_conflict", True),
        ("E4", ("rmdir_eio_post_stat", 3), "SUBSTRATE_UNAVAILABLE", "post_inspection_failed", "post_inspection_failed", True),
    ],
)
def test_a12_removal_rows(a12, monkeypatch, row, inject, outcome, leaf_outcome, observation, leaf_remains):
    run_dir, path = _a12_seed(a12)
    kind, arg = inject
    if kind == "entries":
        _a12_entries_sequence(monkeypatch, arg)
    elif kind == "rmdir":
        behavior = {
            "noop": lambda real, p, fd: None,
            "eio": lambda real, p, fd: _a12_eio(),
            "enotempty": lambda real, p, fd: ((path / "raced").write_text("x"), real(p, dir_fd=fd)),
            "replace": lambda real, p, fd: (real(p, dir_fd=fd), os.mkdir(p, 0o700, dir_fd=fd)),
            "replace_eio": lambda real, p, fd: (real(p, dir_fd=fd), os.mkdir(p, 0o700, dir_fd=fd), _a12_eio()),
        }[arg]
        _a12_patch_rmdir(monkeypatch, behavior)
    elif kind == "post_stat":
        _a12_fail_nth_leaf_stat(monkeypatch, arg)
    elif kind == "rmdir_eio_post_stat":
        _a12_patch_rmdir(monkeypatch, lambda real, p, fd: _a12_eio())
        _a12_fail_nth_leaf_stat(monkeypatch, arg)
    result = a12.reconcile()
    monkeypatch.undo()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], (row, entry.detail)
    assert entry.worktree_leaf_outcome == leaf_outcome
    assert entry.worktree_removal_observation == observation
    assert entry.worktree_absent_transition_confirmed_this_pass is False
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING"
    assert payload["worktree"]["intent"] == "creating"
    assert payload["reconciliation"]["attempts_total"] == 1
    assert os.path.lexists(path) is leaf_remains
    if path.is_dir():
        for child in path.iterdir():
            child.unlink()


def test_a12_e1_absent_after_failed_rmdir_is_reconciled(a12, monkeypatch):
    run_dir, path = _a12_seed(a12)
    _a12_patch_rmdir(monkeypatch, lambda real, p, fd: (real(p, dir_fd=fd), _a12_eio()))
    result = a12.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.worktree_leaf_outcome == "absent_after_failed_rmdir"
    assert not os.path.lexists(path)


def test_a12_c1_close_failure_dominates_and_stops_m4_to_m6_then_resumes_case_b(a12, monkeypatch):
    run_dir, path = _a12_seed(a12)
    real = sr._dominant_cleanup
    calls: list = []

    def fake(fds, primary):
        if len(fds) == 2:  # the leaf handle's own close (parent + leaf)
            calls.append(list(fds))
            lf.close_confirmed(fds)
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "simulated") from primary
        return real(fds, primary)

    def boom(*a, **k):
        raise AssertionError("M5 must not run after a close failure")

    monkeypatch.setattr(sr, "_dominant_cleanup", fake)
    monkeypatch.setattr(rc, "_publish_reconciler_worktree_transition", boom)
    result = a12.reconcile()
    monkeypatch.setattr(rc, "_publish_reconciler_worktree_transition", ls._publish_reconciler_worktree_transition)
    monkeypatch.setattr(sr, "_dominant_cleanup", real)
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.worktree_leaf_outcome == "close_failed"
    assert entry.worktree_removal_observation == "post_absent"
    assert len(calls) == 1
    assert not os.path.lexists(path)
    assert _read_projection_dict(run_dir)["worktree"]["intent"] == "creating"

    second = a12.reconcile().entries[0]  # resume case B
    assert second.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert second.worktree_leaf_outcome == "already_absent"
    assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 1


@pytest.mark.parametrize("failure", ["admin_entry", "listing_fails"])
def test_a12_post_removal_git_recheck(a12, monkeypatch, failure):
    run_dir, path = _a12_seed(a12)
    if failure == "admin_entry":
        real_scan = rc._scan_worktree_admin_entries
        seen = {"n": 0}

        def scan(common, lid):
            seen["n"] += 1
            return rc._AdminScanResult.MATCH_FOUND if seen["n"] == 2 else real_scan(common, lid)

        monkeypatch.setattr(rc, "_scan_worktree_admin_entries", scan)
        expected = rc.ReconciliationEntryOutcome.REFUSED
    else:
        real_list = rc._worktree_registered_paths
        seen = {"n": 0}

        def listing(root):
            seen["n"] += 1
            if seen["n"] == 2:
                raise rc._GitWorktreeListingError("x")
            return real_list(root)

        monkeypatch.setattr(rc, "_worktree_registered_paths", listing)
        expected = rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    entry = a12.reconcile().entries[0]
    monkeypatch.undo()
    assert entry.outcome is expected
    assert entry.worktree_leaf_outcome == "removed"
    assert not os.path.lexists(path)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "creating"


def _a12_publish_fault(monkeypatch, nth, *, installed):
    real = ls.publish_private_file_atomically_at
    seen = {"n": 0}

    def fake(*a, **k):
        seen["n"] += 1
        if seen["n"] == nth:
            if installed:
                real(*a, **k)
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED, "x")
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")
        return real(*a, **k)

    monkeypatch.setattr(ls, "publish_private_file_atomically_at", fake)


# Publication order in this row: #1 RECONCILING (M1), #2 creating->absent
# (M5), #3 RECONCILED (M6). The maintenance trace is not a projection write.
@pytest.mark.parametrize(
    "nth, installed, disk_state, disk_worktree, transition_confirmed, leaf_gone, second_outcome",
    [
        (1, False, "PREPARING", "creating", False, False, "RECONCILED"),
        (1, True, "RECONCILING", "creating", False, False, "RECONCILED"),
        (2, False, "RECONCILING", "creating", False, True, "RECONCILED"),
        (2, True, "RECONCILING", "absent", False, True, "RECONCILED"),
        (3, False, "RECONCILING", "absent", True, True, "RECONCILED"),
        (3, True, "RECONCILED", "absent", True, True, "SKIPPED_TERMINAL"),
    ],
)
def test_a12_publication_failures_leave_exact_disk_state_and_resume(
    a12, monkeypatch, nth, installed, disk_state, disk_worktree, transition_confirmed, leaf_gone, second_outcome
):
    run_dir, path = _a12_seed(a12)
    _a12_publish_fault(monkeypatch, nth, installed=installed)
    first = a12.reconcile()
    monkeypatch.setattr(ls, "publish_private_file_atomically_at", lf.publish_private_file_atomically_at)
    entry = first.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert first.blocked
    assert entry.worktree_absent_transition_confirmed_this_pass is transition_confirmed
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == disk_state
    assert payload["worktree"]["intent"] == disk_worktree
    assert (not os.path.lexists(path)) is leaf_gone

    second = a12.reconcile()
    assert second.entries[0].outcome is rc.ReconciliationEntryOutcome[second_outcome]
    assert not second.blocked
    final = _read_projection_dict(run_dir)
    assert final["state"] == "RECONCILED"
    assert final["reconciliation"]["attempts_total"] == 1
    assert not os.path.lexists(path)


@pytest.mark.parametrize("worktree", ["present", "disposing"])
def test_a12_present_and_disposing_without_registration_refused_by_amendment_13(a12, worktree):
    """Superseded by Amendment 13: `present`/`disposing` records are now
    eligible, so they pass the pre-lock check. With no registration and an
    empty leaf (no `.git` file) the Amendment 13 table still refuses them,
    with nothing changed."""
    run_dir, path = _a12_seed(a12, worktree=worktree)
    entry = a12.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.worktree_initial_persisted_intent == worktree
    assert entry.worktree_removal_attempt == "not_attempted"
    assert _read_projection_dict(run_dir)["state"] == "PREPARING"
    assert path.is_dir()


@pytest.mark.parametrize("worktree", ["present", "disposing"])
def test_a12_present_and_disposing_refused_at_locked_reread(a12, monkeypatch, worktree):
    run_dir, path = _a12_seed(a12)
    real_load = rc.load_lifecycle_projection
    seen = {"n": 0}

    def load(*a, **k):
        seen["n"] += 1
        projection = real_load(*a, **k)
        if seen["n"] == 2:  # the locked authoritative re-read
            projection = dataclasses.replace(
                projection,
                worktree=_wl.WorktreeTransition(
                    intent=_wl.WorktreeIntent(worktree), expected_head=projection.worktree.expected_head
                ),
            )
        return projection

    monkeypatch.setattr(rc, "load_lifecycle_projection", load)
    entry = a12.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.worktree_initial_persisted_intent == worktree
    assert path.is_dir()


def test_a12_maintenance_event_fits_bound_with_every_field_maximal(tmp_path):
    fd = os.open(tmp_path / "trace.jsonl", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        writer = rc._MaintenanceTraceWriter(fd=fd, maintenance_id="f" * 32, state_root_id="s" * 32, repo_key="r" * 32)
        writer.entry_recorded(
            rc.ReconciliationEntryResult(
                "a" * 32,
                rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
                "x" * 2000,
                run_id="r" * 2000,
                attempt_number=10**9,
                baseline_id="b" * 64,
                verification_id="c" * 64,
                worktree_initial_persisted_intent="disposing",
                worktree_leaf_outcome="absent_after_failed_rmdir",
                worktree_removal_observation="pre_inspection_failed",
                worktree_absent_transition_confirmed_this_pass=True,
            )
        )
    finally:
        os.close(fd)
    line = (tmp_path / "trace.jsonl").read_bytes()
    assert len(line) <= rc.MAINTENANCE_EVENT_MAX_BYTES


def test_a12_admin_scan_reads_names_only():
    import inspect

    tree = ast.parse(inspect.getsource(rc._scan_admin_entries_into))
    forbidden = {"stat", "lstat", "fstat", "is_dir", "is_file", "is_symlink", "read", "readlink", "rmdir", "unlink", "remove"}
    opens = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, node.func.attr
            if node.func.attr == "open":
                opens += 1
    assert opens == 2  # the common directory and its worktrees/ child only


def test_a12_every_new_open_is_followed_by_assert_cloexec():
    import inspect

    for fn in (rc._scan_admin_entries_into, sr._open_abandoned_leaf):
        source = inspect.getsource(fn)
        lines = [line.strip() for line in source.splitlines()]
        open_lines = [i for i, line in enumerate(lines) if "os.open(" in line]
        assert open_lines, fn.__name__
        for i in open_lines:
            following = "\n".join(lines[i : i + 12])
            assert "_assert_cloexec(" in following, (fn.__name__, lines[i])


def _a12_fd_count() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("which", ["common_dir", "worktrees_dir"])
@pytest.mark.parametrize("cleanup", ["confirmed", "failed"])
def test_a12_admin_scan_cloexec_failure_propagates_with_exact_cause(a12, monkeypatch, which, cleanup):
    """A failed CLOEXEC check is never converted into a scan result: it
    propagates as the exact SUBSTRATE_UNAVAILABLE error after every opened
    descriptor is closed once, or, if that close fails, CLEANUP_UNCONFIRMED
    dominates and is chained from that exact error."""
    (_a12_git_dir(a12) / "worktrees").mkdir(exist_ok=True)
    fail_on = 1 if which == "common_dir" else 2
    cloexec_error = lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "no cloexec")
    real_assert = rc._assert_cloexec
    seen = {"n": 0}

    def assert_cloexec(fd):
        seen["n"] += 1
        if seen["n"] == fail_on:
            raise cloexec_error
        real_assert(fd)

    monkeypatch.setattr(rc, "_assert_cloexec", assert_cloexec)
    calls: list = []
    if cleanup == "failed":
        monkeypatch.setattr(rc, "_dominant_cleanup", _a12_failing_cleanup(calls))
    before = _a12_fd_count()
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        rc._scan_worktree_admin_entries(a12.identity.canonical_common_dir, _A12_ID)
    if cleanup == "confirmed":
        assert excinfo.value is cloexec_error
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    else:
        assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
        assert excinfo.value.__cause__ is cloexec_error
        assert len(calls) == 1 and len(calls[0]) == fail_on  # each opened descriptor, attempted once
    assert _a12_fd_count() == before


def test_a12_admin_scan_cloexec_failure_blocks_the_entry(a12, monkeypatch):
    run_dir, path = _a12_seed(a12)
    monkeypatch.setattr(
        rc,
        "_assert_cloexec",
        lambda fd: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")),
    )
    entry = a12.reconcile().entries[0]
    monkeypatch.undo()
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    _a12_assert_untouched(run_dir, path)


@pytest.mark.parametrize(
    "names, max_entries, max_bytes, expected",
    [
        # A matching entry that crosses a bound never bypasses it.
        ([_A12_ID], 0, 10**6, "LIMIT_EXCEEDED"),
        ([_A12_ID], 10, 31, "LIMIT_EXCEEDED"),
        # An in-bound matching entry, exactly at both bounds, still matches.
        ([_A12_ID], 1, 32, "MATCH_FOUND"),
        # Ordinary exact-bound behavior.
        (["a1", "b2"], 2, 4, "NONE_FOUND"),
        (["a1", "b2"], 1, 10**6, "LIMIT_EXCEEDED"),
        (["a1", "b2"], 10, 3, "LIMIT_EXCEEDED"),
    ],
)
def test_a12_admin_scan_bounds_apply_before_matching(a12, monkeypatch, names, max_entries, max_bytes, expected):
    admin = _a12_git_dir(a12) / "worktrees"
    for name in names:
        (admin / name).mkdir(parents=True)
    monkeypatch.setattr(rc, "_ADMIN_SCAN_MAX_ENTRIES", max_entries)
    monkeypatch.setattr(rc, "_ADMIN_SCAN_MAX_NAME_BYTES", max_bytes)
    result = rc._scan_worktree_admin_entries(a12.identity.canonical_common_dir, _A12_ID)
    assert result is rc._AdminScanResult[expected]


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 13: materialized `creating`/`present`/`disposing`
# worktrees. Real Git and real filesystem; only the Docker listing is patched.
# ---------------------------------------------------------------------------

import shutil as _shutil

from codeagent import _git_safety as _gs
from codeagent import workspace as _ws

_A13_ID = "e" * 32
_H40 = "a" * 40


def _rec(path, *, head=_H40, kind=b"detached", extra=()):
    fields = [b"worktree " + path.encode()]
    if head is not None:
        fields.append(b"HEAD " + head.encode())
    fields.append(kind)
    fields.extend(extra)
    return b"".join(f + b"\x00" for f in fields) + b"\x00"


def test_a13_parser_accepts_the_real_field_grammar():
    out = (
        _rec("/r", kind=b"branch refs/heads/main")
        + _rec("/w1", extra=(b"locked", b"prunable gitdir file points to non-existent location"))
        + _rec("/w2", extra=(b"locked my reason",))
        + _rec("/bare", head=None, kind=b"bare")
    )
    records = rc._parse_worktree_listing(out, oid_hex_len=40)
    assert [r.path for r in records] == ["/r", "/w1", "/w2", "/bare"]
    assert records[0].kind == "branch" and records[0].branch == "refs/heads/main"
    assert records[1].locked and records[1].prunable
    assert records[2].locked and not records[2].prunable
    assert records[3].kind == "bare" and records[3].head is None
    assert rc._parse_worktree_listing(_rec("/x", head="b" * 64), oid_hex_len=None)[0].head == "b" * 64


@pytest.mark.parametrize(
    "out",
    [
        b"",
        _rec("/a")[:-1],  # missing final record terminator
        b"\x00" + _rec("/a"),  # stray empty field
        _rec("/a", extra=(b"color blue",)),  # unknown key
        _rec("/a", extra=(b"locked", b"locked")),  # duplicate field
        b"HEAD " + _H40.encode() + b"\x00worktree /a\x00detached\x00\x00",  # worktree not first
        _rec("relative/path"),
        _rec("/a", head="b" * 64),  # wrong object-format length
        _rec("/a", head=_H40.upper()),
        _rec("/a", kind=b"detached yes"),
        _rec("/a", head=_H40, kind=b"bare"),  # bare with HEAD
        _rec("/a", kind=b"branch"),  # branch without value
        _rec("/a", kind=b"detached", extra=(b"branch refs/heads/x",)),  # two kinds
        b"worktree /a\x00HEAD " + _H40.encode() + b"\x00\x00",  # no kind
    ],
)
def test_a13_parser_rejects_malformed_listings(out):
    with pytest.raises(rc._WorktreeListingError):
        rc._parse_worktree_listing(out, oid_hex_len=40)


def test_a13_analyzer_counts_target_and_rejects_duplicate_non_targets():
    target = "/state/worktrees/k/" + _A13_ID
    parse = lambda out: rc._parse_worktree_listing(out, oid_hex_len=40)  # noqa: E731
    # Duplicate non-target path: structurally valid, analytically malformed.
    with pytest.raises(rc._WorktreeListingError):
        rc._analyze_worktree_listing(parse(_rec("/r") + _rec("/dup") + _rec("/dup")), target_path=target)
    # Duplicate target path: returned as data ("many"), never malformed.
    many = rc._analyze_worktree_listing(parse(_rec("/r") + _rec(target) + _rec(target)), target_path=target)
    assert many.registration == rc._TargetRegistration("present", "many", None, None, None)
    zero = rc._analyze_worktree_listing(parse(_rec("/r")), target_path=target)
    assert zero.registration == rc._TargetRegistration("absent", 0, None, None, None)
    one = rc._analyze_worktree_listing(parse(_rec("/r") + _rec(target, extra=(b"locked x",))), target_path=target)
    assert one.registration == rc._TargetRegistration("present", 1, False, True, False)
    bare = rc._analyze_worktree_listing(parse(_rec("/r") + _rec(target, head=None, kind=b"bare")), target_path=target)
    assert bare.registration == rc._TargetRegistration("present", 1, True, None, None)


def test_a13_legacy_registered_paths_uses_same_parser_and_rejects_any_duplicate(monkeypatch):
    monkeypatch.setattr(rc, "_run_worktree_listing", lambda root: _rec("/r") + _rec("/t") + _rec("/t"))
    with pytest.raises(rc._GitWorktreeListingError):
        rc._worktree_registered_paths("/repo")
    monkeypatch.setattr(rc, "_run_worktree_listing", lambda root: b"garbage")
    with pytest.raises(rc._GitWorktreeListingError):
        rc._worktree_registered_paths("/repo")
    monkeypatch.setattr(rc, "_run_worktree_listing", lambda root: _rec("/r") + _rec("/t"))
    assert rc._worktree_registered_paths("/repo") == {"/r", "/t"}


def _a13_git_dir(h) -> _Path:
    return _Path(h.identity.canonical_common_dir)


@pytest.mark.parametrize(
    "names, limit, expected",
    [([], None, 0), ([_A13_ID], None, 1), ([_A13_ID, f"{_A13_ID}9"], None, "many"), (["x", "y", "z"], 2, "unknown")],
)
def test_a13_admin_count(harness, monkeypatch, names, limit, expected):
    for name in names:
        (_a13_git_dir(harness) / "worktrees" / name).mkdir(parents=True)
    if limit is not None:
        monkeypatch.setattr(rc, "_ADMIN_SCAN_MAX_ENTRIES", limit)
    assert rc._count_worktree_admin_entries(harness.identity.canonical_common_dir, _A13_ID) == expected


def test_a13_admin_count_close_failure_raises(harness, monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "_dominant_cleanup", _a12_failing_cleanup(calls))
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        rc._count_worktree_admin_entries(harness.identity.canonical_common_dir, _A13_ID)
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED


def _a13_leaf(h) -> _Path:
    return _Path(h.state_root.path) / "worktrees" / h.identity.repo_key / _A13_ID


def _a13_materialize(h):
    """A real, registered, materialized worktree at the deterministic path,
    left behind as a crashed owner would leave it."""
    with h.state_root.reserve_worktree_leaf(h.identity.repo_key, _A13_ID) as reservation:
        wt = _ws.GitWorktree(h.repo, run_id="crashed", reservation=reservation)
        wt.__enter__()
    return _a13_leaf(h)


def _a13_seed(h, intent, *, state=ls.LifecycleState.PREPARING, attempts=0, materialize=True):
    head = _run("git", "-C", str(h.repo), "rev-parse", "HEAD").stdout.strip()
    projection = dataclasses.replace(
        _initial_projection(h, _A13_ID),
        state=state,
        worktree=_wl.WorktreeTransition(intent=_wl.WorktreeIntent(intent), expected_head=head),
        reconciliation=ls.ReconciliationSummary(attempts_total=attempts, recent_failures=()),
    )
    run_dir = _seed_run_dir(h, _A13_ID, projection)
    leaf = _a13_materialize(h) if materialize else _a13_leaf(h)
    return run_dir, leaf


def _a13_registered(h) -> bool:
    out = _run("git", "-C", str(h.repo), "worktree", "list", "--porcelain").stdout
    return f"worktree {os.path.realpath(_a13_leaf(h))}" in out


@pytest.fixture
def a13(harness, monkeypatch):
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    yield harness
    leaf = _a13_leaf(harness)
    if _a13_registered(harness):
        _run("git", "-C", str(harness.repo), "worktree", "unlock", str(leaf), check=False)
        _run("git", "-C", str(harness.repo), "worktree", "remove", "--force", str(leaf), check=False)
    admin = _a13_git_dir(harness) / "worktrees"
    if admin.is_symlink():
        admin.unlink()
    elif admin.is_dir():
        for child in admin.iterdir():
            if child.name.startswith(_A13_ID):
                _shutil.rmtree(child)
    if leaf.is_symlink() or leaf.is_file():
        leaf.unlink()
    elif leaf.exists():
        for root, dirs, _files in os.walk(leaf):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        os.chmod(leaf, 0o700)
        _shutil.rmtree(leaf)


def _a13_event(h, result):
    event, _ = _a12_entry_event(h, result)
    return event["worktree"]


@pytest.mark.parametrize("intent", ["creating", "present", "disposing"])
def test_a13_real_materialized_worktree_is_removed_and_reconciled(a13, intent):
    run_dir, leaf = _a13_seed(a13, intent)
    assert _a13_registered(a13) and (leaf / ".git").is_file()
    result = a13.reconcile()
    entry = result.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
    assert not result.blocked
    assert not _a13_registered(a13) and not os.path.lexists(leaf)
    assert rc._count_worktree_admin_entries(a13.identity.canonical_common_dir, _A13_ID) == 0
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["worktree"] == {"intent": "absent", "expected_head": None}
    assert payload["reconciliation"]["attempts_total"] == 1
    trace = _a13_event(a13, result)
    assert trace["initial_persisted_intent"] == intent
    assert trace["registration_pre"] == {
        "state": "present", "target_records": 1, "target_bare": False, "locked": False, "prunable": False
    }
    assert trace["admin_matches_pre"] == 1 and trace["leaf_pre"] == "materialized"
    assert trace["removal_attempt"] == "exited_zero"
    assert trace["registration_post"] == {
        "state": "absent", "target_records": 0, "target_bare": None, "locked": None, "prunable": None
    }
    assert trace["admin_matches_post"] == 0 and trace["leaf_post"] == "absent"
    assert trace["leaf_outcome"] == "removed_by_git"
    assert trace["disposing_transition_confirmed_this_pass"] is (intent != "disposing")
    assert trace["absent_transition_confirmed_this_pass"] is True


def test_a13_real_sha256_present_worktree_is_reconciled(tmp_path, monkeypatch):
    repo = tmp_path / "repo256"
    init = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(repo)], capture_output=True, text=True
    )
    if init.returncode != 0:
        pytest.skip("installed git does not support --object-format=sha256")
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    _set_state_dir(monkeypatch, tmp_path, "state-256")
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    h = _Harness(repo, tmp_path / "state-256")
    try:
        assert h.identity.object_format == "sha256"
        run_dir, leaf = _a13_seed(h, "present")
        entry = h.reconcile().entries[0]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
        assert not os.path.lexists(leaf)
        assert _read_projection_dict(run_dir)["state"] == "RECONCILED"
    finally:
        h.close()


@pytest.mark.parametrize("intent, expected", [("disposing", "RECONCILED"), ("present", "REFUSED"), ("creating", "REFUSED")])
def test_a13_registered_but_directory_missing_d2_only_for_disposing(a13, intent, expected):
    run_dir, leaf = _a13_seed(a13, intent)
    _shutil.rmtree(leaf)
    assert _a13_registered(a13)
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome[expected], entry.detail
    if expected == "RECONCILED":
        assert not _a13_registered(a13)
        assert rc._count_worktree_admin_entries(a13.identity.canonical_common_dir, _A13_ID) == 0
    else:
        assert _a13_registered(a13)  # nothing changed
        assert entry.worktree_removal_attempt == "not_attempted"
        assert _read_projection_dict(run_dir)["state"] == "PREPARING"


def test_a13_disposing_with_nothing_left_collapses_without_git(a13, monkeypatch):
    run_dir, _ = _a13_seed(a13, "disposing", materialize=False)
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: pytest.fail("no removal for a collapse"))
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.worktree_leaf_outcome == "already_absent"
    assert entry.worktree_removal_attempt == "not_attempted"
    assert _read_projection_dict(run_dir)["state"] == "RECONCILED"


def test_a13_present_with_nothing_left_is_refused(a13):
    run_dir, _ = _a13_seed(a13, "present", materialize=False)
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert _read_projection_dict(run_dir)["state"] == "PREPARING"


@pytest.mark.parametrize(
    "setup", ["locked", "missing_git_file", "extra_admin", "unregistered_leftover", "leaf_symlink_swap"]
)
@pytest.mark.parametrize("intent", ["present", "disposing"])
def test_a13_pre_removal_refusals_change_nothing(a13, monkeypatch, tmp_path, setup, intent):
    run_dir, leaf = _a13_seed(a13, intent)
    if setup == "locked":
        _run("git", "-C", str(a13.repo), "worktree", "lock", str(leaf))
    elif setup == "missing_git_file":
        (leaf / ".git").unlink()
    elif setup == "extra_admin":
        (_a13_git_dir(a13) / "worktrees" / f"{_A13_ID}9").mkdir()
    elif setup == "unregistered_leftover":
        _shutil.rmtree(_a13_git_dir(a13) / "worktrees" / _A13_ID)
    elif setup == "leaf_symlink_swap":
        os.rename(leaf, tmp_path / "moved")
        os.symlink(tmp_path / "moved", leaf)
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: pytest.fail("must not attempt removal"))
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED, (setup, entry.detail)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "PREPARING" and payload["worktree"]["intent"] == intent
    if setup == "leaf_symlink_swap":
        os.unlink(leaf)
        os.rename(tmp_path / "moved", leaf)


def test_a13_unregistered_creating_leftover_routes_to_amendment_12_and_is_refused(a13, monkeypatch):
    run_dir, leaf = _a13_seed(a13, "creating")
    _shutil.rmtree(_a13_git_dir(a13) / "worktrees" / _A13_ID)
    monkeypatch.setattr(
        sr.StateRoot, "observe_materialized_worktree_leaf", lambda *a: pytest.fail("Amendment 13 observer must not run")
    )
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED  # Amendment 12: non-empty leaf -> CONFLICT
    assert entry.worktree_leaf_outcome == "conflict_not_removed"
    assert leaf.is_dir()


@pytest.mark.parametrize("setup", ["checkpoint_ref", "container"])
def test_a13_checkpoint_ref_or_container_present_is_refused(a13, monkeypatch, setup):
    run_dir, leaf = _a13_seed(a13, "present")
    if setup == "checkpoint_ref":
        monkeypatch.setattr(rc.CheckpointRef, "observe", lambda self: cr.RefObservation(present=True, oid="0" * 40))
    else:
        monkeypatch.setattr(
            rc, "_docker_ps_all_id_name_pairs", lambda: _listing({f"codeagent-verification-{_A13_ID}": "f" * 64})
        )
    entry = a13.reconcile().entries[0]
    monkeypatch.undo()
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert _a13_registered(a13) and leaf.is_dir()


def test_a13_symlink_inside_worktree_and_hostile_filter_are_harmless(a13, tmp_path):
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "keep.txt").write_text("keep")
    marker = tmp_path / "FILTER_RAN"
    _run("git", "-C", str(a13.repo), "config", "filter.spy.clean", f"touch {marker}; cat")
    run_dir, leaf = _a13_seed(a13, "present")
    os.symlink(canary, leaf / "link-out")
    (leaf / ".gitattributes").write_text("* filter=spy\n")
    (leaf / "f.txt").write_text("changed")
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert (canary / "keep.txt").read_text() == "keep"
    assert not marker.exists()


def _a13_obs(state="present", records=1, bare=False, locked=False, prunable=False, admin=1, leaf="materialized"):
    if state == "unknown":
        registration = rc._UNKNOWN_REGISTRATION
    elif records == 0:
        registration = rc._TargetRegistration("absent", 0, None, None, None)
    elif records == "many":
        registration = rc._TargetRegistration("present", "many", None, None, None)
    elif bare:
        registration = rc._TargetRegistration("present", 1, True, None, None)
    else:
        registration = rc._TargetRegistration("present", 1, False, locked, prunable)
    return rc._WorktreeObservation(registration, admin, leaf)


@pytest.mark.parametrize(
    "obs, outcome, leaf_outcome",
    [
        (_a13_obs(state="unknown"), "SUBSTRATE_UNAVAILABLE", "removal_unconfirmed"),
        (_a13_obs(admin="unknown"), "SUBSTRATE_UNAVAILABLE", "removal_unconfirmed"),
        (_a13_obs(leaf="unknown"), "SUBSTRATE_UNAVAILABLE", "removal_unconfirmed"),
        (_a13_obs(leaf="conflict"), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(records="many"), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(bare=True), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(locked=True), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(admin=0), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(admin="many"), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(), "FAILED", "removal_unconfirmed"),
        (_a13_obs(leaf="absent"), "FAILED", "registered_directory_missing"),
        (_a13_obs(records=0, admin=1, leaf="absent"), "REFUSED", "removal_unconfirmed"),
        (_a13_obs(records=0, admin=0), "FAILED", "partial_removal_leftover"),
        (_a13_obs(records=0, admin=0, leaf="absent"), "RECONCILED", "removed_by_git"),
    ],
)
def test_a13_after_command_outcome_table(a13, monkeypatch, obs, outcome, leaf_outcome):
    run_dir, _ = _a13_seed(a13, "present")
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: "exited_zero")
    monkeypatch.setattr(rc, "_observe_worktree", lambda **k: obs)
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], entry.detail
    assert entry.worktree_leaf_outcome == leaf_outcome
    payload = _read_projection_dict(run_dir)
    if outcome == "RECONCILED":
        assert payload["state"] == "RECONCILED" and entry.worktree_absent_transition_confirmed_this_pass
    else:
        assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "disposing"
        assert entry.worktree_absent_transition_confirmed_this_pass is False


@pytest.mark.parametrize("reason", [lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE])
def test_a13_post_observation_descriptor_failures_dominate(a13, monkeypatch, reason):
    run_dir, _ = _a13_seed(a13, "present")
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: "exited_zero")
    monkeypatch.setattr(rc, "_observe_worktree", lambda **k: (_ for _ in ()).throw(lf.LifecycleFsError(reason, "x")))
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.worktree_leaf_outcome == (
        "close_failed" if reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED else "removal_unconfirmed"
    )
    assert _read_projection_dict(run_dir)["worktree"]["intent"] == "disposing"


@pytest.mark.parametrize(
    "reason, attempt, outcome, observed",
    [
        (_gs.GitSafetyFailure.BOUNDED_COMMAND_FAILED, "exited_nonzero", "FAILED", True),
        (_gs.GitSafetyFailure.GIT_COMMAND_TIMEOUT, "stopped_after_failure", "FAILED", True),
        (_gs.GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE, "stopped_after_failure", "FAILED", True),
        (_gs.GitSafetyFailure.PROCESS_SETUP_FAILED, "stopped_after_failure", "FAILED", True),
        (_gs.GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED, "cleanup_unconfirmed", "SUBSTRATE_UNAVAILABLE", False),
        (_gs.GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE, "launch_failed", "SUBSTRATE_UNAVAILABLE", False),
        (_gs.GitSafetyFailure.MALFORMED_OID, "cleanup_unconfirmed", "SUBSTRATE_UNAVAILABLE", False),  # defensive fallback
    ],
)
def test_a13_command_failure_table(a13, monkeypatch, reason, attempt, outcome, observed):
    run_dir, leaf = _a13_seed(a13, "present")
    real = rc.run_git_bounded

    def fake(root, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            raise _gs.GitSafetyError(reason, "simulated")
        return real(root, *args, **kwargs)

    monkeypatch.setattr(rc, "run_git_bounded", fake)
    entry = a13.reconcile().entries[0]
    monkeypatch.undo()
    assert entry.worktree_removal_attempt == attempt
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome]
    assert (entry.worktree_registration_post is not None) is observed
    assert (entry.worktree_leaf_post is not None) is observed
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "disposing"
    assert entry.worktree_absent_transition_confirmed_this_pass is False
    assert _a13_registered(a13) and leaf.is_dir()  # the fake changed nothing


def test_a13_deterministic_registration_removed_but_directory_remains(a13, monkeypatch):
    """Portable model of a partial removal: the 'command' deletes only the
    Git admin entry. Fresh observation: unregistered, no admin entry, a
    materialized directory -> FAILED now, REFUSED on every later pass."""
    run_dir, leaf = _a13_seed(a13, "present")

    def partial(root, target):
        _shutil.rmtree(_a13_git_dir(a13) / "worktrees" / _A13_ID)
        return "exited_nonzero"

    monkeypatch.setattr(rc, "_attempt_worktree_remove", partial)
    first = a13.reconcile().entries[0]
    assert first.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert first.worktree_leaf_outcome == "partial_removal_leftover"
    monkeypatch.undo()
    second = a13.reconcile().entries[0]
    assert second.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert leaf.is_dir()
    assert _read_projection_dict(run_dir)["worktree"]["intent"] == "disposing"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks are bypassed for root")
def test_a13_conditional_real_partial_removal_after_permission_failure(a13, tmp_path):
    """Conditional evidence only (the portable proof is the deterministic
    test above): runs only if this host's Git reproduces a partial removal."""
    probe_repo = _make_repo(tmp_path, "probe")
    probe_wt = tmp_path / "probe-wt"
    _run("git", "-C", str(probe_repo), "worktree", "add", "-q", "--detach", str(probe_wt))
    (probe_wt / "sub").mkdir()
    (probe_wt / "sub" / "f").write_text("x")
    os.chmod(probe_wt / "sub", 0o500)
    _run("git", "-C", str(probe_repo), "worktree", "remove", "--force", str(probe_wt), check=False)
    reproduced = probe_wt.exists() and str(probe_wt) not in _run(
        "git", "-C", str(probe_repo), "worktree", "list", "--porcelain"
    ).stdout.replace(os.path.realpath(probe_wt), str(probe_wt))
    os.chmod(probe_wt / "sub", 0o700) if (probe_wt / "sub").exists() else None
    if not reproduced:
        pytest.skip("this host's git does not reproduce a partial removal")
    run_dir, leaf = _a13_seed(a13, "present")
    (leaf / "sub").mkdir()
    (leaf / "sub" / "f").write_text("x")
    os.chmod(leaf / "sub", 0o500)
    entry = a13.reconcile().entries[0]
    os.chmod(leaf / "sub", 0o700)
    assert entry.outcome in (rc.ReconciliationEntryOutcome.FAILED, rc.ReconciliationEntryOutcome.REFUSED)
    assert _read_projection_dict(run_dir)["worktree"]["intent"] == "disposing"


# Publication order for a fresh `present` entry: #1 RECONCILING (M1),
# #2 present->disposing (M2), #3 disposing->absent (M5), #4 RECONCILED (M6).
@pytest.mark.parametrize(
    "nth, installed, disk_state, disk_worktree, removed",
    [
        (1, False, "PREPARING", "present", False),
        (1, True, "RECONCILING", "present", False),
        (2, False, "RECONCILING", "present", False),
        (2, True, "RECONCILING", "disposing", False),
        (3, False, "RECONCILING", "disposing", True),
        (3, True, "RECONCILING", "absent", True),
        (4, False, "RECONCILING", "absent", True),
        (4, True, "RECONCILED", "absent", True),
    ],
)
def test_a13_publication_failures_and_resume(a13, monkeypatch, nth, installed, disk_state, disk_worktree, removed):
    run_dir, leaf = _a13_seed(a13, "present")
    _a12_publish_fault(monkeypatch, nth, installed=installed)
    first = a13.reconcile()
    monkeypatch.setattr(ls, "publish_private_file_atomically_at", lf.publish_private_file_atomically_at)
    entry = first.entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    assert entry.worktree_disposing_transition_confirmed_this_pass is (nth > 2)
    assert entry.worktree_absent_transition_confirmed_this_pass is (nth > 3)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == disk_state and payload["worktree"]["intent"] == disk_worktree
    assert (not _a13_registered(a13)) is removed
    second = a13.reconcile()
    assert not second.blocked, second.entries[0].detail
    final = _read_projection_dict(run_dir)
    assert final["state"] == "RECONCILED" and final["worktree"]["intent"] == "absent"
    assert final["reconciliation"]["attempts_total"] == 1
    assert not _a13_registered(a13) and not os.path.lexists(leaf)


@pytest.mark.parametrize("intent", ["present", "disposing"])
def test_a13_resume_from_reconciling_keeps_attempts(a13, intent):
    run_dir, leaf = _a13_seed(a13, intent, state=ls.LifecycleState.RECONCILING, attempts=1)
    entry = a13.reconcile().entries[0]
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert entry.worktree_disposing_transition_confirmed_this_pass is (intent == "present")
    assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 1


def test_a13_maintenance_event_fits_bound_with_every_new_field_maximal(tmp_path):
    reg = {"state": "unknown", "target_records": "many", "target_bare": False, "locked": False, "prunable": False}
    fd = os.open(tmp_path / "trace.jsonl", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        writer = rc._MaintenanceTraceWriter(fd=fd, maintenance_id="f" * 32, state_root_id="s" * 32, repo_key="r" * 32)
        writer.entry_recorded(
            rc.ReconciliationEntryResult(
                "a" * 32,
                rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE,
                "x" * 2000,
                run_id="r" * 2000,
                attempt_number=10**9,
                baseline_id="b" * 64,
                verification_id="c" * 64,
                worktree_initial_persisted_intent="disposing",
                worktree_leaf_outcome="registered_directory_missing",
                worktree_removal_observation="pre_inspection_failed",
                worktree_absent_transition_confirmed_this_pass=True,
                worktree_registration_pre=reg,
                worktree_registration_post=reg,
                worktree_admin_matches_pre="unknown",
                worktree_admin_matches_post="unknown",
                worktree_leaf_pre="materialized",
                worktree_leaf_post="materialized",
                worktree_removal_attempt="stopped_after_failure",
                worktree_disposing_transition_confirmed_this_pass=True,
            )
        )
    finally:
        os.close(fd)
    assert len((tmp_path / "trace.jsonl").read_bytes()) <= rc.MAINTENANCE_EVENT_MAX_BYTES


def test_a13_exactly_one_git_mutation_argv_and_single_force():
    import inspect

    source = inspect.getsource(rc)
    tree = ast.parse(source)
    removes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            consts = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "remove" in consts and "worktree" in consts:
                removes.append(consts)
    # Exactly one Git mutation call, carrying exactly one `--force` (never
    # `-f -f`); the module's only other `--force` is the pre-existing
    # `docker rm --force` by immutable container id.
    assert removes == [["worktree", "remove", "--force"]]


def _a13_trap_observer(monkeypatch):
    """Replace the Amendment 13 leaf observer with one that counts its
    calls and fails with CLEANUP_UNCONFIRMED -- so any row that wrongly
    observes the leaf would turn into SUBSTRATE_UNAVAILABLE."""
    calls = {"n": 0}

    def trap(self, repo_key, lifecycle_id):
        calls["n"] += 1
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "would-be leaf cleanup failure")

    monkeypatch.setattr(sr.StateRoot, "observe_materialized_worktree_leaf", trap)
    return calls


@pytest.mark.parametrize(
    "intent, registration, admin, real_setup, outcome",
    [
        ("present", None, "unknown", None, "SUBSTRATE_UNAVAILABLE"),
        ("present", rc._TargetRegistration("present", "many", None, None, None), None, None, "REFUSED"),
        ("present", rc._TargetRegistration("present", 1, True, None, None), None, None, "REFUSED"),
        ("present", None, None, "lock", "REFUSED"),
        ("disposing", None, None, "lock", "REFUSED"),
        ("present", None, 0, None, "REFUSED"),
        ("present", None, None, "extra_admin", "REFUSED"),
        ("disposing", None, "many", None, "REFUSED"),
        ("present", None, None, "unregister", "REFUSED"),
        ("present", rc._TargetRegistration("absent", 0, None, None, None), 1, None, "REFUSED"),
        ("disposing", rc._TargetRegistration("absent", 0, None, None, None), 1, None, "REFUSED"),
        ("disposing", rc._TargetRegistration("absent", 0, None, None, None), "many", None, "REFUSED"),
    ],
)
def test_a13_leaf_observer_never_runs_when_evidence_already_decides(
    a13, monkeypatch, intent, registration, admin, real_setup, outcome
):
    run_dir, leaf = _a13_seed(a13, intent)
    if real_setup == "lock":
        _run("git", "-C", str(a13.repo), "worktree", "lock", str(leaf))
    elif real_setup == "extra_admin":
        (_a13_git_dir(a13) / "worktrees" / f"{_A13_ID}9").mkdir()
    elif real_setup == "unregister":
        _shutil.rmtree(_a13_git_dir(a13) / "worktrees" / _A13_ID)  # 0 registrations, 0 admin entries
    if registration is not None:
        monkeypatch.setattr(rc, "_observe_target_registration", lambda *a, **k: registration)
    if admin is not None:
        monkeypatch.setattr(rc, "_count_worktree_admin_entries", lambda *a: admin)
    calls = _a13_trap_observer(monkeypatch)
    entry = a13.reconcile().entries[0]
    assert calls["n"] == 0
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], entry.detail
    assert entry.worktree_leaf_pre is None
    assert entry.worktree_removal_attempt == "not_attempted"
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "PREPARING" and payload["worktree"]["intent"] == intent


def test_a13_unknown_registration_never_reaches_the_leaf_observer(a13, monkeypatch):
    run_dir, _ = _a13_seed(a13, "present")
    monkeypatch.setattr(rc, "_observe_target_registration", lambda *a, **k: rc._UNKNOWN_REGISTRATION)
    calls = _a13_trap_observer(monkeypatch)
    entry = a13.reconcile().entries[0]
    assert calls["n"] == 0
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert _read_projection_dict(run_dir)["state"] == "PREPARING"


@pytest.mark.parametrize("intent, materialize", [("present", True), ("disposing", True), ("disposing", False)])
def test_a13_leaf_observer_runs_exactly_once_where_its_result_is_needed(a13, monkeypatch, intent, materialize):
    """One eligible registration with one admin entry, or `disposing` with
    neither: the observer runs once, so its cleanup failure now (and only
    now) dominates as SUBSTRATE_UNAVAILABLE."""
    run_dir, _ = _a13_seed(a13, intent, materialize=materialize)
    calls = _a13_trap_observer(monkeypatch)
    entry = a13.reconcile().entries[0]
    assert calls["n"] == 1
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.worktree_leaf_outcome == "close_failed"
    assert _read_projection_dict(run_dir)["state"] == "PREPARING"


# ---------------------------------------------------------------------------
# Final whole-family container leftover check (keep last)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 16: checkpoint-ref reconciliation for a dead entry whose
# worktree and containers are already absent. Real Git throughout; only the
# reconciler's Docker listing is patched (no container is involved).
# ---------------------------------------------------------------------------

_A16_ID = "a16" + "0" * 28 + "6"
_A16_REF = f"refs/codeagent/runs/{_A16_ID}/checkpoint"


def _a16_commit(repo, message):
    """A new commit object that moves no branch."""
    return _run(
        "git", "-C", str(repo), "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", message
    ).stdout.strip()


def _a16_set(repo, sha, ref=_A16_REF):
    _run("git", "-C", str(repo), "update-ref", ref, sha)


def _a16_value(repo, ref=_A16_REF):
    out = _run("git", "-C", str(repo), "for-each-ref", "--format=%(objectname) %(symref)", ref).stdout.strip()
    return out or None


def _a16_refs_snapshot(repo):
    return _run("git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname) %(symref)").stdout


def _a16_symbolic(repo, *, dangling):
    """A symbolic ref at the owned name: dangling (a target that never
    exists) or resolvable (to a created branch). Deterministic on any host
    default branch name."""
    if dangling:
        target = "refs/heads/a16-does-not-exist"
    else:
        target = "refs/heads/a16-pin"
        _run("git", "-C", str(repo), "update-ref", target, "HEAD")
    _run("git", "-C", str(repo), "symbolic-ref", _A16_REF, target)


def _a16_record(intent, A, B=None):
    if intent == "creating":
        return cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=A)
    if intent == "present":
        return cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=A)
    if intent == "advancing":
        return cs.CheckpointTransition(
            intent=cs.CheckpointIntent.ADVANCING, accepted_sha=A, expected_old_sha=A, proposed_new_sha=B
        )
    return cs.CheckpointTransition(intent=cs.CheckpointIntent.REMOVING, accepted_sha=A, expected_old_sha=A)


def _a16_seed(h, transition, *, state=ls.LifecycleState.ACTIVE, attempts=0):
    projection = dataclasses.replace(
        _initial_projection(h, _A16_ID),
        state=state,
        checkpoint_ref=transition,
        reconciliation=ls.ReconciliationSummary(attempts_total=attempts, recent_failures=()),
    )
    return _seed_run_dir(h, _A16_ID, projection)


def _a16_entry(result):
    (entry,) = [e for e in result.entries if e.lifecycle_id == _A16_ID]
    return entry


def _a16_trace_events(h):
    events = []
    for trace in sorted((h.repo_dir() / rc.MAINTENANCE_DIRNAME).glob("*.jsonl")):
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            if event.get("lifecycle_id") == _A16_ID:
                events.append(event)
    return events


def _a16_assert_no_sha_in_trace(h, *shas):
    raw = "".join(p.read_text() for p in (h.repo_dir() / rc.MAINTENANCE_DIRNAME).glob("*.jsonl"))
    for sha in shas:
        if sha:
            assert sha not in raw


@pytest.fixture
def a16(harness, monkeypatch):
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    harness.A = _a16_commit(harness.repo, "A")
    harness.B = _a16_commit(harness.repo, "B")
    harness.C = _a16_commit(harness.repo, "C")
    yield harness
    for ref in _run("git", "-C", str(harness.repo), "for-each-ref", "--format=%(refname)", "refs/codeagent").stdout.split():
        _run("git", "-C", str(harness.repo), "update-ref", "-d", "--no-deref", ref, check=False)


# (intent, live, outcome, role, attempt)
_A16_MATRIX = [
    ("creating", "A", "RECONCILED", "proposed", "applied"),
    ("creating", "absent", "RECONCILED", None, "not_attempted"),
    ("creating", "C", "REFUSED", None, "not_attempted"),
    ("creating", "symbolic", "REFUSED", None, "not_attempted"),
    ("present", "A", "RECONCILED", "accepted", "applied"),
    ("present", "absent", "RECONCILED", None, "not_attempted"),
    ("present", "C", "REFUSED", None, "not_attempted"),
    ("present", "symbolic", "REFUSED", None, "not_attempted"),
    ("advancing", "A", "RECONCILED", "accepted", "applied"),
    ("advancing", "B", "RECONCILED", "proposed", "applied"),
    ("advancing", "absent", "RECONCILED", None, "not_attempted"),
    ("advancing", "C", "REFUSED", None, "not_attempted"),
    ("advancing", "symbolic", "REFUSED", None, "not_attempted"),
    ("removing", "A", "RECONCILED", "accepted", "applied"),
    ("removing", "absent", "RECONCILED", None, "not_attempted"),
    ("removing", "B", "REFUSED", None, "not_attempted"),
    ("removing", "symbolic", "REFUSED", None, "not_attempted"),
]


@pytest.mark.parametrize("intent, live, outcome, role, attempt", _A16_MATRIX)
def test_a16_intent_by_live_ref_matrix(a16, intent, live, outcome, role, attempt):
    h = a16
    run_dir = _a16_seed(h, _a16_record(intent, h.A, h.B))
    if live == "symbolic":
        _a16_symbolic(h.repo, dangling=True)
    elif live != "absent":
        _a16_set(h.repo, getattr(h, live))
    before_value = _a16_value(h.repo)
    before_projection = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()

    result = h.reconcile()
    entry = _a16_entry(result)
    assert entry.outcome.value == outcome.lower()
    assert entry.checkpoint_ref_initial_persisted_intent == intent
    assert entry.checkpoint_ref_candidate_role == role
    assert entry.checkpoint_ref_removal_attempt == attempt
    expected_pre = {"absent": "absent", "symbolic": "symbolic"}.get(live) or (
        f"candidate_{role}" if role else "unexpected"
    )
    assert entry.checkpoint_ref_observation_pre == expected_pre

    payload = _read_projection_dict(run_dir)
    if outcome == "RECONCILED":
        assert not result.blocked
        assert _a16_value(h.repo) is None
        assert payload["state"] == "RECONCILED"
        assert payload["checkpoint_ref"]["intent"] == "absent"
        assert payload["reconciliation"]["attempts_total"] == 1
        assert entry.checkpoint_ref_absent_transition_confirmed_this_pass
        assert entry.checkpoint_ref_removing_transition_confirmed_this_pass is (
            role is not None and intent != "removing"
        )
    else:
        assert result.blocked
        assert _a16_value(h.repo) == before_value  # zero Git mutation
        assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before_projection  # zero write
    (event,) = _a16_trace_events(h)[-1:]
    trace = event["checkpoint_ref"]
    assert trace["initial_persisted_intent"] == intent
    assert trace["observation_pre"] == expected_pre
    assert trace["candidate_role"] == role
    assert trace["removal_attempt"] == attempt
    _a16_assert_no_sha_in_trace(h, h.A, h.B, h.C)


@pytest.mark.parametrize("intent", ["creating", "present", "advancing", "removing"])
def test_a16_resolvable_symbolic_ref_is_refused_with_zero_mutation(a16, intent):
    h = a16
    run_dir = _a16_seed(h, _a16_record(intent, h.A, h.B))
    _a16_symbolic(h.repo, dangling=False)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.checkpoint_ref_observation_pre == "symbolic"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _run("git", "-C", str(h.repo), "symbolic-ref", _A16_REF).stdout.strip() == "refs/heads/a16-pin"


def test_a16_real_sha256_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo256"
    init = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(repo)], capture_output=True, text=True
    )
    if init.returncode != 0:
        pytest.skip("installed git does not support --object-format=sha256")
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    h = _Harness(repo, state_dir)
    try:
        assert h.identity.object_format == "sha256"
        A, B = _a16_commit(repo, "A"), _a16_commit(repo, "B")
        assert len(A) == 64
        run_dir = _a16_seed(h, _a16_record("advancing", A, B))
        _a16_set(repo, B)
        entry = _a16_entry(h.reconcile())
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.checkpoint_ref_candidate_role == "proposed"
        assert _a16_value(repo) is None
        assert _read_projection_dict(run_dir)["state"] == "RECONCILED"
        _a16_assert_no_sha_in_trace(h, A, B)
    finally:
        h.close()


def test_a16_ambiguous_observation_is_substrate_unavailable(a16):
    """A ref nested under the checkpoint name makes the exact-name listing
    report a different refname: ambiguous, never treated as absent."""
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A, ref=f"{_A16_REF}/nested")
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.checkpoint_ref_observation_pre == "ambiguous"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_value(h.repo, ref=f"{_A16_REF}/nested").startswith(h.A)


def test_a16_observation_failure_is_substrate_unavailable(a16, monkeypatch):
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()

    def fail(self):
        raise cr.CheckpointRefError(cr.CheckpointRefFailure.OBSERVATION_FAILED, "x", outcome=cr.MutationOutcome.UNKNOWN)

    monkeypatch.setattr(rc.CheckpointRef, "observe", fail)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.checkpoint_ref_observation_pre == "unknown"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_value(h.repo).startswith(h.A)


def test_a16_object_format_mismatch_is_refused(a16, monkeypatch):
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    monkeypatch.setattr(rc.CheckpointRef, "object_format", property(lambda self: cr.ObjectFormat.SHA256))
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_value(h.repo).startswith(h.A)


_A16_DELETE_OUTCOMES = [
    pytest.param(cr.CheckpointRefFailure.GIT_COMMAND_TIMEOUT, cr.MutationOutcome.UNCHANGED, "FAILED", "unchanged", id="unchanged"),
    pytest.param(cr.CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, cr.MutationOutcome.UNCHANGED, "FAILED", "unchanged", id="cas-rejected"),
    pytest.param(cr.CheckpointRefFailure.UNEXPECTED_VALUE, cr.MutationOutcome.UNEXPECTED, "REFUSED", "unexpected", id="unexpected"),
    pytest.param(cr.CheckpointRefFailure.SYMBOLIC_REF, cr.MutationOutcome.SYMBOLIC, "REFUSED", "symbolic", id="symbolic"),
    pytest.param(cr.CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN, cr.MutationOutcome.UNKNOWN, "SUBSTRATE_UNAVAILABLE", "unknown", id="unknown"),
    pytest.param(
        cr.CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED,
        cr.MutationOutcome.UNCHANGED,
        "SUBSTRATE_UNAVAILABLE",
        "unknown",
        id="transaction-cleanup-unconfirmed-dominates",
    ),
    pytest.param(
        cr.CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN,
        cr.MutationOutcome.APPLIED,
        "SUBSTRATE_UNAVAILABLE",
        "unknown",
        id="error-claiming-applied-is-degraded",
    ),
]


@pytest.mark.parametrize("reason, mutation, outcome, category", _A16_DELETE_OUTCOMES)
def test_a16_every_delete_mutation_outcome(a16, monkeypatch, reason, mutation, outcome, category):
    """Only a normal return is applied. Every failure leaves the write-ahead
    `removing(observed)` record installed, the state RECONCILING, and the
    count incremented exactly once."""
    h = a16
    run_dir = _a16_seed(h, _a16_record("advancing", h.A, h.B))
    _a16_set(h.repo, h.B)
    calls = []

    def fake_delete(self, *, expected_oid):
        calls.append(expected_oid)
        raise cr.CheckpointRefError(reason, "injected", outcome=mutation)

    monkeypatch.setattr(rc.CheckpointRef, "delete", fake_delete)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome.value == outcome.lower()
    assert entry.checkpoint_ref_removal_attempt == category
    assert calls == [h.B]
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING"
    assert payload["reconciliation"]["attempts_total"] == 1
    assert payload["checkpoint_ref"] == {
        "intent": "removing", "accepted_sha": h.B, "expected_old_sha": h.B, "proposed_new_sha": None,
    }
    assert _a16_value(h.repo).startswith(h.B)


def test_a16_delete_is_the_compare_and_swap_primitive_with_the_observed_value(a16, monkeypatch):
    h = a16
    _a16_seed(h, _a16_record("advancing", h.A, h.B))
    _a16_set(h.repo, h.A)
    seen = []
    real = rc.CheckpointRef.delete

    def spy(self, *, expected_oid):
        seen.append((self.ref_name, expected_oid))
        return real(self, expected_oid=expected_oid)

    monkeypatch.setattr(rc.CheckpointRef, "delete", spy)
    assert _a16_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert seen == [(_A16_REF, h.A)]


@pytest.mark.parametrize("race", ["moved", "deleted"])
def test_a16_compare_and_swap_race_is_refused_and_never_deletes_a_moved_ref(a16, monkeypatch, race):
    """The ref changes between the reconciler's observation and the real
    delete: the primitive's own pre-check refuses (UNEXPECTED -> REFUSED),
    a moved ref is never deleted, and the next pass resolves from scratch."""
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    real = rc.CheckpointRef.delete

    def racing(self, *, expected_oid):
        if race == "moved":
            _a16_set(h.repo, h.C)
        else:
            _run("git", "-C", str(h.repo), "update-ref", "-d", _A16_REF)
        return real(self, expected_oid=expected_oid)

    monkeypatch.setattr(rc.CheckpointRef, "delete", racing)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.checkpoint_ref_removal_attempt == "unexpected"
    assert _read_projection_dict(run_dir)["checkpoint_ref"]["intent"] == "removing"
    monkeypatch.setattr(rc.CheckpointRef, "delete", real)
    if race == "moved":
        assert _a16_value(h.repo).startswith(h.C)  # the moved ref survives
        assert _a16_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.REFUSED
        assert _a16_value(h.repo).startswith(h.C)
    else:
        resumed = _a16_entry(h.reconcile())
        assert resumed.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert resumed.checkpoint_ref_observation_pre == "absent"
        assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 1


def _a16_hold_lifecycle_lock(run_dir, ready_evt, release_evt):
    fd = os.open(os.path.join(run_dir, "lifecycle.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready_evt.set()
    release_evt.wait(timeout=30)
    os.close(fd)


def test_a16_held_lifecycle_lock_is_skipped_active_with_zero_mutation(a16):
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    ctx = multiprocessing.get_context("spawn")
    ready_evt, release_evt = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_a16_hold_lifecycle_lock, args=(str(run_dir), ready_evt, release_evt))
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        result = h.reconcile()
        assert _a16_entry(result).outcome is rc.ReconciliationEntryOutcome.SKIPPED_ACTIVE
        assert result.blocked
        assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
        assert _a16_value(h.repo).startswith(h.A)
    finally:
        release_evt.set()
        proc.join(timeout=10)


def _a16_leaf(h):
    return h.state_dir / "worktrees" / h.identity.repo_key / _A16_ID


def _a16_common_dir(h):
    return os.path.realpath(h.identity.canonical_common_dir)


@pytest.mark.parametrize(
    "blocker, outcome",
    [
        ("container_present", "REFUSED"),
        ("container_listing_fails", "SUBSTRATE_UNAVAILABLE"),
        ("worktree_registered", "REFUSED"),
        ("admin_entry", "REFUSED"),
        ("leaf_present", "REFUSED"),
        ("leaf_symlink", "REFUSED"),
        ("worktree_listing_fails", "SUBSTRATE_UNAVAILABLE"),
    ],
)
def test_a16_blockers_mutate_nothing(a16, monkeypatch, blocker, outcome):
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    leaf = _a16_leaf(h)
    created_admin = None
    if blocker == "container_present":
        monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _listing({f"codeagent-verification-{_A16_ID}": "e" * 64}))
    elif blocker == "container_listing_fails":
        monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: (_ for _ in ()).throw(rc._DockerListingError("x")))
    elif blocker == "worktree_registered":
        leaf.parent.mkdir(parents=True, exist_ok=True)
        _run("git", "-C", str(h.repo), "worktree", "add", "-q", "--detach", str(leaf))
    elif blocker == "admin_entry":
        created_admin = os.path.join(_a16_common_dir(h), "worktrees", _A16_ID)
        os.makedirs(created_admin)
    elif blocker == "leaf_present":
        leaf.mkdir(parents=True, mode=0o700)
    elif blocker == "leaf_symlink":
        leaf.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(h.state_dir, leaf)
    elif blocker == "worktree_listing_fails":
        monkeypatch.setattr(rc, "_worktree_registered_paths", lambda root: (_ for _ in ()).throw(rc._GitWorktreeListingError("x")))
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    try:
        entry = _a16_entry(h.reconcile())
        assert entry.outcome.value == outcome.lower()
        assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
        assert _a16_value(h.repo).startswith(h.A)
        assert entry.checkpoint_ref_removal_attempt == "not_attempted"
    finally:
        if blocker == "worktree_registered":
            _run("git", "-C", str(h.repo), "worktree", "remove", "--force", str(leaf))
        if created_admin:
            os.rmdir(created_admin)
        if leaf.is_symlink():
            leaf.unlink()
        elif leaf.is_dir():
            os.rmdir(leaf)


@pytest.mark.parametrize(
    "shape",
    ["worktree_present", "baseline_creating", "verification_present", "failure_set", "terminal_complete"],
)
def test_a16_ineligible_ref_shapes_stay_refused(a16, shape):
    """A ref alongside a container record is deliberately not admitted;
    neither is a populated failure or a terminal state with a non-absent
    ref. A ref alongside a `present` worktree record is now Amendment 17's
    chained shape, but with no real registered worktree it stays REFUSED by
    Amendment 13's worktree gate, before any mutation."""
    h = a16
    transition = _a16_record("present", h.A)
    projection = dataclasses.replace(_initial_projection(h, _A16_ID), state=ls.LifecycleState.ACTIVE, checkpoint_ref=transition)
    if shape == "worktree_present":
        projection = dataclasses.replace(
            projection, worktree=_wl.WorktreeTransition(intent=_wl.WorktreeIntent.PRESENT, expected_head=h.A)
        )
    elif shape == "baseline_creating":
        projection = dataclasses.replace(projection, baseline=ls.ContainerAttribution(intent=ls.ContainerIntent.CREATING, id=None))
    elif shape == "verification_present":
        projection = dataclasses.replace(
            projection, verification=ls.ContainerAttribution(intent=ls.ContainerIntent.PRESENT, id="e" * 64)
        )
    elif shape == "failure_set":
        projection = dataclasses.replace(projection, failure=ls.FailureDetail(phase="p", detail="d"))
    else:
        projection = dataclasses.replace(projection, state=ls.LifecycleState.COMPLETE)
    run_dir = _seed_run_dir(h, _A16_ID, projection)
    _a16_set(h.repo, h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    result = h.reconcile()
    assert _a16_entry(result).outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert result.blocked
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_value(h.repo).startswith(h.A)


def _a16_write_fault(monkeypatch, *, index, kind, deletes):
    """Fail the `index`-th projection write of the row (1 RECONCILING, 2
    removing, 3 absent, 4 RECONCILED). `publication` installs nothing;
    `durability` installs the write, then reports it unconfirmed."""
    counter = {"n": 0}
    real_state, real_ref = rc._publish_projection_state, rc._publish_reconciler_checkpoint_ref_transition

    def wrap(real):
        def inner(*a, **k):
            counter["n"] += 1
            if counter["n"] == index:
                if kind == "durability":
                    real(*a, **k)
                    reason = ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED
                else:
                    reason = ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
                raise ls.LifecycleStoreError(reason, "injected")
            return real(*a, **k)

        return inner

    monkeypatch.setattr(rc, "_publish_projection_state", wrap(real_state))
    monkeypatch.setattr(rc, "_publish_reconciler_checkpoint_ref_transition", wrap(real_ref))
    real_delete = rc.CheckpointRef.delete

    def counting(self, *, expected_oid):
        deletes.append(expected_oid)
        return real_delete(self, expected_oid=expected_oid)

    monkeypatch.setattr(rc.CheckpointRef, "delete", counting)


@pytest.mark.parametrize("kind", ["publication", "durability"])
@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_a16_every_write_failure_is_failed_then_resumes(a16, monkeypatch, index, kind):
    """A failed or unconfirmed write is FAILED with no later mutation; the
    next pass resumes from whatever was installed and finishes RECONCILED
    with the attempt count incremented exactly once overall."""
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    deletes: list[str] = []
    _a16_write_fault(monkeypatch, index=index, kind=kind, deletes=deletes)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED
    # No mutation after the failed write: the delete happens only after
    # write 2 (the write-ahead `removing`) was confirmed.
    assert deletes == ([h.A] if index > 2 else [])
    ref_after = _a16_value(h.repo)
    assert (ref_after is None) is (index > 2)
    monkeypatch.undo()
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(h.state_dir))
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    resumed = _a16_entry(h.reconcile())
    # An installed-but-unconfirmed final RECONCILED write is already clean
    # final: the next pass skips it without locking or inspecting (I11).
    expected = (
        rc.ReconciliationEntryOutcome.SKIPPED_TERMINAL
        if (index, kind) == (4, "durability")
        else rc.ReconciliationEntryOutcome.RECONCILED
    )
    assert resumed.outcome is expected
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["checkpoint_ref"]["intent"] == "absent"
    assert payload["reconciliation"]["attempts_total"] == 1
    assert _a16_value(h.repo) is None


def test_a16_resumed_reconciling_removing_entry_does_not_reincrement(a16):
    """A durable RECONCILING + `removing(A)` left by an earlier pass: the next
    pass deletes without a second write-ahead and without incrementing."""
    h = a16
    run_dir = _a16_seed(h, _a16_record("removing", h.A), state=ls.LifecycleState.RECONCILING, attempts=3)
    _a16_set(h.repo, h.A)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert not entry.checkpoint_ref_removing_transition_confirmed_this_pass
    assert entry.checkpoint_ref_removal_attempt == "applied"
    assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 3


def _a16_reconcile_and_sigkill(repo_path, state_dir, point):
    """Module-level (picklable) child: a real admission whose reconciliation
    self-SIGKILLs right after the durable write-ahead `removing` publish
    (before the delete), or right after the real delete returns (before
    the `absent` publish)."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_child
    import codeagent.reconciliation as rc_child

    rc_child._docker_ps_all_id_name_pairs = lambda: ({}, {})

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    if point == "after_removing":
        real = rc_child._publish_reconciler_checkpoint_ref_transition

        def publish(*a, **k):
            out = real(*a, **k)
            if k["target"].intent is cs.CheckpointIntent.REMOVING:
                die()
            return out

        rc_child._publish_reconciler_checkpoint_ref_transition = publish
    else:
        real_delete = rc_child.CheckpointRef.delete

        def delete(self, *, expected_oid):
            real_delete(self, expected_oid=expected_oid)
            die()

        rc_child.CheckpointRef.delete = delete
    ls_child.prepare_lifecycle(repo_path, run_id="a16-reconciler-killed")
    die()  # unreachable


@pytest.mark.parametrize("point, ref_present", [("after_removing", True), ("after_delete", False)])
def test_a16_real_sigkill_inside_the_row_resumes(tmp_path, monkeypatch, point, ref_present):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    h = _Harness(repo, state_dir)
    try:
        A = _a16_commit(repo, "A")
        run_dir = _a16_seed(h, _a16_record("present", A))
        _a16_set(repo, A)
    finally:
        h.close()

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_a16_reconcile_and_sigkill, args=(str(repo), str(state_dir), point))
    proc.start()
    proc.join(timeout=60)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("reconciler child did not self-SIGKILL within the timeout")
    assert proc.exitcode == -signal.SIGKILL

    crashed = _read_projection_dict(run_dir)
    assert crashed["state"] == "RECONCILING"
    assert crashed["reconciliation"]["attempts_total"] == 1
    assert crashed["checkpoint_ref"]["intent"] == "removing"
    assert (_a16_value(repo) is not None) is ref_present

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    h = _Harness(repo, state_dir)
    try:
        entry = _a16_entry(h.reconcile())
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.checkpoint_ref_removal_attempt == ("applied" if ref_present else "not_attempted")
        payload = _read_projection_dict(run_dir)
        assert payload["state"] == "RECONCILED"
        assert payload["reconciliation"]["attempts_total"] == 1
        assert _a16_value(repo) is None
    finally:
        h.close()


def test_a16_unrelated_refs_stay_byte_identical(a16):
    """Never a sweep: another lifecycle's checkpoint ref, branches, and tags
    are untouched; only the exact owned ref disappears."""
    h = a16
    other = "b16" + "0" * 28 + "6"
    _a16_set(h.repo, h.C, ref=f"refs/codeagent/runs/{other}/checkpoint")
    _run("git", "-C", str(h.repo), "tag", "a16-tag", h.B)
    _run("git", "-C", str(h.repo), "update-ref", "refs/heads/a16-branch", h.C)
    _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    before = [line for line in _a16_refs_snapshot(h.repo).splitlines() if _A16_REF not in line]
    assert _a16_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.RECONCILED
    after = _a16_refs_snapshot(h.repo).splitlines()
    assert after == before
    assert _a16_value(h.repo) is None


# ---------------------------------------------------------------------------
# Amendment 16 disclosed correction: the existing absent-record observer maps
# a symbolic ref to REFUSED (ADR 0004 section 5); other observation failures
# stay SUBSTRATE_UNAVAILABLE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dangling", [True, False], ids=["dangling", "resolvable"])
def test_a16_absent_record_with_symbolic_ref_is_refused(a16, dangling):
    h = a16
    run_dir = _seed_run_dir(h, _A16_ID, _initial_projection(h, _A16_ID))
    _a16_symbolic(h.repo, dangling=dangling)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.detail == "the recomputed checkpoint ref is symbolic"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before


def test_a16_absent_record_observation_failure_stays_substrate_unavailable(a16, monkeypatch):
    h = a16
    _seed_run_dir(h, _A16_ID, _initial_projection(h, _A16_ID))

    def fail(self):
        raise cr.CheckpointRefError(cr.CheckpointRefFailure.OBSERVATION_FAILED, "x", outcome=cr.MutationOutcome.UNKNOWN)

    monkeypatch.setattr(rc.CheckpointRef, "observe", fail)
    entry = _a16_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.detail == "the checkpoint ref could not be observed"


def test_a16_unreadable_leaf_parent_is_never_mistaken_for_absence(a16):
    """Joint-review correction (Amendment 16): the leaf exists, but its
    parent directory cannot be searched. `os.path.lexists` would report that
    `EACCES` as absence and let the ref be deleted; the descriptor-relative
    observer reports an inspection failure instead -- zero mutation."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    h = a16
    run_dir = _a16_seed(h, _a16_record("present", h.A))
    _a16_set(h.repo, h.A)
    leaf = _a16_leaf(h)
    leaf.mkdir(parents=True, mode=0o700)
    parent = leaf.parent
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    os.chmod(parent, 0o000)
    try:
        assert not os.path.lexists(leaf)  # the trap: lexists hides EACCES
        entry = _a16_entry(h.reconcile())
    finally:
        os.chmod(parent, 0o700)
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.detail == "the worktree leaf could not be inspected"
    assert entry.checkpoint_ref_removal_attempt == "not_attempted"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_value(h.repo).startswith(h.A)
    os.rmdir(leaf)


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 17: worktree -> checkpoint-ref chaining in one pass.
# The lifecycle is `_A13_ID` so Amendment 13's real-worktree helpers apply.
# ---------------------------------------------------------------------------

_A17_REF = f"refs/codeagent/runs/{_A13_ID}/checkpoint"


@pytest.fixture
def a17(a13, monkeypatch):
    """Amendment 13's fixture plus three branch-free commits and one ordered
    log of every container listing (with the durable projection at that
    moment), `CheckpointRef.observe`, and `CheckpointRef.delete` call.
    `h.listing_queue` holds per-call listing results (a value or an
    exception); once empty, every listing is empty."""
    h = a13
    h.A, h.B, h.C = (_a16_commit(h.repo, m) for m in "ABC")
    h.log = []
    h.listing_queue = []
    run_dir = h.runs_dir() / _A13_ID

    def listing():
        p = _read_projection_dict(run_dir) if (run_dir / ls.LIFECYCLE_JSON_FILENAME).exists() else None
        h.log.append(("listing", p and (p["state"], p["worktree"]["intent"], p["checkpoint_ref"]["intent"])))
        nxt = h.listing_queue.pop(0) if h.listing_queue else _empty_listing()
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    real_observe, real_delete = rc.CheckpointRef.observe, rc.CheckpointRef.delete

    def observe(self):
        h.log.append(("observe", None))
        return real_observe(self)

    def delete(self, *, expected_oid):
        h.log.append(("delete", expected_oid))
        return real_delete(self, expected_oid=expected_oid)

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", listing)
    monkeypatch.setattr(rc.CheckpointRef, "observe", observe)
    monkeypatch.setattr(rc.CheckpointRef, "delete", delete)
    yield h
    for ref in _run("git", "-C", str(h.repo), "for-each-ref", "--format=%(refname)", "refs/codeagent").stdout.split():
        _run("git", "-C", str(h.repo), "update-ref", "-d", "--no-deref", ref, check=False)


def _a17_seed(h, wt_intent, ref_record, *, live=None, state=ls.LifecycleState.ACTIVE, attempts=0, materialize=True):
    head = _run("git", "-C", str(h.repo), "rev-parse", "HEAD").stdout.strip()
    projection = dataclasses.replace(
        _initial_projection(h, _A13_ID),
        state=state,
        worktree=_wl.WorktreeTransition(intent=_wl.WorktreeIntent(wt_intent), expected_head=head),
        checkpoint_ref=ref_record,
        reconciliation=ls.ReconciliationSummary(attempts_total=attempts, recent_failures=()),
    )
    run_dir = _seed_run_dir(h, _A13_ID, projection)
    if materialize:
        _a13_materialize(h)
    if live is not None:
        _a16_set(h.repo, live, ref=_A17_REF)
    return run_dir


def _a17_entry(result):
    (entry,) = [e for e in result.entries if e.lifecycle_id == _A13_ID]
    return entry


def _a17_kinds(h, kind):
    return [x for x in h.log if x[0] == kind]


def _a17_ref(h):
    return _a16_value(h.repo, _A17_REF)


def _a17_assert_fully_reconciled(h, run_dir, *, attempts=1):
    assert not _a13_registered(h)
    assert rc._count_worktree_admin_entries(h.identity.canonical_common_dir, _A13_ID) == 0
    assert not os.path.lexists(_a13_leaf(h))
    assert _a17_ref(h) is None
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILED"
    assert payload["worktree"] == {"intent": "absent", "expected_head": None}
    assert payload["checkpoint_ref"]["intent"] == "absent"
    assert payload["containers"]["baseline"]["intent"] == "absent"
    assert payload["containers"]["verification"]["intent"] == "absent"
    assert payload["reconciliation"]["attempts_total"] == attempts


def _a17_assert_untouched(h, run_dir, before, *, ref_prefix):
    """Zero mutation: projection bytes, the registered worktree, and the ref."""
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a17_ref(h).startswith(ref_prefix)
    assert not _a17_kinds(h, "delete")


def _a17_raw_trace(h):
    return "".join(p.read_text() for p in (h.repo_dir() / rc.MAINTENANCE_DIRNAME).glob("*.jsonl"))


def _a17_no_removal(monkeypatch):
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: pytest.fail("must not remove the worktree"))


# (worktree intent, ref intent, live ref, expected observation category)
_A17_SUCCESS = [
    ("present", "present", "A", "candidate_accepted"),
    ("present", "advancing", "A", "candidate_accepted"),
    ("present", "advancing", "B", "candidate_proposed"),
    ("disposing", "removing", "A", "candidate_accepted"),
    ("creating", "creating", "A", "candidate_proposed"),
    ("present", "present", None, "absent"),
]


@pytest.mark.parametrize("wt_intent, ref_intent, live, category", _A17_SUCCESS)
def test_a17_chained_row_removes_worktree_then_ref(a17, wt_intent, ref_intent, live, category):
    h = a17
    run_dir = _a17_seed(h, wt_intent, _a16_record(ref_intent, h.A, h.B), live=getattr(h, live) if live else None)
    head = _run("git", "-C", str(h.repo), "rev-parse", "HEAD").stdout.strip()
    result = h.reconcile()
    entry = _a17_entry(result)
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
    assert not result.blocked
    _a17_assert_fully_reconciled(h, run_dir)
    assert entry.checkpoint_ref_gate_observation == category
    assert entry.checkpoint_ref_observation_pre == category
    assert entry.checkpoint_ref_container_gate == "confirmed_absent"
    assert entry.checkpoint_ref_removal_attempt == ("applied" if live else "not_attempted")
    assert len(_a17_kinds(h, "listing")) == 2
    event, _ = _a12_entry_event(h, result)
    assert event["worktree"]["initial_persisted_intent"] == wt_intent
    assert event["worktree"]["removal_attempt"] == "exited_zero"
    assert event["worktree"]["absent_transition_confirmed_this_pass"] is True
    assert event["worktree"]["disposing_transition_confirmed_this_pass"] is (wt_intent != "disposing")
    ref = event["checkpoint_ref"]
    assert ref["initial_persisted_intent"] == ref_intent
    assert ref["gate_observation"] == category and ref["observation_pre"] == category
    assert ref["container_gate"] == "confirmed_absent"
    assert ref["absent_transition_confirmed_this_pass"] is True
    assert ref["confirmed_absent"] is True and event["worktree"]["confirmed_absent"] is True
    _a16_assert_no_sha_in_trace(h, h.A, h.B, h.C, head)


def test_a17_d2_registered_directory_missing_disposing_with_ref(a17):
    h = a17
    run_dir = _a17_seed(h, "disposing", _a16_record("present", h.A), live=h.A)
    _shutil.rmtree(_a13_leaf(h))
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
    _a17_assert_fully_reconciled(h, run_dir)


def test_a17_disposing_collapse_with_ref_runs_no_git_removal(a17, monkeypatch):
    h = a17
    run_dir = _a17_seed(h, "disposing", _a16_record("present", h.A), live=h.A, materialize=False)
    _a17_no_removal(monkeypatch)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
    assert entry.worktree_leaf_outcome == "already_absent"
    _a17_assert_fully_reconciled(h, run_dir)


def test_a17_real_sha256_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo256"
    init = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(repo)], capture_output=True, text=True)
    if init.returncode != 0:
        pytest.skip("installed git does not support --object-format=sha256")
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    state_dir = _set_state_dir(monkeypatch, tmp_path, "state-256")
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    h = _Harness(repo, state_dir)
    try:
        assert h.identity.object_format == "sha256"
        A, B = _a16_commit(repo, "A"), _a16_commit(repo, "B")
        assert len(A) == 64
        run_dir = _a17_seed(h, "present", _a16_record("advancing", A, B), live=B)
        entry = _a17_entry(h.reconcile())
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
        assert entry.checkpoint_ref_candidate_role == "proposed"
        _a17_assert_fully_reconciled(h, run_dir)
        _a16_assert_no_sha_in_trace(h, A, B)
    finally:
        h.close()


def test_a17_listing_order_is_load_bearing(a17):
    """Exactly two listings: the first before any projection write, the
    second after `worktree absent` is durable and before the authoritative
    ref observation; nothing lists between that observation and the delete."""
    h = a17
    _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    assert _a17_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.RECONCILED
    first_delete = [x[0] for x in h.log].index("delete")
    assert [x[0] for x in h.log[: first_delete + 1]] == ["listing", "observe", "listing", "observe", "delete"]
    assert h.log[0] == ("listing", ("ACTIVE", "present", "present"))
    assert h.log[2] == ("listing", ("RECONCILING", "absent", "present"))
    assert h.log[first_delete] == ("delete", h.A)
    assert len(_a17_kinds(h, "listing")) == 2


def test_a17_container_name_only_in_second_listing_is_refused_before_any_ref_step(a17):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    present = _listing({f"codeagent-baseline-{_A13_ID}": "d" * 64})
    h.listing_queue[:] = [_empty_listing(), present]
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.detail == "a deterministic container name is present"
    assert entry.checkpoint_ref_container_gate == "present"
    assert entry.checkpoint_ref_observation_pre is None
    assert entry.checkpoint_ref_gate_observation == "candidate_accepted"
    assert len(_a17_kinds(h, "observe")) == 1  # the gate only; nothing after gate 2
    assert not _a17_kinds(h, "delete")
    assert _a17_ref(h).startswith(h.A)
    assert not _a13_registered(h) and not os.path.lexists(_a13_leaf(h))
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "absent"
    assert payload["checkpoint_ref"] == {"intent": "present", "accepted_sha": h.A, "expected_old_sha": None, "proposed_new_sha": None}
    assert payload["reconciliation"]["attempts_total"] == 1
    # While the container exists, the resumed (Amendment 16) row stays refused.
    h.listing_queue[:] = [present]
    again = _a17_entry(h.reconcile())
    assert again.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert _a17_ref(h).startswith(h.A)
    assert _read_projection_dict(run_dir)["reconciliation"]["attempts_total"] == 1


def test_a17_second_listing_failure_is_substrate_unavailable_then_resumes(a17):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    h.listing_queue[:] = [_empty_listing(), rc._DockerListingError("x")]
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.SUBSTRATE_UNAVAILABLE
    assert entry.detail == "container listing failed"
    assert entry.checkpoint_ref_container_gate == "unknown"
    assert len(_a17_kinds(h, "observe")) == 1 and not _a17_kinds(h, "delete")
    assert _a17_ref(h).startswith(h.A)
    assert _read_projection_dict(run_dir)["worktree"]["intent"] == "absent"
    resumed = _a17_entry(h.reconcile())
    assert resumed.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert resumed.checkpoint_ref_container_gate == "confirmed_absent"
    _a17_assert_fully_reconciled(h, run_dir)


@pytest.mark.parametrize(
    "first, outcome",
    [
        (lambda: _listing({f"codeagent-verification-{_A13_ID}": "d" * 64}), "REFUSED"),
        (lambda: rc._DockerListingError("x"), "SUBSTRATE_UNAVAILABLE"),
    ],
    ids=["name-present", "listing-fails"],
)
def test_a17_first_container_gate_stops_with_zero_mutation(a17, monkeypatch, first, outcome):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    h.listing_queue[:] = [first()]
    _a17_no_removal(monkeypatch)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome]
    assert entry.checkpoint_ref_container_gate == "not_attempted"
    assert not _a17_kinds(h, "observe") and len(_a17_kinds(h, "listing")) == 1
    _a17_assert_untouched(h, run_dir, before, ref_prefix=h.A)
    assert _a13_registered(h)


def test_a17_standalone_amendment_13_row_lists_exactly_once(a17):
    h = a17
    run_dir, _ = _a13_seed(h, "present")
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert len(_a17_kinds(h, "listing")) == 1
    assert entry.checkpoint_ref_container_gate == "not_attempted"
    assert entry.checkpoint_ref_gate_observation is None
    assert _read_projection_dict(run_dir)["state"] == "RECONCILED"


def test_a17_standalone_amendment_16_row_lists_exactly_once(a17):
    h = a17
    projection = dataclasses.replace(
        _initial_projection(h, _A13_ID), state=ls.LifecycleState.ACTIVE, checkpoint_ref=_a16_record("present", h.A)
    )
    run_dir = _seed_run_dir(h, _A13_ID, projection)
    _a16_set(h.repo, h.A, ref=_A17_REF)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert len(_a17_kinds(h, "listing")) == 1
    assert entry.checkpoint_ref_container_gate == "confirmed_absent"
    assert entry.checkpoint_ref_gate_observation is None
    assert _read_projection_dict(run_dir)["state"] == "RECONCILED"
    assert _a17_ref(h) is None


def _a17_symbolic(h, *, dangling):
    target = "refs/heads/a17-missing" if dangling else "refs/heads/a17-pin"
    if not dangling:
        _run("git", "-C", str(h.repo), "update-ref", target, "HEAD")
    _run("git", "-C", str(h.repo), "symbolic-ref", _A17_REF, target)


def _a17_failing_observe(reason):
    def fail(self):
        raise cr.CheckpointRefError(reason, "x", outcome=cr.MutationOutcome.UNKNOWN)

    return fail


@pytest.mark.parametrize(
    "setup, outcome, category",
    [
        ("unrelated", "REFUSED", "unexpected"),
        ("dangling_symbolic", "REFUSED", "symbolic"),
        ("resolvable_symbolic", "REFUSED", "symbolic"),
        ("ambiguous", "SUBSTRATE_UNAVAILABLE", "ambiguous"),
        ("failed", "SUBSTRATE_UNAVAILABLE", "unknown"),
    ],
)
def test_a17_ref_gate_stops_before_any_worktree_mutation(a17, monkeypatch, setup, outcome, category):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A))
    if setup == "unrelated":
        _a16_set(h.repo, h.C, ref=_A17_REF)
    elif setup.endswith("symbolic"):
        _a17_symbolic(h, dangling=setup == "dangling_symbolic")
    else:
        _a16_set(h.repo, h.A, ref=_A17_REF)
        reason = cr.CheckpointRefFailure.AMBIGUOUS_OBSERVATION if setup == "ambiguous" else cr.CheckpointRefFailure.OBSERVATION_FAILED
        monkeypatch.setattr(rc.CheckpointRef, "observe", _a17_failing_observe(reason))
    ref_before = _a16_refs_snapshot(h.repo)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    _a17_no_removal(monkeypatch)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], entry.detail
    assert entry.checkpoint_ref_gate_observation == category
    assert entry.checkpoint_ref_observation_pre is None
    assert entry.checkpoint_ref_container_gate == "not_attempted"
    assert (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes() == before
    assert _a16_refs_snapshot(h.repo) == ref_before
    assert _a13_registered(h) and len(_a17_kinds(h, "listing")) == 1


def test_a17_never_registered_creating_worktree_with_ref_is_refused(a17, monkeypatch):
    """Amendment 12's shape plus a ref is excluded: REFUSED, zero mutation,
    and the empty leaf is left in place."""
    h = a17
    run_dir = _a17_seed(h, "creating", _a16_record("present", h.A), live=h.A, materialize=False)
    leaf = _a13_leaf(h)
    leaf.mkdir(parents=True, mode=0o700)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    _a17_no_removal(monkeypatch)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.detail == "a never-registered creating worktree with a checkpoint ref is not reconciled"
    assert not _a17_kinds(h, "observe")
    _a17_assert_untouched(h, run_dir, before, ref_prefix=h.A)
    assert leaf.is_dir()


def test_a17_failure_bearing_projection_is_not_eligible(a17, monkeypatch):
    h = a17
    projection = dataclasses.replace(
        _initial_projection(h, _A13_ID),
        state=ls.LifecycleState.ACTIVE,
        worktree=_wl.WorktreeTransition(intent=_wl.WorktreeIntent.PRESENT, expected_head=h.A),
        checkpoint_ref=_a16_record("present", h.A),
        failure=ls.FailureDetail(phase="p", detail="d"),
    )
    run_dir = _seed_run_dir(h, _A13_ID, projection)
    _a13_materialize(h)
    _a16_set(h.repo, h.A, ref=_A17_REF)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    _a17_no_removal(monkeypatch)
    result = h.reconcile()
    assert _a17_entry(result).outcome is rc.ReconciliationEntryOutcome.REFUSED and result.blocked
    assert not _a17_kinds(h, "listing")
    _a17_assert_untouched(h, run_dir, before, ref_prefix=h.A)


def test_a17_held_lifecycle_lock_is_skipped_active(a17, monkeypatch):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    _a17_no_removal(monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    ready_evt, release_evt = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_a16_hold_lifecycle_lock, args=(str(run_dir), ready_evt, release_evt))
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        assert _a17_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.SKIPPED_ACTIVE
        _a17_assert_untouched(h, run_dir, before, ref_prefix=h.A)
        assert _a13_registered(h)
    finally:
        release_evt.set()
        proc.join(timeout=10)


def _a17_partial(h):
    def partial(root, target):
        _shutil.rmtree(_a13_git_dir(h) / "worktrees" / _A13_ID)
        return "exited_nonzero"

    return partial


@pytest.mark.parametrize(
    "fault, outcome",
    [
        ("launch_failed", "SUBSTRATE_UNAVAILABLE"),
        ("cleanup_unconfirmed", "SUBSTRATE_UNAVAILABLE"),
        ("partial_removal", "FAILED"),
    ],
)
def test_a17_phase_w_failure_stops_every_later_step(a17, monkeypatch, fault, outcome):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    monkeypatch.setattr(
        rc, "_attempt_worktree_remove", _a17_partial(h) if fault == "partial_removal" else (lambda *a: fault)
    )
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome], entry.detail
    assert len(_a17_kinds(h, "listing")) == 1  # no container gate 2
    assert len(_a17_kinds(h, "observe")) == 1  # the gate only
    assert not _a17_kinds(h, "delete")
    assert entry.checkpoint_ref_container_gate == "not_attempted"
    assert _a17_ref(h).startswith(h.A)
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "disposing"
    assert payload["checkpoint_ref"]["intent"] == "present"
    assert payload["reconciliation"]["attempts_total"] == 1


def test_a17_locked_registration_is_refused_in_gate_a(a17):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    _run("git", "-C", str(h.repo), "worktree", "lock", str(_a13_leaf(h)))
    before = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.REFUSED
    assert entry.detail == "the worktree registration is locked"
    assert not _a17_kinds(h, "observe") and len(_a17_kinds(h, "listing")) == 1
    _a17_assert_untouched(h, run_dir, before, ref_prefix=h.A)


@pytest.mark.parametrize("race", ["moved", "deleted", "symbolic", "observation_fails"])
def test_a17_ref_change_during_phase_w_is_caught_by_the_fresh_observation(a17, monkeypatch, race):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    real_remove = rc._attempt_worktree_remove

    def remove_then_race(root, target):
        out = real_remove(root, target)
        if race == "moved":
            _a16_set(h.repo, h.C, ref=_A17_REF)
        elif race in ("deleted", "symbolic"):
            _run("git", "-C", str(h.repo), "update-ref", "-d", _A17_REF)
            if race == "symbolic":
                _a17_symbolic(h, dangling=True)
        else:
            monkeypatch.setattr(rc.CheckpointRef, "observe", _a17_failing_observe(cr.CheckpointRefFailure.OBSERVATION_FAILED))
        return out

    monkeypatch.setattr(rc, "_attempt_worktree_remove", remove_then_race)
    entry = _a17_entry(h.reconcile())
    assert entry.checkpoint_ref_gate_observation == "candidate_accepted"
    assert not _a13_registered(h) and not os.path.lexists(_a13_leaf(h))
    payload = _read_projection_dict(run_dir)
    if race == "deleted":
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.checkpoint_ref_observation_pre == "absent"
        assert entry.checkpoint_ref_removal_attempt == "not_attempted"
        _a17_assert_fully_reconciled(h, run_dir)
        return
    expected = {"moved": ("REFUSED", "unexpected"), "symbolic": ("REFUSED", "symbolic"), "observation_fails": ("SUBSTRATE_UNAVAILABLE", "unknown")}
    outcome, category = expected[race]
    assert entry.outcome is rc.ReconciliationEntryOutcome[outcome]
    assert entry.checkpoint_ref_observation_pre == category
    assert not _a17_kinds(h, "delete")
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "absent"
    assert payload["checkpoint_ref"]["intent"] == "present"
    if race == "moved":
        assert _a17_ref(h).startswith(h.C)
        assert _a17_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.REFUSED  # Amendment 16 row
        assert _a17_ref(h).startswith(h.C)
        assert not _a17_kinds(h, "delete")


@pytest.mark.parametrize("reason, mutation, outcome, category", _A16_DELETE_OUTCOMES)
def test_a17_phase_r_delete_failures_keep_the_write_ahead_record(a17, monkeypatch, reason, mutation, outcome, category):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)

    def fake_delete(self, *, expected_oid):
        h.log.append(("delete", expected_oid))
        raise cr.CheckpointRefError(reason, "injected", outcome=mutation)

    monkeypatch.setattr(rc.CheckpointRef, "delete", fake_delete)
    entry = _a17_entry(h.reconcile())
    assert entry.outcome.value == outcome.lower()
    assert entry.checkpoint_ref_removal_attempt == category
    payload = _read_projection_dict(run_dir)
    assert payload["state"] == "RECONCILING" and payload["worktree"]["intent"] == "absent"
    assert payload["checkpoint_ref"]["intent"] == "removing"
    assert payload["reconciliation"]["attempts_total"] == 1
    assert _a17_ref(h).startswith(h.A)


# The chained pass's six projection writes, in order: 1 RECONCILING,
# 2 worktree disposing, 3 worktree absent, 4 ref removing, 5 ref absent,
# 6 RECONCILED. (installed state, worktree, ref) after each failure.
_A17_WRITES = {
    (1, "publication"): ("ACTIVE", "present", "present"),
    (1, "durability"): ("RECONCILING", "present", "present"),
    (2, "publication"): ("RECONCILING", "present", "present"),
    (2, "durability"): ("RECONCILING", "disposing", "present"),
    (3, "publication"): ("RECONCILING", "disposing", "present"),
    (3, "durability"): ("RECONCILING", "absent", "present"),
    (4, "publication"): ("RECONCILING", "absent", "present"),
    (4, "durability"): ("RECONCILING", "absent", "removing"),
    (5, "publication"): ("RECONCILING", "absent", "removing"),
    (5, "durability"): ("RECONCILING", "absent", "absent"),
    (6, "publication"): ("RECONCILING", "absent", "absent"),
    (6, "durability"): ("RECONCILED", "absent", "absent"),
}


@pytest.mark.parametrize("index, kind", sorted(_A17_WRITES))
def test_a17_every_write_failure_is_failed_then_resumes(a17, monkeypatch, index, kind):
    h = a17
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    fault = {"n": 0, "armed": True, "log_len": None}

    def wrap(real):
        def inner(*a, **k):
            fault["n"] += 1
            if fault["armed"] and fault["n"] == index:
                fault["log_len"] = len(h.log)
                if kind == "durability":
                    real(*a, **k)
                    reason = ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED
                else:
                    reason = ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
                raise ls.LifecycleStoreError(reason, "injected")
            return real(*a, **k)

        return inner

    for name in (
        "_publish_projection_state",
        "_publish_reconciler_worktree_transition",
        "_publish_reconciler_checkpoint_ref_transition",
    ):
        monkeypatch.setattr(rc, name, wrap(getattr(rc, name)))
    removals = []
    real_remove = rc._attempt_worktree_remove
    monkeypatch.setattr(rc, "_attempt_worktree_remove", lambda *a: removals.append(1) or real_remove(*a))

    entry = _a17_entry(h.reconcile())
    assert entry.outcome is rc.ReconciliationEntryOutcome.FAILED, entry.detail
    assert h.log[fault["log_len"]:] == []  # nothing listed, observed, or deleted after the failed write
    assert len(removals) == (1 if index > 2 else 0)
    assert len(_a17_kinds(h, "delete")) == (1 if index > 4 else 0)
    payload = _read_projection_dict(run_dir)
    assert (payload["state"], payload["worktree"]["intent"], payload["checkpoint_ref"]["intent"]) == _A17_WRITES[(index, kind)]

    fault["armed"] = False
    resumed = _a17_entry(h.reconcile())
    expected = (
        rc.ReconciliationEntryOutcome.SKIPPED_TERMINAL
        if (index, kind) == (6, "durability")
        else rc.ReconciliationEntryOutcome.RECONCILED
    )
    assert resumed.outcome is expected, resumed.detail
    _a17_assert_fully_reconciled(h, run_dir)


def _a17_reconcile_and_sigkill(repo_path, state_dir, point):
    """Module-level (picklable) child: a real admission whose chained
    reconciliation self-SIGKILLs right after one durable write."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_child
    import codeagent.reconciliation as rc_child

    rc_child._docker_ps_all_id_name_pairs = lambda: ({}, {})
    real_wt = rc_child._publish_reconciler_worktree_transition
    real_ref = rc_child._publish_reconciler_checkpoint_ref_transition

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    def wt(*a, **k):
        out = real_wt(*a, **k)
        disposing = "target" in k and k["target"].intent is _wl.WorktreeIntent.DISPOSING
        if (point == "after_disposing") is disposing and point in ("after_disposing", "after_worktree_absent"):
            die()
        return out

    def ref(*a, **k):
        out = real_ref(*a, **k)
        if point == "after_ref_removing" and k["target"].intent is cs.CheckpointIntent.REMOVING:
            die()
        return out

    rc_child._publish_reconciler_worktree_transition = wt
    rc_child._publish_reconciler_checkpoint_ref_transition = ref
    ls_child.prepare_lifecycle(repo_path, run_id="a17-reconciler-killed")
    die()  # unreachable


@pytest.mark.parametrize(
    "point, crashed_wt, crashed_ref, registered",
    [
        ("after_disposing", "disposing", "present", True),
        ("after_worktree_absent", "absent", "present", False),
        ("after_ref_removing", "absent", "removing", False),
    ],
)
def test_a17_real_sigkill_inside_the_chained_row_resumes(tmp_path, monkeypatch, point, crashed_wt, crashed_ref, registered):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    h = _Harness(repo, state_dir)
    try:
        A = _a16_commit(repo, "A")
        run_dir = _a17_seed(h, "present", _a16_record("present", A), live=A)
    finally:
        h.close()
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_a17_reconcile_and_sigkill, args=(str(repo), str(state_dir), point))
    proc.start()
    proc.join(timeout=60)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("reconciler child did not self-SIGKILL within the timeout")
    assert proc.exitcode == -signal.SIGKILL
    crashed = _read_projection_dict(run_dir)
    assert crashed["state"] == "RECONCILING"
    assert crashed["reconciliation"]["attempts_total"] == 1
    assert (crashed["worktree"]["intent"], crashed["checkpoint_ref"]["intent"]) == (crashed_wt, crashed_ref)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: _empty_listing())
    h = _Harness(repo, state_dir)
    try:
        assert _a13_registered(h) is registered
        assert _a16_value(repo, _A17_REF).startswith(A)
        entry = _a17_entry(h.reconcile())
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED, entry.detail
        _a17_assert_fully_reconciled(h, run_dir)
    finally:
        if _a13_registered(h):
            _run("git", "-C", str(repo), "worktree", "remove", "--force", str(_a13_leaf(h)), check=False)
        h.close()


def test_a17_unrelated_refs_branches_and_tags_stay_byte_identical(a17):
    h = a17
    other = "f" * 32
    _a16_set(h.repo, h.C, ref=f"refs/codeagent/runs/{other}/checkpoint")
    _run("git", "-C", str(h.repo), "tag", "a17-tag", h.B)
    _run("git", "-C", str(h.repo), "update-ref", "refs/heads/a17-branch", h.C)
    run_dir = _a17_seed(h, "present", _a16_record("present", h.A), live=h.A)
    before = [line for line in _a16_refs_snapshot(h.repo).splitlines() if _A17_REF not in line]
    assert _a17_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.RECONCILED
    assert _a16_refs_snapshot(h.repo).splitlines() == before
    _a17_assert_fully_reconciled(h, run_dir)


def test_a17_trace_is_categorical_only(a17):
    """No SHA, container name or id, path, or exception text reaches the raw
    trace, including on a gate-2 refusal that observed a container."""
    h = a17
    _a17_seed(h, "present", _a16_record("advancing", h.A, h.B), live=h.B)
    cid = "d" * 64
    h.listing_queue[:] = [_empty_listing(), _listing({f"codeagent-baseline-{_A13_ID}": cid})]
    assert _a17_entry(h.reconcile()).outcome is rc.ReconciliationEntryOutcome.REFUSED
    raw = _a17_raw_trace(h)
    for secret in (h.A, h.B, cid, f"codeagent-baseline-{_A13_ID}", str(_a13_leaf(h)), str(h.state_dir), str(h.repo)):
        assert secret not in raw
