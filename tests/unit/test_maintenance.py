"""Tests for `codeagent.maintenance` (ADR 0004 sections 10-12, Amendment 18):
the presence table (P1-P7), normal and forced abandonment rows, explicit
reconciliation, the dry run, and single-attempt release stages. Real
filesystem and Git; Docker listings are faked (real Docker is exercised by
`tests/integration/test_reconcile_cli.py`)."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from codeagent import _lifecycle_fs as lf
from codeagent import abandonment as ab
from codeagent import lifecycle_store as ls
from codeagent import maintenance as mt
from codeagent import reconciliation as rc
from codeagent import repo_identity as ri
from codeagent import state_locks as sl
from codeagent import state_root as sr

REASON = "/Users/SENTINEL-REASON-9b1c/secret"


def _git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i")
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    identity, _ = ri.discover_repository_identity_and_context(str(repo))

    class Env:
        pass

    e = Env()
    e.repo, e.state, e.repo_key = repo, state, identity.repo_key
    e.identity = identity
    return e


def _init_namespace(e):
    """state-root.json, the repository lock file, and repo.json -- exactly
    what a first real run creates before its own reconciliation."""
    _, context = ri.discover_repository_identity_and_context(str(e.repo))
    fd, canonical = sr.open_or_create_canonical_root(lf.resolve_state_root_path())
    state_root = sr.init_state_root(fd, canonical)
    lock = sl.acquire_repository_lock(state_root, e.repo_key)
    ri.load_or_create_repo_json(state_root, e.identity, lock)
    lock.release()
    e.state_root_id = state_root.state_root_id
    state_root.close()


def _runs(e) -> Path:
    d = e.state / "repos" / e.repo_key / "runs"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _projection(e, lifecycle_id, **changes):
    projection = ls.build_initial_preparing_projection(
        lifecycle_id=lifecycle_id,
        state_root_id=e.state_root_id,
        repo_key=e.repo_key,
        run_id="run-1",
        source_repo_path=str(e.repo),
    )
    return dataclasses.replace(projection, **changes)


def _seed(e, lifecycle_id, *, projection=True, lock=True, **changes) -> Path:
    run_dir = _runs(e) / lifecycle_id
    run_dir.mkdir(mode=0o700)
    if projection:
        data = lf.canonical_json_dumps(ls.projection_to_dict(_projection(e, lifecycle_id, **changes)))
        (run_dir / "lifecycle.json").write_bytes(data)
        (run_dir / "lifecycle.json").chmod(0o600)
    if lock:
        (run_dir / "lifecycle.lock").write_bytes(b"")
        (run_dir / "lifecycle.lock").chmod(0o600)
    return run_dir


def _tree(root: Path):
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, st.st_ino, st.st_mtime_ns)
    return out


def _traces(e):
    d = e.state / "repos" / e.repo_key / "maintenance"
    return sorted(d.iterdir()) if d.exists() else []


def _abandon(e, lifecycle_id, *, forced=False):
    return mt.run_abandon(
        str(e.repo), lifecycle_id, acknowledge_unresolved=forced, reason=REASON if forced else None
    )


def _present_container(lifecycle_id):
    return lambda: ({f"codeagent-baseline-{lifecycle_id}": "1" * 64}, {"1" * 64: f"codeagent-baseline-{lifecycle_id}"})


LID = "a" * 32


# ---------------------------------------------------------------------------
# Presence table P1-P7 (C3, U5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_p1_no_state_root_is_clean_and_creates_nothing(env, dry_run):
    report = mt.run_reconcile(str(env.repo), dry_run=dry_run)
    assert report.result is mt.ReconcileResult.CLEAN and report.no_recorded_state
    assert report.maintenance_id is None
    assert not env.state.exists()
    refused = _abandon(env, LID)
    assert refused.outcome is mt.AbandonOutcome.REFUSED and refused.refusal == "no_recorded_state"
    assert not env.state.exists()


def test_p1_empty_state_root_is_clean_and_stays_empty(env):
    env.state.mkdir(mode=0o700)
    assert mt.run_reconcile(str(env.repo), dry_run=False).no_recorded_state
    assert os.listdir(env.state) == []


def test_p2_content_without_identity_blocks(env):
    env.state.mkdir(mode=0o700)
    (env.state / "repos").mkdir(mode=0o700)
    report = mt.run_reconcile(str(env.repo), dry_run=True)
    assert report.result is mt.ReconcileResult.BLOCKED and report.blocked_reason == "state_root_unavailable"


def test_p3_no_lock_and_no_namespace_state_is_clean(env):
    fd, canonical = sr.open_or_create_canonical_root(lf.resolve_state_root_path())
    sr.init_state_root(fd, canonical).close()
    before = _tree(env.state)
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.CLEAN and report.no_recorded_state
    assert _tree(env.state) == before


@pytest.mark.parametrize("where", ["repo_json", "runs", "worktrees"])
def test_p4_missing_lock_beside_recorded_state_blocks_and_writes_nothing(env, where):
    """C3 / M23."""
    _init_namespace(env)
    if where == "runs":
        _seed(env, LID)
    elif where == "worktrees":
        (env.state / "worktrees" / env.repo_key / LID).mkdir(parents=True, mode=0o700)
    (env.state / "repo-locks" / f"{env.repo_key}.lock").unlink()
    before = _tree(env.state)
    for dry_run in (False, True):
        report = mt.run_reconcile(str(env.repo), dry_run=dry_run)
        assert report.result is mt.ReconcileResult.BLOCKED
        assert report.blocked_reason == "repository_lock_missing_with_state"
    refused = _abandon(env, LID)
    assert refused.outcome is mt.AbandonOutcome.REFUSED
    assert refused.refusal == "repository_lock_missing_with_state"
    assert _tree(env.state) == before


def test_p5_lock_without_namespace_is_clean(env):
    fd, canonical = sr.open_or_create_canonical_root(lf.resolve_state_root_path())
    state_root = sr.init_state_root(fd, canonical)
    sl.acquire_repository_lock(state_root, env.repo_key).release()
    state_root.close()
    before = _tree(env.state)
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.CLEAN and report.no_recorded_state
    assert _abandon(env, LID).refusal == "no_recorded_state"
    assert _tree(env.state) == before


def test_p5_busy_repository_lock_blocks(env):
    _init_namespace(env)
    fd = os.open(env.state / "repo-locks" / f"{env.repo_key}.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mt.run_reconcile(str(env.repo), dry_run=True).blocked_reason == "repository_active"
        assert mt.run_reconcile(str(env.repo), dry_run=False).blocked_reason == "repository_active"
        assert _abandon(env, LID).refusal == "repository_active"
    finally:
        os.close(fd)


def test_p6_missing_repo_json_beside_runs_blocks(env):
    _init_namespace(env)
    _seed(env, LID)
    (env.state / "repos" / env.repo_key / "repo.json").unlink()
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.BLOCKED and not report.no_recorded_state
    assert report.blocked_reason == "namespace_refused"


def test_p6_identity_mismatch_names_fields_only(env):
    _init_namespace(env)
    path = env.state / "repos" / env.repo_key / "repo.json"
    payload = json.loads(path.read_text())
    payload["st_ino"] += 1
    path.write_bytes(lf.canonical_json_dumps(payload))
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.blocked_reason == "namespace_refused" and report.mismatched_fields == frozenset({"st_ino"})
    refused = _abandon(env, LID)
    assert refused.refusal == "namespace_refused" and refused.mismatched_fields == frozenset({"st_ino"})


def test_invalid_repository_raises_without_path(env, tmp_path):
    not_repo = tmp_path / "not-a-repo"
    not_repo.mkdir()
    with pytest.raises(mt.InvalidRepositoryError) as excinfo:
        mt.run_reconcile(str(not_repo), dry_run=False)
    assert str(not_repo) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Explicit reconcile and dry run
# ---------------------------------------------------------------------------


def test_explicit_reconcile_writes_an_explicit_trace(env):
    _init_namespace(env)
    _seed(env, LID)
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.CLEAN and report.maintenance_id is not None
    (trace,) = _traces(env)
    assert trace.name == f"{report.maintenance_id}.jsonl"
    assert {json.loads(l)["trigger"] for l in trace.read_text().splitlines()} == {"explicit"}


def test_dry_run_has_no_maintenance_identity_and_writes_nothing(env):
    """C4 / M1."""
    _init_namespace(env)
    _seed(env, LID)
    before = _tree(env.state)
    report = mt.run_reconcile(str(env.repo), dry_run=True)
    assert report.maintenance_id is None
    assert report.result is mt.ReconcileResult.BLOCKED  # PENDING blocks
    assert report.entries[0].outcome is rc.PlanEntryOutcome.PENDING
    assert _tree(env.state) == before


@pytest.mark.parametrize(
    "markers,expected",
    [
        ([], mt.ReconcileResult.CLEAN),
        (["abandoned"], mt.ReconcileResult.CLEAN),
        (["unresolved"], mt.ReconcileResult.UNRESOLVED_ACKNOWLEDGED),
        (["unresolved", "blocker"], mt.ReconcileResult.BLOCKED),
        (["abandoned", "blocker"], mt.ReconcileResult.BLOCKED),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_result_precedence(env, markers, expected, dry_run):
    """M5 / M16: BLOCKED > UNRESOLVED_ACKNOWLEDGED > CLEAN."""
    _init_namespace(env)
    for i, kind in enumerate(markers):
        lid = f"{i:x}" * 32
        if kind == "blocker":
            _seed(env, lid, projection=False)
            continue
        run_dir = _seed(env, lid, projection=False)
        disposition = (
            ab.AbandonmentDisposition.ABANDONED_UNRESOLVED if kind == "unresolved" else ab.AbandonmentDisposition.ABANDONED
        )
        marker = ab.AbandonmentMarker(
            lifecycle_id=lid,
            repo_key=env.repo_key,
            state_root_id=env.state_root_id,
            disposition=disposition,
            maintenance_id="e" * 32,
            timestamp="2026-10-04T00:00:00Z",
            reason=REASON if kind == "unresolved" else None,
            remaining={**{k: "absent" for k in ab.RESOURCE_FIELDS}, "checkpoint_ref": "present"}
            if kind == "unresolved"
            else None,
        )
        (run_dir / ab.MARKER_FILENAME).write_bytes(ab.marker_to_bytes(marker))
        (run_dir / ab.MARKER_FILENAME).chmod(0o600)
    report = mt.run_reconcile(str(env.repo), dry_run=dry_run)
    assert report.result is expected
    assert REASON not in repr(report)


def test_reconcile_release_failure_blocks_and_names_the_stage(env, monkeypatch):
    _init_namespace(env)
    real = sr.StateRoot.close

    def close(self):
        real(self)
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")

    monkeypatch.setattr(sr.StateRoot, "close", close)
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.BLOCKED and report.unconfirmed_stages == ("state_root",)


def test_reconcile_unexpected_exception_still_releases_everything(env, monkeypatch):
    _init_namespace(env)
    monkeypatch.setattr(mt, "reconcile_repository", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        mt.run_reconcile(str(env.repo), dry_run=False)
    # The repository lock was released: a fresh acquisition succeeds.
    assert mt.run_reconcile(str(env.repo), dry_run=True).blocked_reason is None


# ---------------------------------------------------------------------------
# Normal abandonment rows (section 5.1)
# ---------------------------------------------------------------------------


def _assert_recorded(env, run_dir, report, disposition):
    # The loader never owns the caller's directory descriptor: close it here.
    run_dir_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        marker = ab.load_abandonment_marker(
            run_dir_fd,
            names=os.listdir(run_dir),
            expected_lifecycle_id=run_dir.name,
            expected_repo_key=env.repo_key,
            expected_state_root_id=env.state_root_id,
        )
    finally:
        os.close(run_dir_fd)
    assert marker.disposition is disposition
    assert marker.maintenance_id == report.maintenance_id
    (trace,) = [t for t in _traces(env) if t.name == f"{report.maintenance_id}.jsonl"]
    events = [json.loads(l) for l in trace.read_text().splitlines()]
    assert [e["event_type"] for e in events] == ["AbandonmentRecorded"]
    return marker, events[0]


def test_row1_unknown_runs(env):
    _init_namespace(env)
    assert _abandon(env, LID).refusal == "unknown_lifecycle"
    (_runs(env) / "not-a-lifecycle").mkdir()
    assert _abandon(env, LID).refusal == "runs_refused"


def test_row2_unknown_lifecycle(env):
    _init_namespace(env)
    _seed(env, "b" * 32)
    before = _tree(env.state)
    assert _abandon(env, LID).refusal == "unknown_lifecycle"
    assert _tree(env.state) == before


def test_row3_symlinked_run_directory_refused(env, tmp_path):
    _init_namespace(env)
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    (_runs(env) / LID).symlink_to(tmp_path / "elsewhere")
    assert _abandon(env, LID).outcome is mt.AbandonOutcome.REFUSED
    assert os.listdir(tmp_path / "elsewhere") == []


@pytest.mark.parametrize("valid", [True, False])
def test_row4_existing_final_marker_refused_and_untouched(env, valid):
    _init_namespace(env)
    run_dir = _seed(env, LID, projection=False)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    if not valid:
        (run_dir / ab.MARKER_FILENAME).chmod(0o644)
    before = _tree(env.state)
    again = _abandon(env, LID, forced=True)
    assert again.outcome is mt.AbandonOutcome.REFUSED
    assert again.refusal == ("already_abandoned" if valid else "marker_invalid")
    assert _tree(env.state) == before


def test_row5_unsafe_temp_refused(env, tmp_path):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    (run_dir / (".abandonment.json.tmp-" + "0" * 16)).symlink_to(tmp_path)
    assert _abandon(env, LID).refusal == "marker_temp_invalid"


def _temps(run_dir, n):
    for i in range(n):
        p = run_dir / f".abandonment.json.tmp-{i:016x}"
        p.write_bytes(b"stale")
        p.chmod(0o600)


def test_row6_sixteen_stale_temps_refuse_a_new_attempt(env):
    """M20."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    _temps(run_dir, 16)
    before = _tree(env.state)
    report = _abandon(env, LID)
    assert report.refusal == "too_many_stale_abandonment_temps" and report.abandonment_temp_leftovers == 16
    assert _tree(env.state) == before


def test_row7_fifteen_stale_temps_do_not_refuse_and_stay_untouched(env):
    """C1 / M18 / M19."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    _temps(run_dir, 15)
    stale = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.name.startswith(".abandonment")}
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED and report.abandonment_temp_leftovers == 15
    after = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.name.startswith(".abandonment")}
    assert after == stale
    _, event = _assert_recorded(env, run_dir, report, ab.AbandonmentDisposition.ABANDONED)
    assert event["abandonment_temp_leftovers"] == 15


def test_row8_busy_lifecycle_lock_refused(env):
    """M8."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    fd = os.open(run_dir / "lifecycle.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = _abandon(env, LID)
    finally:
        os.close(fd)
    assert report.refusal == "lifecycle_active"
    assert not (run_dir / ab.MARKER_FILENAME).exists() and _traces(env) == []


def test_row8_missing_lifecycle_lock_is_created_for_real_abandonment(env):
    """U10."""
    _init_namespace(env)
    run_dir = _seed(env, LID, projection=False, lock=False)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert (run_dir / "lifecycle.lock").exists()


@pytest.mark.parametrize("state", [ls.LifecycleState.COMPLETE, ls.LifecycleState.RECONCILED])
def test_row9_clean_final_refused(env, state):
    _init_namespace(env)
    _seed(env, LID, state=state)
    assert _abandon(env, LID).refusal == "already_clean_final"


def test_row10_resources_remain_refused_with_summary(env, monkeypatch):
    """M2."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    projection_bytes = (run_dir / "lifecycle.json").read_bytes()
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _present_container(LID))
    report = _abandon(env, LID)
    assert report.refusal == "resources_remain"
    assert report.remaining.baseline_container == "present"
    assert not (run_dir / ab.MARKER_FILENAME).exists() and _traces(env) == []
    assert (run_dir / "lifecycle.json").read_bytes() == projection_bytes


def test_row10_inspection_failure_refused(env, monkeypatch):
    """M3."""
    _init_namespace(env)
    _seed(env, LID)

    def down():
        raise rc._DockerListingError("down")

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", down)
    report = _abandon(env, LID)
    assert report.refusal == "inspection_failed" and report.remaining.baseline_container == "unknown"
    assert _traces(env) == []


def test_row11_trace_creation_failure_records_nothing(env, monkeypatch):
    _init_namespace(env)
    run_dir = _seed(env, LID)

    def fail(*a, **k):
        raise rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "x")

    monkeypatch.setattr(mt, "open_maintenance_trace", fail)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.REFUSED and report.refusal == "maintenance_trace_unavailable"
    assert report.maintenance_id is None
    assert not (run_dir / ab.MARKER_FILENAME).exists()


def test_row12_link_failure_is_not_recorded_and_leaves_an_empty_trace(env, monkeypatch):
    """C5 / M26 / M27: no disposition, no marker, an empty trace remains,
    and a retry succeeds with a second trace."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    real_link = lf.os.link
    monkeypatch.setattr(lf.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.NOT_RECORDED
    assert report.marker_publication is None and report.maintenance_id is not None
    assert not (run_dir / ab.MARKER_FILENAME).exists()
    (trace,) = _traces(env)
    assert trace.name == f"{report.maintenance_id}.jsonl" and trace.read_bytes() == b""
    assert not [p for p in run_dir.iterdir() if p.name.startswith(".abandonment")]

    monkeypatch.setattr(lf.os, "link", real_link)
    retry = _abandon(env, LID)
    assert retry.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert retry.maintenance_id != report.maintenance_id
    assert len(_traces(env)) == 2


def test_row12_temp_cleanup_failure_is_reported(env, monkeypatch):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    monkeypatch.setattr(lf.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    monkeypatch.setattr(lf.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.NOT_RECORDED
    assert "marker_temp_cleanup" in report.unconfirmed_stages
    assert len([p for p in run_dir.iterdir() if p.name.startswith(".abandonment")]) == 1


def test_row13_normal_abandonment_records_and_mutates_nothing_else(env):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    projection_bytes = (run_dir / "lifecycle.json").read_bytes()
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert report.marker_publication == "confirmed" and report.unconfirmed_stages == ()
    marker, event = _assert_recorded(env, run_dir, report, ab.AbandonmentDisposition.ABANDONED)
    assert marker.reason is None and marker.remaining is None
    assert event["reason_recorded"] is False and event["trigger"] == "explicit"
    assert event["projection_status"] == "valid" and event["run_id"] == "run-1"
    assert (run_dir / "lifecycle.json").read_bytes() == projection_bytes
    assert sorted(os.listdir(run_dir)) == ["abandonment.json", "lifecycle.json", "lifecycle.lock"]


def test_row13_unreadable_projection_is_not_a_refusal(env):
    _init_namespace(env)
    run_dir = _seed(env, LID, projection=False)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    _, event = _assert_recorded(env, run_dir, report, ab.AbandonmentDisposition.ABANDONED)
    assert event["projection_status"] == "invalid" and event["run_id"] is None


def _fail_after(monkeypatch, owner, name, exc):
    real = getattr(owner, name)

    def wrapper(*a, **k):
        result = real(*a, **k)
        raise exc
        return result  # pragma: no cover

    monkeypatch.setattr(owner, name, wrapper)


@pytest.mark.parametrize(
    "stage",
    [
        "marker_directory_fsync",
        "marker_temp_unlink",
        "maintenance_event",
        "maintenance_trace",
        "lifecycle_lock",
        "repository_lock",
        "state_root",
    ],
)
def test_row14_later_unconfirmed_stage_preserves_the_disposition(env, monkeypatch, stage):
    """U6: the marker is installed; the stage is reported; exit becomes 4."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    fs_err = lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")
    if stage == "marker_directory_fsync":
        real = lf.fsync_fd
        run_ino = os.stat(run_dir).st_ino

        def fsync_fd(fd):
            if os.fstat(fd).st_ino == run_ino:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "x")
            return real(fd)

        monkeypatch.setattr(lf, "fsync_fd", fsync_fd)
    elif stage == "marker_temp_unlink":
        monkeypatch.setattr(lf.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    elif stage == "maintenance_event":
        monkeypatch.setattr(
            rc._MaintenanceTraceWriter,
            "abandonment_recorded",
            lambda self, **k: (_ for _ in ()).throw(rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "x")),
        )
    elif stage == "maintenance_trace":
        _fail_after(monkeypatch, rc._MaintenanceTraceWriter, "close", rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "x"))
    elif stage in ("lifecycle_lock", "repository_lock"):
        kind = sl.LockKind.LIFECYCLE if stage == "lifecycle_lock" else sl.LockKind.REPOSITORY
        real_release = sl.LockHandle.release

        def release(self):
            real_release(self)
            if self.scope.kind is kind:
                raise sl.LockError(sl.LockFailure.RELEASE_UNCONFIRMED, "x")

        monkeypatch.setattr(sl.LockHandle, "release", release)
    else:
        _fail_after(monkeypatch, sr.StateRoot, "close", fs_err)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert stage in report.unconfirmed_stages
    assert (run_dir / ab.MARKER_FILENAME).exists()
    # Each stage was attempted exactly once: a later reconcile still works.
    monkeypatch.undo()
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    assert mt.run_reconcile(str(env.repo), dry_run=False).result is mt.ReconcileResult.CLEAN


def test_row14_descriptor_close_failures_are_named(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    real = mt.close_confirmed

    def close_confirmed(fds):
        real(fds)
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")

    monkeypatch.setattr(mt, "close_confirmed", close_confirmed)
    # Presence probing does not use mt.close_confirmed once the lock exists.
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert {"repository_directory", "run_directory", "runs_directory"} <= set(report.unconfirmed_stages)


def test_every_release_stage_is_attempted_exactly_once(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    counts = {}
    real_push = mt._Releases.push

    def push(self, stage, release):
        def counted():
            counts[stage] = counts.get(stage, 0) + 1
            return release()

        real_push(self, stage, counted)

    monkeypatch.setattr(mt._Releases, "push", push)
    _abandon(env, LID)
    assert counts == {
        "state_root": 1,
        "repository_lock": 1,
        "runs_directory": 1,
        "run_directory": 1,
        "lifecycle_lock": 1,
        "repository_directory": 1,
        "maintenance_trace": 1,
    }


# ---------------------------------------------------------------------------
# Forced acknowledgement (section 5.2)
# ---------------------------------------------------------------------------


def test_row10f_forced_with_nothing_remaining_is_refused(env):
    """U4."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    report = _abandon(env, LID, forced=True)
    assert report.refusal == "nothing_remains_use_normal_abandonment"
    assert not (run_dir / ab.MARKER_FILENAME).exists() and _traces(env) == []


def test_row11f_forced_records_unresolved_and_keeps_reason_marker_only(env, monkeypatch):
    """M4 / C2: the reason is persisted only in abandonment.json."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", _present_container(LID))
    report = _abandon(env, LID, forced=True)
    assert report.outcome is mt.AbandonOutcome.RECORDED_UNRESOLVED
    marker, event = _assert_recorded(env, run_dir, report, ab.AbandonmentDisposition.ABANDONED_UNRESOLVED)
    assert marker.reason == REASON
    assert marker.remaining["baseline_container"] == "present"
    assert event["reason_recorded"] is True and "reason" not in event
    assert REASON not in (_traces(env)[0]).read_text()
    assert REASON not in repr(report)
    assert REASON in (run_dir / ab.MARKER_FILENAME).read_text()


def test_forced_rejects_an_invalid_reason_before_any_work(env):
    _init_namespace(env)
    _seed(env, LID)
    with pytest.raises(ValueError):
        mt.run_abandon(str(env.repo), LID, acknowledge_unresolved=True, reason="bad\nreason")
    with pytest.raises(ValueError):
        mt.run_abandon(str(env.repo), LID, acknowledge_unresolved=False, reason="unexpected")
    assert _traces(env) == []


def test_maintenance_modules_contain_no_removal_or_mutating_argv():
    """I16 / M13: abandonment and the CLI never remove, rename, or replace
    anything, and carry no docker rm / worktree remove / update-ref -d argv."""
    import ast
    import inspect

    from codeagent import cli

    forbidden_calls = {"unlink", "remove", "rmdir", "rmtree", "rename", "replace", "renames", "delete"}
    forbidden_names = {"_remove_and_confirm_absent", "_attempt_worktree_remove", "remove_if_still_empty"}
    for module in (mt, ab, cli):
        tree = ast.parse(inspect.getsource(module))
        dataclass_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses"
            for alias in node.names
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    # any os./shutil./Path-style removal, and any .delete()/.remove() on a collaborator
                    assert node.func.attr not in forbidden_calls, (module.__name__, node.func.attr)
                elif isinstance(node.func, ast.Name) and node.func.id not in dataclass_names:
                    assert node.func.id not in forbidden_calls, (module.__name__, node.func.id)
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names, (module.__name__, node.id)
            if isinstance(node, ast.List) and len(node.elts) >= 2:
                lead = [e.value for e in node.elts[:3] if isinstance(e, ast.Constant)]
                assert lead[:2] not in (["docker", "rm"], ["git", "worktree"]), (module.__name__, lead)
                assert "update-ref" not in lead, module.__name__


def test_maintenance_imports_only_read_only_reconciliation_entry_points():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mt))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "reconciliation"
        for alias in node.names
    }
    callables = {n for n in imported if n[0].islower() or n.startswith("_")}
    assert callables == {
        "_enumerate_runs",
        "inspect_remaining_resources",
        "open_maintenance_trace",
        "plan_repository",
        "reconcile_repository",
    }


# ---------------------------------------------------------------------------
# D1 correction pass (joint review F1-F6)
# ---------------------------------------------------------------------------


def _open_fds() -> set[int]:
    found = set()
    for fd in range(1024):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError:
            continue
        found.add(fd)
    return found


def _symlink_repo_locks(env):
    parent = env.state / "repo-locks"
    parent.rename(env.state / "saved-locks")
    parent.symlink_to(env.state / "saved-locks", target_is_directory=True)


@pytest.mark.parametrize("mode", ["reconcile", "dry_run", "abandon"])
def test_f1_unsafe_lock_parent_is_blocked_without_leaking(env, mode):
    """F1: a refusal raised while opening `repo-locks/` (after the state
    root is held) is a structured refusal, and nothing leaks."""
    _init_namespace(env)
    _symlink_repo_locks(env)
    before = _open_fds()
    if mode == "abandon":
        report = _abandon(env, LID)
        assert report.outcome is mt.AbandonOutcome.REFUSED and report.refusal == "repository_lock_unavailable"
    else:
        report = mt.run_reconcile(str(env.repo), dry_run=mode == "dry_run")
        assert report.result is mt.ReconcileResult.BLOCKED
        assert report.blocked_reason == "repository_lock_unavailable"
    assert _open_fds() == before
    assert report.unconfirmed_stages == ()


def test_f1_unexpected_setup_failure_after_state_root_releases_it(env, monkeypatch):
    _init_namespace(env)
    boom = RuntimeError("setup /Users/SENTINEL-PATH")
    monkeypatch.setattr(mt, "acquire_repository_lock", lambda *a, **k: (_ for _ in ()).throw(boom))
    before = _open_fds()
    with pytest.raises(RuntimeError) as excinfo:
        mt.run_reconcile(str(env.repo), dry_run=True)
    assert excinfo.value is boom
    assert _open_fds() == before
    assert not hasattr(excinfo.value, "codeagent_unconfirmed_stages")


def test_f1_unexpected_failure_after_lock_releases_lock_and_root(env, monkeypatch):
    _init_namespace(env)
    boom = RuntimeError("repo.json")
    monkeypatch.setattr(mt, "load_existing_repo_json", lambda *a, **k: (_ for _ in ()).throw(boom))
    before = _open_fds()
    with pytest.raises(RuntimeError):
        _abandon(env, LID)
    assert _open_fds() == before
    monkeypatch.undo()
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))
    assert mt.run_reconcile(str(env.repo), dry_run=True).blocked_reason is None  # the lock was released


def test_f1_setup_failure_plus_state_root_close_failure_keeps_primary_and_names_stage(env, monkeypatch):
    _init_namespace(env)
    boom = RuntimeError("setup")
    monkeypatch.setattr(mt, "acquire_repository_lock", lambda *a, **k: (_ for _ in ()).throw(boom))
    real_close = sr.StateRoot.close

    def close(self):
        real_close(self)
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")

    monkeypatch.setattr(sr.StateRoot, "close", close)
    with pytest.raises(RuntimeError) as excinfo:
        mt.run_reconcile(str(env.repo), dry_run=False)
    assert excinfo.value is boom
    assert excinfo.value.codeagent_unconfirmed_stages == ("state_root",)
    assert any("state_root" in note for note in excinfo.value.__notes__)


def test_f2_unexpected_inner_release_still_releases_outer():
    releases = mt._Releases()
    called = []
    releases.push("outer", lambda: called.append("outer"))

    def inner():
        called.append("inner")
        raise RuntimeError("/Users/SENTINEL-PATH")

    releases.push("inner", inner)
    failed, interrupt = releases.release_all()
    assert called == ["inner", "outer"]
    assert failed == ["inner"] and interrupt is None


def test_f2_interrupt_in_a_release_is_returned_after_every_release():
    releases = mt._Releases()
    called = []
    releases.push("outer", lambda: called.append("outer"))
    interrupt = KeyboardInterrupt()

    def inner():
        called.append("inner")
        raise interrupt

    releases.push("inner", inner)
    failed, returned = releases.release_all()
    assert called == ["inner", "outer"] and failed == ["inner"] and returned is interrupt


def _counting_releases(monkeypatch):
    counts: dict[str, int] = {}
    real_push = mt._Releases.push

    def push(self, stage, release):
        def counted():
            counts[stage] = counts.get(stage, 0) + 1
            return release()

        real_push(self, stage, counted)

    monkeypatch.setattr(mt._Releases, "push", push)
    return counts


def test_f2_body_failure_plus_cleanup_failures_releases_everything_once(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    counts = _counting_releases(monkeypatch)
    boom = RuntimeError("body /Users/SENTINEL-PATH")
    monkeypatch.setattr(mt, "inspect_remaining_resources", lambda **k: (_ for _ in ()).throw(boom))
    real_release = sl.LockHandle.release

    def release(self):
        real_release(self)
        if self.scope.kind is sl.LockKind.LIFECYCLE:
            raise RuntimeError("unexpected release /Users/SENTINEL-PATH")

    monkeypatch.setattr(sl.LockHandle, "release", release)
    real_close = sr.StateRoot.close

    def close(self):
        real_close(self)
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")

    monkeypatch.setattr(sr.StateRoot, "close", close)
    before = _open_fds()
    with pytest.raises(RuntimeError) as excinfo:
        _abandon(env, LID)
    assert excinfo.value is boom
    assert set(excinfo.value.codeagent_unconfirmed_stages) == {"lifecycle_lock", "state_root"}
    assert counts == {"state_root": 1, "repository_lock": 1, "runs_directory": 1, "run_directory": 1, "lifecycle_lock": 1}
    assert _open_fds() == before
    assert all("SENTINEL" not in note for note in excinfo.value.__notes__)


def test_f2_unexpected_release_error_without_body_failure_is_a_named_stage(env, monkeypatch):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    real_release = sl.LockHandle.release

    def release(self):
        real_release(self)
        if self.scope.kind is sl.LockKind.REPOSITORY:
            raise RuntimeError("unexpected")

    monkeypatch.setattr(sl.LockHandle, "release", release)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.RECORDED_ABANDONED
    assert "repository_lock" in report.unconfirmed_stages  # -> exit 4
    assert (run_dir / ab.MARKER_FILENAME).exists()


def test_f2_interrupt_during_release_propagates_after_all_releases(env, monkeypatch):
    _init_namespace(env)
    counts = _counting_releases(monkeypatch)
    real_release = sl.LockHandle.release

    def release(self):
        real_release(self)
        raise KeyboardInterrupt()

    monkeypatch.setattr(sl.LockHandle, "release", release)
    before = _open_fds()
    with pytest.raises(KeyboardInterrupt) as excinfo:
        mt.run_reconcile(str(env.repo), dry_run=True)
    assert counts == {"state_root": 1, "repository_lock": 1}
    assert excinfo.value.codeagent_unconfirmed_stages == ("repository_lock",)
    assert _open_fds() == before


# --- F4: ordinary inspection uncertainty vs CodeAgent's own cleanup --------


def test_f4_ordinary_inspection_failure_is_still_acknowledgeable(env, monkeypatch):
    _init_namespace(env)
    run_dir = _seed(env, LID)

    def down():
        raise rc._DockerListingError("down")

    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", down)
    report = _abandon(env, LID, forced=True)
    assert report.outcome is mt.AbandonOutcome.RECORDED_UNRESOLVED and report.unconfirmed_stages == ()
    assert report.remaining.cleanup_unconfirmed == ()
    assert (run_dir / ab.MARKER_FILENAME).exists()


def _cleanup_fault(kind, monkeypatch):
    if kind == "admin_scan":
        monkeypatch.setattr(
            rc,
            "_scan_worktree_admin_entries",
            lambda *a, **k: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")),
        )
    elif kind == "leaf_observation":
        monkeypatch.setattr(
            sr.StateRoot,
            "observe_materialized_worktree_leaf",
            lambda *a, **k: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")),
        )
    elif kind == "docker_listing":
        def listing():
            try:
                raise rc.BoundedProcessError(rc.BoundedProcessFailure.TERMINATION_UNCONFIRMED, "x")
            except rc.BoundedProcessError as exc:
                raise rc._DockerListingError("docker listing failed") from exc

        monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", listing)
    else:
        def git_listing(*a, **k):
            try:
                raise rc.GitSafetyError(rc.GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED, "x")
            except rc.GitSafetyError as exc:
                raise rc._WorktreeListingError("listing failed") from exc

        monkeypatch.setattr(rc, "_run_worktree_listing", git_listing)


@pytest.mark.parametrize("kind", ["admin_scan", "leaf_observation", "docker_listing", "worktree_listing"])
@pytest.mark.parametrize("forced", [False, True])
def test_f4_own_cleanup_failure_refuses_with_a_named_stage(env, monkeypatch, kind, forced):
    """F4: forced acknowledgement never absorbs CodeAgent's own unconfirmed
    cleanup; both forms refuse before any write and name the stage."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    _cleanup_fault(kind, monkeypatch)
    report = _abandon(env, LID, forced=forced)
    assert report.outcome is mt.AbandonOutcome.REFUSED
    assert report.refusal == "inspection_cleanup_unconfirmed"
    assert report.unconfirmed_stages == (f"inspection_{kind}",)
    assert report.remaining.cleanup_unconfirmed == (kind,)
    assert not (run_dir / ab.MARKER_FILENAME).exists() and _traces(env) == []


def test_f4_own_cleanup_failure_in_the_dry_run_is_named(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    _cleanup_fault("admin_scan", monkeypatch)
    report = mt.run_reconcile(str(env.repo), dry_run=True)
    assert report.result is mt.ReconcileResult.BLOCKED
    assert "inspection_admin_scan" in report.unconfirmed_stages


# --- F5: trace presence ----------------------------------------------------


def test_f5_failure_before_trace_creation_reports_no_trace(env, monkeypatch):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    monkeypatch.setattr(
        rc,
        "open_private_create_exclusive_at",
        lambda *a, **k: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")),
    )
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.REFUSED and report.refusal == "maintenance_trace_unavailable"
    assert report.maintenance_id is None and _traces(env) == []
    assert not (run_dir / ab.MARKER_FILENAME).exists()


def test_f5_failure_after_trace_creation_reports_the_trace(env, monkeypatch):
    """The real opener: the file is created, then its directory fsync fails."""
    _init_namespace(env)
    run_dir = _seed(env, LID)
    real = rc.fsync_fd
    maintenance_ino = []

    def fsync_fd(fd):
        if os.fstat(fd).st_ino in maintenance_ino:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "x")
        return real(fd)

    (env.state / "repos" / env.repo_key / "maintenance").mkdir(mode=0o700)
    maintenance_ino.append(os.stat(env.state / "repos" / env.repo_key / "maintenance").st_ino)
    monkeypatch.setattr(rc, "fsync_fd", fsync_fd)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.NOT_RECORDED and report.refusal == "maintenance_trace_unconfirmed"
    (trace,) = _traces(env)
    assert trace.name == f"{report.maintenance_id}.jsonl" and trace.read_bytes() == b""
    assert not (run_dir / ab.MARKER_FILENAME).exists()
    monkeypatch.setattr(rc, "fsync_fd", real)
    assert _abandon(env, LID).outcome is mt.AbandonOutcome.RECORDED_ABANDONED


def test_f5_exclusive_create_then_verification_failure_reports_the_file(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    real = rc.open_private_create_exclusive_at

    def create(dir_fd, name, mode=0o600):
        fd = real(dir_fd, name, mode)
        os.close(fd)
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.UNSAFE_PERMISSIONS, "x")

    monkeypatch.setattr(rc, "open_private_create_exclusive_at", create)
    report = _abandon(env, LID)
    assert report.outcome is mt.AbandonOutcome.NOT_RECORDED
    (trace,) = _traces(env)
    assert trace.name == f"{report.maintenance_id}.jsonl"


def test_f5_explicit_reconcile_reports_an_incomplete_trace(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    monkeypatch.setattr(
        rc._MaintenanceTraceWriter,
        "started",
        lambda self: (_ for _ in ()).throw(rc.ReconciliationError(rc.ReconciliationFailure.SUBSTRATE_UNAVAILABLE, "x")),
    )
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.BLOCKED and report.trace_incomplete
    (trace,) = _traces(env)
    assert trace.name == f"{report.maintenance_id}.jsonl"


def test_f5_explicit_reconcile_failure_before_creation_reports_no_trace(env, monkeypatch):
    _init_namespace(env)
    _seed(env, LID)
    monkeypatch.setattr(
        rc,
        "open_private_create_exclusive_at",
        lambda *a, **k: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")),
    )
    report = mt.run_reconcile(str(env.repo), dry_run=False)
    assert report.result is mt.ReconcileResult.BLOCKED
    assert report.maintenance_id is None and not report.trace_incomplete and _traces(env) == []


# --- F6 --------------------------------------------------------------------


def test_f6_assert_recorded_closes_its_descriptor(env):
    _init_namespace(env)
    run_dir = _seed(env, LID)
    report = _abandon(env, LID)
    before = _open_fds()
    _assert_recorded(env, run_dir, report, ab.AbandonmentDisposition.ABANDONED)
    assert _open_fds() == before


def test_body_exception_stays_primary_over_a_release_interrupt(env, monkeypatch):
    """First failure is primary, later failures are named: an interrupt
    raised by a release while a body exception propagates is recorded by
    stage, and every release is still attempted."""
    _init_namespace(env)
    counts = _counting_releases(monkeypatch)
    boom = RuntimeError("body")
    monkeypatch.setattr(mt, "plan_repository", lambda **k: (_ for _ in ()).throw(boom))
    real_release = sl.LockHandle.release

    def release(self):
        real_release(self)
        raise KeyboardInterrupt()

    monkeypatch.setattr(sl.LockHandle, "release", release)
    before = _open_fds()
    with pytest.raises(RuntimeError) as excinfo:
        mt.run_reconcile(str(env.repo), dry_run=True)
    assert excinfo.value is boom
    assert excinfo.value.codeagent_unconfirmed_stages == ("repository_lock",)
    assert counts == {"state_root": 1, "repository_lock": 1}
    assert _open_fds() == before
