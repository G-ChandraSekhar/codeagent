"""Real-substrate tests for `codeagent reconcile` (ADR 0004 sections 10-12,
Amendment 18; ledger D1). Every command runs as a real subprocess
(`python -m codeagent.cli`) against real Git, real lock files, real
SIGKILLed children, and -- where marked -- a real Docker daemon and the
real lifecycle-aware composition's crash shapes."""

from __future__ import annotations

import importlib.metadata
import json
import multiprocessing
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from codeagent import abandonment as ab
from codeagent import lifecycle_store as ls
from codeagent import repo_identity as ri
from tests.integration.test_lifecycle_run import (  # noqa: F401 - `env` is a fixture
    _assert_fully_clean,
    _codeagent_refs,
    _container_names,
    _crash,
    _docker_available,
    _invoke,
    _leaf,
    _only_run,
    _projection,
    _registered_worktrees,
    env,
)

REASON = "/Users/SENTINEL-REASON-9b1c/secret"
PATH_SENTINEL = "SENTINEL-7f3a"
_DOCKER_REASON = "requires a running local Docker daemon"
requires_docker = pytest.mark.skipif(not _docker_available(), reason=_DOCKER_REASON)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cli(state: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    child_env = {**os.environ, "CODEAGENT_STATE_DIR": str(state), **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-m", "codeagent.cli", "reconcile", *args],
        capture_output=True,
        text=True,
        env=child_env,
        timeout=300,
    )


def _make_repo(parent: Path, name="repo") -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    return repo


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """A plain repository and an isolated state root, for shapes that need
    no real container. `prepare_lifecycle` creates a real PREPARING entry."""
    repo = _make_repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))

    class P:
        pass

    p = P()
    p.repo, p.state = repo, state
    p.repo_key = ri.discover_repository_identity_and_context(str(repo))[0].repo_key
    return p


def _real_entry(p) -> tuple[str, Path]:
    """A real dead PREPARING entry (lock file, valid projection)."""
    lease = ls.prepare_lifecycle(str(p.repo), run_id="r-d1")
    lifecycle_id = lease.lifecycle_id
    lease.close()
    return lifecycle_id, p.state / "repos" / p.repo_key / "runs" / lifecycle_id


def _residual(p, lifecycle_id="c" * 32, *, lock=True) -> Path:
    """The 3B-1 residual: a run directory without lifecycle.json."""
    if not (p.state / "repos" / p.repo_key / "repo.json").exists():
        _real_entry(p)
        # Reconcile the setup entry away so only the residual remains undecided.
        assert _cli(p.state, "--repo", str(p.repo)).returncode == 0
    run_dir = p.state / "repos" / p.repo_key / "runs" / lifecycle_id
    run_dir.mkdir(mode=0o700)
    if lock:
        (run_dir / "lifecycle.lock").write_bytes(b"")
        (run_dir / "lifecycle.lock").chmod(0o600)
    return run_dir


def _traces(state: Path) -> list[Path]:
    return sorted(state.glob("repos/*/maintenance/*.jsonl"))


def _explicit_traces(state: Path) -> list[Path]:
    """Traces written by maintenance commands (setup via `prepare_lifecycle`
    writes its own `pre_run` traces)."""
    return [t for t in _traces(state) if '"trigger":"explicit"' in t.read_text() or t.read_text() == ""]


def _tree(root: Path) -> dict:
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            out[os.path.relpath(path, root)] = (stat.S_IFMT(st.st_mode), st.st_mode, st.st_size, st.st_ino, st.st_mtime_ns)
    return out


def _world(state: Path, repo: Path, lifecycle_ids) -> tuple:
    refs = subprocess.run(["git", "-C", str(repo), "for-each-ref"], capture_output=True, text=True, check=True).stdout
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"], capture_output=True, check=True
    ).stdout
    containers = ()
    if _docker_available():
        listing = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}} {{.State}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        containers = tuple(sorted(l for l in listing if any(i in l for i in lifecycle_ids)))
    return _tree(state), refs, worktrees, containers


def _fake_docker_bin(tmp_path: Path) -> dict:
    """A PATH whose `docker` always fails, so inspection cannot succeed."""
    bin_dir = tmp_path / "fake-docker-bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _marker_of(run_dir: Path) -> dict:
    return json.loads((run_dir / ab.MARKER_FILENAME).read_text())


# ---------------------------------------------------------------------------
# 13.5-1 Dry-run invariance
# ---------------------------------------------------------------------------


def test_dry_run_with_no_state_creates_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    state = tmp_path / "state"
    result = _cli(state, "--repo", str(repo), "--dry-run")
    assert result.returncode == 0 and "no_recorded_state" in result.stdout
    assert "maintenance trace: none" in result.stdout
    assert not state.exists()


def test_dry_run_never_creates_a_missing_lock_file(plain):
    lifecycle_id, run_dir = _real_entry(plain)
    (run_dir / "lifecycle.lock").unlink()
    before = _tree(plain.state)
    result = _cli(plain.state, "--repo", str(plain.repo), "--dry-run")
    assert result.returncode == 4
    assert _tree(plain.state) == before
    (plain.state / "repo-locks" / f"{plain.repo_key}.lock").unlink()
    before = _tree(plain.state)
    assert _cli(plain.state, "--repo", str(plain.repo), "--dry-run").returncode == 4
    assert _tree(plain.state) == before


@requires_docker
@pytest.mark.parametrize("crash_point", ["baseline_present", "ref_present"])
def test_dry_run_is_invariant_over_real_git_docker_and_state(env, crash_point):
    """M1 / M15: a real crash shape (T36: worktree + live container; or
    T37: worktree + checkpoint ref), a 3B-1 residual, an abandoned entry,
    and a temp-alone entry. Nothing in the state root, Git, or Docker
    changes."""
    repo_key, t36, _ = _crash(env, crash_point)
    runs = env.state / "repos" / repo_key / "runs"
    residual = runs / ("c" * 32)
    residual.mkdir(mode=0o700)
    abandoned = runs / ("d" * 32)
    abandoned.mkdir(mode=0o700)
    assert _cli(env.state, "--repo", str(env.repo), "--abandon", "d" * 32).returncode == 0
    temp = residual / (".abandonment.json.tmp-" + "0" * 16)
    temp.write_bytes(b"stale")
    temp.chmod(0o600)
    ids = [t36, "c" * 32, "d" * 32]
    before = _world(env.state, env.repo, ids)
    result = _cli(env.state, "--repo", str(env.repo), "--dry-run")
    assert result.returncode == 4
    assert _world(env.state, env.repo, ids) == before
    assert f"entry {'d' * 32}: skipped_abandoned" in result.stdout
    assert "maintenance trace: none" in result.stdout
    if crash_point == "baseline_present":
        assert f"codeagent-baseline-{t36}" in _container_names()
    else:
        assert _codeagent_refs(env.repo) == [f"refs/codeagent/runs/{t36}/checkpoint"]


# ---------------------------------------------------------------------------
# 13.5-2/3 The 3B-1 residual
# ---------------------------------------------------------------------------


@requires_docker
def test_residual_blocks_then_normal_abandon_clears_it(plain):
    residual = _residual(plain)
    assert _cli(plain.state, "--repo", str(plain.repo)).returncode == 4
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", residual.name)
    assert abandon.returncode == 0, abandon.stdout + abandon.stderr
    assert "outcome=RECORDED_ABANDONED" in abandon.stdout
    assert _marker_of(residual)["disposition"] == "ABANDONED"
    reconcile = _cli(plain.state, "--repo", str(plain.repo))
    assert reconcile.returncode == 0 and "skipped_abandoned" in reconcile.stdout
    with ls.prepare_lifecycle(str(plain.repo), run_id="after") as lease:
        assert lease.unresolved_acknowledged == ()


@requires_docker
def test_residual_without_a_lock_file_is_abandoned_and_the_lock_created(plain):
    """U10: real abandonment creates the missing lock; the dry run before it did not."""
    residual = _residual(plain, lock=False)
    before = _tree(plain.state)
    assert _cli(plain.state, "--repo", str(plain.repo), "--dry-run").returncode == 4
    assert _tree(plain.state) == before
    assert _cli(plain.state, "--repo", str(plain.repo), "--abandon", residual.name).returncode == 0
    assert (residual / "lifecycle.lock").exists()


@requires_docker
def test_forced_with_nothing_remaining_is_refused(plain):
    residual = _residual(plain)
    result = _cli(
        plain.state, "--repo", str(plain.repo), "--abandon", residual.name, "--acknowledge-unresolved", "--reason", "x"
    )
    assert result.returncode == 4 and "nothing_remains_use_normal_abandonment" in result.stdout
    assert not (residual / ab.MARKER_FILENAME).exists()
    assert [t for t in _explicit_traces(plain.state) if "Abandonment" in t.read_text() or t.read_text() == ""] == []


# ---------------------------------------------------------------------------
# 13.5-4 The real T36 shape; 13.5-5 a partial-removal leftover
# ---------------------------------------------------------------------------


@requires_docker
def test_t36_shape_normal_refused_forced_records_and_nothing_is_touched(env):
    """M2 / M4 / M5 / M13."""
    repo_key, lifecycle_id, proj = _crash(env, "baseline_present")
    run_dir = env.state / "repos" / repo_key / "runs" / lifecycle_id
    container = f"codeagent-baseline-{lifecycle_id}"
    projection_bytes = (run_dir / "lifecycle.json").read_bytes()
    worktrees_before, refs_before = _registered_worktrees(env.repo), _codeagent_refs(env.repo)
    assert worktrees_before != set()  # T36: a live container plus a worktree (no ref yet at this crash point)
    assert _cli(env.state, "--repo", str(env.repo)).returncode == 4

    normal = _cli(env.state, "--repo", str(env.repo), "--abandon", lifecycle_id)
    assert normal.returncode == 4 and "refusal: resources_remain" in normal.stdout
    assert "baseline_container=present" in normal.stdout
    assert not (run_dir / ab.MARKER_FILENAME).exists()

    forced = _cli(
        env.state, "--repo", str(env.repo), "--abandon", lifecycle_id, "--acknowledge-unresolved", "--reason", REASON
    )
    assert forced.returncode == 3, forced.stdout + forced.stderr
    marker = _marker_of(run_dir)
    assert marker["disposition"] == "ABANDONED_UNRESOLVED" and marker["remaining"]["baseline_container"] == "present"
    # Nothing was deleted, adopted, or changed.
    assert container in _container_names()
    assert _registered_worktrees(env.repo) == worktrees_before
    assert _codeagent_refs(env.repo) == refs_before
    assert (run_dir / "lifecycle.json").read_bytes() == projection_bytes

    reconcile = _cli(env.state, "--repo", str(env.repo))
    assert reconcile.returncode == 3
    assert f"WARNING: run {lifecycle_id}" in reconcile.stderr
    assert "SENTINEL-REASON" not in reconcile.stdout + reconcile.stderr
    with ls.prepare_lifecycle(str(env.repo), run_id="after") as lease:
        assert [a.lifecycle_id for a in lease.unresolved_acknowledged] == [lifecycle_id]

    again = _cli(
        env.state, "--repo", str(env.repo), "--abandon", lifecycle_id, "--acknowledge-unresolved", "--reason", "x"
    )
    assert again.returncode == 4 and "already_abandoned" in again.stdout

    # The operator removes the resources outside CodeAgent; the entry stays unresolved forever.
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=True)
    for path in _registered_worktrees(env.repo):
        subprocess.run(["git", "-C", str(env.repo), "worktree", "remove", "--force", path], check=True)
    for ref in _codeagent_refs(env.repo):
        subprocess.run(["git", "-C", str(env.repo), "update-ref", "-d", ref], check=True)
    assert _cli(env.state, "--repo", str(env.repo)).returncode == 3


@requires_docker
def test_partial_removal_leftover_normal_refused_forced_records(env):
    """A materialized worktree record whose registration is gone but whose
    directory remains (an Amendment 13 refusal shape)."""
    repo_key, lifecycle_id, proj = _crash(env, "worktree_present")
    leaf = _leaf(env.state, repo_key, lifecycle_id)
    subprocess.run(["git", "-C", str(env.repo), "worktree", "remove", "--force", str(leaf)], check=True)
    leaf.mkdir(mode=0o700)
    (leaf / "left-behind").write_text("x")
    assert _cli(env.state, "--repo", str(env.repo)).returncode == 4
    normal = _cli(env.state, "--repo", str(env.repo), "--abandon", lifecycle_id)
    assert normal.returncode == 4 and "worktree_directory=present" in normal.stdout
    forced = _cli(
        env.state, "--repo", str(env.repo), "--abandon", lifecycle_id, "--acknowledge-unresolved", "--reason", "leftover"
    )
    assert forced.returncode == 3
    assert (leaf / "left-behind").read_text() == "x"
    shutil.rmtree(leaf)


# ---------------------------------------------------------------------------
# 13.5-6 Inspection failure
# ---------------------------------------------------------------------------


def test_inspection_failure_refuses_normal_and_allows_forced(plain, tmp_path):
    """M3: a failing `docker` makes container observations unknown."""
    residual = _residual(plain) if _docker_available() else None
    if residual is None:
        lifecycle_id, residual = _real_entry(plain)
    fake = _fake_docker_bin(tmp_path)
    normal = _cli(plain.state, "--repo", str(plain.repo), "--abandon", residual.name, extra_env=fake)
    assert normal.returncode == 4 and "refusal: inspection_failed" in normal.stdout
    assert "baseline_container=unknown" in normal.stdout
    assert not (residual / ab.MARKER_FILENAME).exists()
    forced = _cli(
        plain.state,
        "--repo",
        str(plain.repo),
        "--abandon",
        residual.name,
        "--acknowledge-unresolved",
        "--reason",
        "docker is down",
        extra_env=fake,
    )
    assert forced.returncode == 3
    assert _marker_of(residual)["remaining"]["baseline_container"] == "unknown"


# ---------------------------------------------------------------------------
# 13.5-8/9 Real cross-process lock contention
# ---------------------------------------------------------------------------


def _child_hold_repository_lock(state: str, repo: str, ready, release) -> None:
    os.environ["CODEAGENT_STATE_DIR"] = state
    from codeagent import _lifecycle_fs as lf_c
    from codeagent import repo_identity as ri_c
    from codeagent import state_locks as sl_c
    from codeagent import state_root as sr_c

    identity, _ = ri_c.discover_repository_identity_and_context(repo)
    fd, canonical = sr_c.open_or_create_canonical_root(lf_c.resolve_state_root_path())
    root = sr_c.init_state_root(fd, canonical)
    lock = sl_c.acquire_repository_lock(root, identity.repo_key)
    ready.set()
    release.wait(120)
    lock.release()
    root.close()


def _child_hold_lifecycle_lock(run_dir: str, repo_key: str, lifecycle_id: str, ready, release) -> None:
    from codeagent import state_locks as sl_c

    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    lock = sl_c.acquire_lifecycle_lock(fd, repo_key=repo_key, lifecycle_id=lifecycle_id, diagnostic_path="x")
    ready.set()
    release.wait(120)
    lock.release()
    os.close(fd)


def _with_child(target, args):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=target, args=(*args, ready, release))
    proc.start()
    assert ready.wait(60), "child did not acquire the lock"
    return proc, release


def test_repository_lock_held_by_another_process_blocks_every_mode(plain):
    lifecycle_id, run_dir = _real_entry(plain)
    proc, release = _with_child(_child_hold_repository_lock, (str(plain.state), str(plain.repo)))
    try:
        before = _tree(plain.state)
        for args in ([], ["--dry-run"], ["--abandon", lifecycle_id]):
            result = _cli(plain.state, "--repo", str(plain.repo), *args)
            assert result.returncode == 4, args
            assert "repository_active" in result.stdout
        assert _tree(plain.state) == before
    finally:
        release.set()
        proc.join(60)
    assert proc.exitcode == 0


def test_lifecycle_lock_held_by_another_process_refuses_abandonment(plain):
    """M8."""
    lifecycle_id, run_dir = _real_entry(plain)
    proc, release = _with_child(_child_hold_lifecycle_lock, (str(run_dir), plain.repo_key, lifecycle_id))
    try:
        result = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
        assert result.returncode == 4 and "refusal: lifecycle_active" in result.stdout
        assert not (run_dir / ab.MARKER_FILENAME).exists() and _explicit_traces(plain.state) == []
        dry = _cli(plain.state, "--repo", str(plain.repo), "--dry-run")
        assert dry.returncode == 4 and "skipped_active" in dry.stdout
    finally:
        release.set()
        proc.join(60)
    assert proc.exitcode == 0


# ---------------------------------------------------------------------------
# 13.5-10/11/12 Refused namespace, missing lock beside state, lock-only
# ---------------------------------------------------------------------------


def test_refused_namespace_names_fields_only_and_writes_nothing(plain):
    """M9."""
    lifecycle_id, _ = _real_entry(plain)
    path = plain.state / "repos" / plain.repo_key / "repo.json"
    payload = json.loads(path.read_text())
    payload["st_ino"] += 1
    path.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    before = _tree(plain.state)
    reconcile = _cli(plain.state, "--repo", str(plain.repo))
    assert reconcile.returncode == 4 and "mismatched identity fields: st_ino" in reconcile.stdout
    assert str(payload["st_ino"]) not in reconcile.stdout
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert abandon.returncode == 4 and "namespace_refused" in abandon.stdout
    assert _tree(plain.state) == before


def _assert_missing_lock_blocks(state, repo, lifecycle_id, repo_key):
    lock = state / "repo-locks" / f"{repo_key}.lock"
    lock.unlink()
    before = _tree(state)
    for args in ([], ["--dry-run"], ["--abandon", lifecycle_id]):
        result = _cli(state, "--repo", str(repo), *args)
        assert result.returncode == 4, args
        assert "repository_lock_missing_with_state" in result.stdout
    assert _tree(state) == before
    assert not lock.exists()


def test_deleted_repository_lock_beside_a_blocked_entry_never_reports_clean(plain):
    """C3 / M23."""
    lifecycle_id, _ = _real_entry(plain)
    _assert_missing_lock_blocks(plain.state, plain.repo, lifecycle_id, plain.repo_key)


@requires_docker
def test_deleted_repository_lock_beside_a_real_complete_run_never_reports_clean(env):
    """C3 / M23: even a fully clean terminal history is recorded state."""
    assert _invoke(env).release_confirmed
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["state"] == "COMPLETE"
    _assert_missing_lock_blocks(env.state, env.repo, lifecycle_id, repo_key)


def test_deleted_repo_json_beside_runs_is_never_no_recorded_state(plain):
    _real_entry(plain)
    (plain.state / "repos" / plain.repo_key / "repo.json").unlink()
    for args in ([], ["--dry-run"]):
        result = _cli(plain.state, "--repo", str(plain.repo), *args)
        assert result.returncode == 4 and "no_recorded_state" not in result.stdout


def test_lock_present_namespace_absent_is_no_recorded_state(plain):
    from codeagent import _lifecycle_fs as lf_c
    from codeagent import state_root as sr_c

    fd, canonical = sr_c.open_or_create_canonical_root(lf_c.resolve_state_root_path())
    sr_c.init_state_root(fd, canonical).close()
    (plain.state / "repo-locks").mkdir(mode=0o700)
    lock = plain.state / "repo-locks" / f"{plain.repo_key}.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    before = _tree(plain.state)
    reconcile = _cli(plain.state, "--repo", str(plain.repo))
    assert reconcile.returncode == 0 and "no_recorded_state" in reconcile.stdout
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", "a" * 32)
    assert abandon.returncode == 4 and "refusal: no_recorded_state" in abandon.stdout
    assert _tree(plain.state) == before


# ---------------------------------------------------------------------------
# 13.5-13 Already clean-final; 13.5-19 explicit reconcile of a real shape
# ---------------------------------------------------------------------------


@requires_docker
def test_real_complete_run_cannot_be_abandoned(env):
    result = _invoke(env)
    assert result.release_confirmed
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["state"] == "COMPLETE"
    abandon = _cli(env.state, "--repo", str(env.repo), "--abandon", lifecycle_id)
    assert abandon.returncode == 4 and "already_clean_final" in abandon.stdout
    assert _cli(env.state, "--repo", str(env.repo)).returncode == 0


@requires_docker
def test_explicit_reconcile_of_the_real_t37_shape(env):
    repo_key, lifecycle_id, proj = _crash(env, "ref_present")
    result = _cli(env.state, "--repo", str(env.repo))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"entry {lifecycle_id}: reconciled" in result.stdout
    (trace,) = _explicit_traces(env.state)
    assert {json.loads(l)["trigger"] for l in trace.read_text().splitlines()} == {"explicit"}
    assert _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)["state"] == "RECONCILED"
    _assert_fully_clean(env, repo_key, lifecycle_id)


# ---------------------------------------------------------------------------
# 13.5-14 Hostile filesystem shapes
# ---------------------------------------------------------------------------


def _blocked_without_marker_effect(p, run_dir):
    result = _cli(p.state, "--repo", str(p.repo))
    assert result.returncode == 4
    assert f"entry {run_dir.name}: refused" in result.stdout


def test_symlinked_final_marker_is_refused(plain, tmp_path):
    lifecycle_id, run_dir = _real_entry(plain)
    target = tmp_path / "marker-elsewhere"
    target.write_text("{}")
    (run_dir / ab.MARKER_FILENAME).symlink_to(target)
    _blocked_without_marker_effect(plain, run_dir)


def test_marker_hard_linked_from_outside_is_refused(plain, tmp_path):
    lifecycle_id, run_dir = _real_entry(plain)
    marker = run_dir / ab.MARKER_FILENAME
    from codeagent import state_root as sr_c

    payload = {
        "schema_version": 1,
        "lifecycle_id": lifecycle_id,
        "repo_key": plain.repo_key,
        "state_root_id": json.loads((plain.state / sr_c.STATE_ROOT_JSON_FILENAME).read_text())["state_root_id"],
        "disposition": "ABANDONED",
        "maintenance_id": "e" * 32,
        "timestamp": "2026-10-04T00:00:00Z",
    }
    marker.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    marker.chmod(0o600)
    os.link(marker, tmp_path / "outside")
    _blocked_without_marker_effect(plain, run_dir)


def test_symlinked_marker_temp_is_refused(plain, tmp_path):
    lifecycle_id, run_dir = _real_entry(plain)
    (run_dir / (".abandonment.json.tmp-" + "0" * 16)).symlink_to(tmp_path)
    _blocked_without_marker_effect(plain, run_dir)
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert abandon.returncode == 4 and "marker_temp_invalid" in abandon.stdout


def test_symlinked_lifecycle_lock_refuses_abandonment(plain, tmp_path):
    lifecycle_id, run_dir = _real_entry(plain)
    (run_dir / "lifecycle.lock").unlink()
    (tmp_path / "lock-target").write_bytes(b"")
    (run_dir / "lifecycle.lock").symlink_to(tmp_path / "lock-target")
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert abandon.returncode == 4 and "lifecycle_lock_unavailable" in abandon.stdout
    assert not (run_dir / ab.MARKER_FILENAME).exists()


def test_symlinked_run_directory_aborts_the_pass_and_abandonment(plain, tmp_path):
    _real_entry(plain)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (plain.state / "repos" / plain.repo_key / "runs" / ("f" * 32)).symlink_to(elsewhere)
    assert _cli(plain.state, "--repo", str(plain.repo)).returncode == 4
    abandon = _cli(plain.state, "--repo", str(plain.repo), "--abandon", "f" * 32)
    assert abandon.returncode == 4
    assert os.listdir(elsewhere) == []


# ---------------------------------------------------------------------------
# 13.5-15 SIGKILL during abandonment publication
# ---------------------------------------------------------------------------


def _child_abandon_and_die(repo: str, state: str, lifecycle_id: str, point: str) -> None:
    os.environ["CODEAGENT_STATE_DIR"] = state
    from codeagent import _lifecycle_fs as lf_c
    from codeagent import maintenance as mt_c
    from codeagent import reconciliation as rc_c

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    if point == "after_trace_create":
        real = mt_c.open_maintenance_trace

        def opener(*a, **k):
            real(*a, **k)
            die()

        mt_c.open_maintenance_trace = opener
    elif point == "after_temp_write":
        lf_c.os.link = lambda *a, **k: die()
    elif point == "after_link":
        real_link = lf_c.os.link

        def link(*a, **k):
            real_link(*a, **k)
            die()

        lf_c.os.link = link
    elif point == "after_dir_fsync":
        lf_c.os.unlink = lambda *a, **k: die()
    elif point == "after_unlink":
        real_unlink = lf_c.os.unlink

        def unlink(*a, **k):
            real_unlink(*a, **k)
            die()

        lf_c.os.unlink = unlink
    elif point == "after_event":
        real_event = rc_c._MaintenanceTraceWriter.abandonment_recorded

        def event(self, **k):
            real_event(self, **k)
            die()

        rc_c._MaintenanceTraceWriter.abandonment_recorded = event
    mt_c.run_abandon(repo, lifecycle_id, acknowledge_unresolved=False, reason=None)
    die()


def _kill_during_abandon(p, lifecycle_id, point):
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child_abandon_and_die, args=(str(p.repo), str(p.state), lifecycle_id, point))
    proc.start()
    proc.join(120)
    if proc.is_alive():
        proc.kill()
        proc.join(10)
        pytest.fail("child did not self-SIGKILL")
    assert proc.exitcode == -signal.SIGKILL


@requires_docker
@pytest.mark.parametrize(
    "point,marker,twin,event",
    [
        ("after_trace_create", False, False, False),
        ("after_temp_write", False, False, False),
        ("after_link", True, True, False),
        ("after_dir_fsync", True, True, False),
        ("after_unlink", True, False, False),
        ("after_event", True, False, True),
    ],
)
def test_sigkill_publication_windows(plain, point, marker, twin, event):
    """C1 / C5 / section 11."""
    lifecycle_id, run_dir = _real_entry(plain)
    _kill_during_abandon(plain, lifecycle_id, point)
    temps = [p for p in run_dir.iterdir() if ab.MARKER_TEMP_RE.fullmatch(p.name)]
    (trace,) = _explicit_traces(plain.state)
    lines = trace.read_text().splitlines()
    assert [json.loads(l)["event_type"] for l in lines] == (["AbandonmentRecorded"] if event else [])
    assert (run_dir / ab.MARKER_FILENAME).exists() is marker
    if marker:
        assert os.stat(run_dir / ab.MARKER_FILENAME).st_nlink == (2 if twin else 1)
        reconcile = _cli(plain.state, "--repo", str(plain.repo))
        assert reconcile.returncode == 0 and "skipped_abandoned" in reconcile.stdout
        retry = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
        assert retry.returncode == 4 and "already_abandoned" in retry.stdout
        return
    # No final marker: never abandoned, never refused because of a temp.
    assert len(temps) == (1 if point == "after_temp_write" else 0)
    stale = {t.name: t.read_bytes() for t in temps}
    dry = _cli(plain.state, "--repo", str(plain.repo), "--dry-run")
    assert "skipped_abandoned" not in dry.stdout and f"entry {lifecycle_id}: pending" in dry.stdout
    retry = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert retry.returncode == 0, retry.stdout
    assert {t: (run_dir / t).read_bytes() for t in stale} == stale
    assert _cli(plain.state, "--repo", str(plain.repo)).returncode == 0


@requires_docker
def test_sigkill_with_stale_temp_alone_then_reconcile_reconciles_normally(plain):
    lifecycle_id, run_dir = _real_entry(plain)
    _kill_during_abandon(plain, lifecycle_id, "after_temp_write")
    (temp,) = [p for p in run_dir.iterdir() if ab.MARKER_TEMP_RE.fullmatch(p.name)]
    content = temp.read_bytes()
    result = _cli(plain.state, "--repo", str(plain.repo))
    assert result.returncode == 0 and f"entry {lifecycle_id}: reconciled" in result.stdout
    assert temp.read_bytes() == content


# ---------------------------------------------------------------------------
# 13.5-16 The stale-temp bound
# ---------------------------------------------------------------------------


def _seed_temps(run_dir, n):
    for i in range(n):
        t = run_dir / f".abandonment.json.tmp-{i:016x}"
        t.write_bytes(b"stale")
        t.chmod(0o600)


def test_sixteen_stale_temps_refuse_a_new_attempt(plain):
    lifecycle_id, run_dir = _real_entry(plain)
    _seed_temps(run_dir, 16)
    before = _tree(plain.state)
    result = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert result.returncode == 4 and "too_many_stale_abandonment_temps" in result.stdout
    assert _tree(plain.state) == before


@requires_docker
def test_fifteen_stale_temps_do_not_refuse(plain):
    lifecycle_id, run_dir = _real_entry(plain)
    _seed_temps(run_dir, 15)
    result = _cli(plain.state, "--repo", str(plain.repo), "--abandon", lifecycle_id)
    assert result.returncode == 0 and "abandonment temporary leftovers: 15" in result.stdout


# ---------------------------------------------------------------------------
# 13.5-17 Output sanitization and reason isolation
# ---------------------------------------------------------------------------


def test_output_and_traces_never_carry_paths_or_the_reason(tmp_path, monkeypatch):
    """C2 / M21 / M22."""
    base = tmp_path / PATH_SENTINEL
    repo = _make_repo(base)
    state = base / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    lease = ls.prepare_lifecycle(str(repo), run_id="r-sanitize")
    lifecycle_id, repo_key = lease.lifecycle_id, lease.repo_key
    lease.close()
    leaf = state / "worktrees" / repo_key / lifecycle_id
    leaf.mkdir(parents=True, mode=0o700)  # something attributable remains
    outputs = []
    forced = _cli(state, "--repo", str(repo), "--abandon", lifecycle_id, "--acknowledge-unresolved", "--reason", REASON)
    assert forced.returncode == 3, forced.stdout + forced.stderr
    outputs.append(forced)
    outputs.append(_cli(state, "--repo", str(repo), "--dry-run"))
    outputs.append(_cli(state, "--repo", str(repo)))
    outputs.append(_cli(state, "--repo", str(repo), "--abandon", lifecycle_id))
    for result in outputs:
        text = result.stdout + result.stderr
        assert PATH_SENTINEL not in text and "SENTINEL-REASON" not in text, text
        assert str(repo) not in text and str(state) not in text
    assert "WARNING: run" in outputs[2].stderr
    traces = _traces(state)
    assert traces
    for trace in traces:
        content = trace.read_text()
        assert PATH_SENTINEL not in content and "SENTINEL-REASON" not in content
        for line in content.splitlines():
            event = json.loads(line)
            if event["event_type"] == "AbandonmentRecorded":
                assert event["reason_recorded"] is True and "reason" not in event
    marker_text = (state / "repos" / repo_key / "runs" / lifecycle_id / ab.MARKER_FILENAME).read_text()
    assert marker_text.count("SENTINEL-REASON") == 1
    everything = "".join(p.read_text(errors="ignore") for p in state.rglob("*") if p.is_file())
    assert everything.count("SENTINEL-REASON") == 1
    shutil.rmtree(leaf)


# ---------------------------------------------------------------------------
# 13.5-18 Console script; 13.6 marker pin
# ---------------------------------------------------------------------------


def test_console_script_is_installed_and_reconcile_only():
    (entry,) = [e for e in importlib.metadata.entry_points(group="console_scripts") if e.name == "codeagent"]
    assert entry.value == "codeagent.cli:main"
    script = Path(sys.executable).parent / "codeagent"
    assert script.exists(), "run `pip install -e .` so the console script exists"
    result = subprocess.run([str(script), "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "reconcile" in result.stdout and "solve" not in result.stdout
    sub = subprocess.run([str(script), "reconcile", "--help"], capture_output=True, text=True, timeout=60)
    assert sub.returncode == 0 and "--abandon" in sub.stdout


def test_exactly_the_real_docker_tests_carry_the_docker_marker():
    this = sys.modules[__name__]

    def marked(func):
        return any(m.name == "skipif" and m.kwargs.get("reason") == _DOCKER_REASON for m in getattr(func, "pytestmark", []))

    assert {n for n in dir(this) if n.startswith("test_") and marked(getattr(this, n))} == {
        "test_dry_run_is_invariant_over_real_git_docker_and_state",
        "test_deleted_repository_lock_beside_a_real_complete_run_never_reports_clean",
        "test_residual_blocks_then_normal_abandon_clears_it",
        "test_residual_without_a_lock_file_is_abandoned_and_the_lock_created",
        "test_forced_with_nothing_remaining_is_refused",
        "test_t36_shape_normal_refused_forced_records_and_nothing_is_touched",
        "test_partial_removal_leftover_normal_refused_forced_records",
        "test_real_complete_run_cannot_be_abandoned",
        "test_explicit_reconcile_of_the_real_t37_shape",
        "test_sigkill_publication_windows",
        "test_sigkill_with_stale_temp_alone_then_reconcile_reconciles_normally",
        "test_fifteen_stale_temps_do_not_refuse",
    }


def test_f3_real_subprocess_grammar_errors_never_echo_input(tmp_path):
    sentinel = "/Users/PRIVATE-REVIEW-SENTINEL/secret"
    for args in (["--repo", "/unused", "--dry-run", sentinel], ["--repo", "/unused", f"--{sentinel}"]):
        result = _cli(tmp_path / "state", *args)
        assert result.returncode == 2
        assert "PRIVATE-REVIEW-SENTINEL" not in result.stdout + result.stderr
        assert "usage: codeagent" in result.stderr
    bad = subprocess.run(
        [sys.executable, "-m", "codeagent.cli", sentinel], capture_output=True, text=True, timeout=60
    )
    assert bad.returncode == 2 and "PRIVATE-REVIEW-SENTINEL" not in bad.stdout + bad.stderr
    assert not (tmp_path / "state").exists()
