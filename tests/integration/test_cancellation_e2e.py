"""ADR 0005 Amendment 1 (ledger D3): real-signal, real-Docker cancellation
tests E1-E12 (D3 plan section 10.3).

Each row launches `tests/support/cancellation_driver.py` as its own process
group. The driver runs `run_lifecycle_aware()` inside
`cli.owned_cancellation()`; the test delivers real SIGINT to the driver's
process group (`os.killpg`) or real SIGTERM to its exact PID (`os.kill`).
Synchronization is by file gates only, and every wait has a deadline that
fails the test. These tests prove the mechanism around the internal
composition, not an operator path: `main()` has no run path in D3, and D6
must repeat them through `codeagent solve`.

Assertion set A (every row but E8): `CANCELLED`, every resource confirmed
absent, the projection `COMPLETE`, then an admission probe, an explicit
reconcile of the probe's own entry, and a clean final dry run. Assertion
set B (E8): a real cleanup-unconfirmed leftover, reconciled at the next
admission by ADR 0004 Amendment 19.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeagent import container_lifecycle as cl
from codeagent import lifecycle_store as ls
from tests.support.fixture_repo import real_fixture_repo
from tests.support.process_shims import pgid_absent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEADLINE = 300.0
BASELINE_BLOCKS = ("sh", "-c", "python3 -B -m unittest tests.test_worker || exec sleep 600")
VERIFICATION_BLOCKS = ("sh", "-c", "python3 -B -m unittest tests.test_worker && exec sleep 600")
PLAIN = ("python3", "-B", "-m", "unittest", "tests.test_worker")
_DEFAULT_STATE_ROOTS = (
    Path.home() / "Library" / "Application Support" / "CodeAgent",
    Path.home() / ".local" / "state" / "codeagent",
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if not _docker_available() and os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1":
    pytest.fail(
        "CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available — failing instead of skipping.",
        pytrace=False,
    )

pytestmark = pytest.mark.skipif(not _docker_available(), reason="requires a running local Docker daemon")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def _registered_worktrees(repo) -> set[str]:
    out = _git(repo, "worktree", "list", "--porcelain", "-z").stdout
    paths = {os.path.realpath(t[len("worktree "):]) for t in out.split("\0") if t.startswith("worktree ")}
    paths.discard(os.path.realpath(repo))
    return paths


def _codeagent_refs(repo) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname)", "refs/codeagent").stdout.split()


def _docker_ps() -> tuple[set[str], set[str]]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = [line.split("\t") for line in out.splitlines() if line]
    return {r[0] for r in rows}, {r[1] for r in rows}


def _wait(predicate, what, deadline=DEADLINE):
    end = time.monotonic() + deadline
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"deadline waiting for {what}")
        time.sleep(0.02)


def _traces(state: Path) -> set[Path]:
    return set(state.glob("repos/*/maintenance/*.jsonl"))


def _trace_outcomes(trace: Path) -> tuple[str, dict[str, str]]:
    trigger, outcomes = None, {}
    for line in trace.read_text().splitlines():
        event = json.loads(line)
        if event["event_type"] == "ReconciliationStarted":
            trigger = event.get("trigger")
        if event["event_type"] == "ReconciliationEntryRecorded":
            assert event["lifecycle_id"] not in outcomes, "one outcome per entry per pass"
            outcomes[event["lifecycle_id"]] = event["outcome"]
    return trigger, outcomes


def _run_dir(state: Path, lifecycle_id: str) -> Path:
    (path,) = state.glob(f"repos/*/runs/{lifecycle_id}")
    return path


def _projection(state: Path, lifecycle_id: str) -> dict:
    return json.loads((_run_dir(state, lifecycle_id) / "lifecycle.json").read_text())


def _leaf(state: Path, lifecycle_id: str) -> Path:
    repo_key = _run_dir(state, lifecycle_id).parent.parent.name
    return state / "worktrees" / repo_key / lifecycle_id


@pytest.fixture
def e(tmp_path, monkeypatch):
    """A real fixture repo and an isolated state root. The failure-path
    finalizer is registered before any driver launches; it runs only if
    the test body did not finish its own cleanup, never instead of an
    assertion, and reports what it had to remove."""
    defaults_before = {p: p.exists() for p in _DEFAULT_STATE_ROOTS}
    # The test process's own admission probes use the same isolated root.
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state"))
    with real_fixture_repo() as repo:
        ns = SimpleNamespace(
            repo=repo,
            state=tmp_path / "state",
            evidence=tmp_path / "evidence",
            gates=tmp_path / "gates",
            tmp=tmp_path,
            driver=None,
            pgids=[],
            container_ids=[],
            leases=[],
            clean=False,
        )
        ns.gates.mkdir()
        try:
            yield ns
        finally:
            removed = []
            if ns.driver is not None and ns.driver.poll() is None:
                os.killpg(ns.driver.pid, signal.SIGKILL)
                ns.driver.wait()
                removed.append("driver")
            for lease in ns.leases:
                try:
                    lease.close()
                except Exception:  # noqa: BLE001 - already-closed leases
                    pass
            if not ns.clean:
                for pgid in ns.pgids:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                        removed.append(f"pgid {pgid}")
                    except OSError:
                        pass
                ids = [d.name for d in ns.state.glob("repos/*/runs/*")]
                live_ids, live_names = _docker_ps()
                for lifecycle_id in ids:
                    for role in ("baseline", "verification"):
                        name = f"codeagent-{role}-{lifecycle_id}"
                        if name in live_names:
                            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                            removed.append(name)
                for cid in ns.container_ids:
                    if cid in live_ids:
                        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
                        removed.append(cid)
                for path in _registered_worktrees(repo):
                    _git(repo, "worktree", "remove", "--force", path, check=False)
                    removed.append("worktree")
                for ref in _codeagent_refs(repo):
                    _git(repo, "update-ref", "-d", ref, check=False)
                    removed.append(ref)
                if removed:
                    print(f"failure-path finalizer removed: {removed}", file=sys.stderr)
            for path, existed in defaults_before.items():
                assert existed or not path.exists(), "the default state root was created"


def _launch(e, *, scenario="none", verify_command=PLAIN, approval="approve", run_id="r-d3"):
    config = {
        "repo": str(e.repo),
        "evidence": str(e.evidence),
        "gates": str(e.gates),
        "scenario": scenario,
        "verify_command": list(verify_command),
        "approval": approval,
        "run_id": run_id,
    }
    config_path = e.tmp / "driver-config.json"
    config_path.write_text(json.dumps(config))
    env = {
        **os.environ,
        "CODEAGENT_STATE_DIR": str(e.state),
        "PYTHONPATH": os.pathsep.join([str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]),
    }
    e.driver = subprocess.Popen(
        [sys.executable, "-m", "tests.support.cancellation_driver", str(config_path)],
        cwd=PROJECT_ROOT,
        env=env,
        start_new_session=True,
        stdout=open(e.tmp / "driver.out", "w"),
        stderr=subprocess.STDOUT,
    )
    return e.driver


def _gate(e, name):
    _wait(lambda: (e.gates / name).exists() or (e.gates / "result.json").exists(), name)
    assert (e.gates / name).exists(), _driver_failure(e)
    return (e.gates / name).read_text()


def _driver_failure(e) -> str:
    out = (e.tmp / "driver.out").read_text() if (e.tmp / "driver.out").exists() else ""
    result = (e.gates / "result.json").read_text() if (e.gates / "result.json").exists() else ""
    return f"driver output:\n{out}\nresult:\n{result}"


def _ps_field(pid: int, field: str) -> str:
    return subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def _sync_running_attach(e, n: int) -> int:
    """E12: wait for the n-th attach client, synchronize on its container
    running, then inspect only that live client's identity."""
    info = json.loads(_gate(e, f"attach-{n}.json"))
    pid, container_id = info["pid"], info["container_id"]
    e.container_ids.append(container_id)

    def running():
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id], capture_output=True, text=True
        )
        return out.stdout.strip() == "true"

    _wait(running, "the container running")
    pgid = _ps_field(pid, "pgid")
    assert pgid, "the attach client was not alive at inspection"
    assert int(pgid) == pid, "the attach client must lead its own process group"
    if sys.platform.startswith("linux"):
        sid = int(_ps_field(pid, "sid"))
        assert sid == pid and sid != os.getsid(e.driver.pid)
    e.pgids.append(pid)
    return pid


def _finish(e) -> dict:
    _wait(lambda: e.driver.poll() is not None, "driver exit")
    assert (e.gates / "result.json").exists(), _driver_failure(e)
    result = json.loads((e.gates / "result.json").read_text())
    assert "exception" not in result, result.get("exception")
    assert e.driver.returncode == 0, _driver_failure(e)
    assert result["handlers_restored"] is True  # E11
    return result


def _assert_physically_absent(e, lifecycle_id: str, pgids=()):
    for pgid in pgids:
        pgid_absent(pgid)
    ids, names = _docker_ps()
    assert f"codeagent-baseline-{lifecycle_id}" not in names
    assert f"codeagent-verification-{lifecycle_id}" not in names
    assert not set(e.container_ids) & ids
    assert _registered_worktrees(e.repo) == set()
    admin = e.repo / ".git" / "worktrees"
    assert not admin.exists() or list(admin.iterdir()) == []
    assert not os.path.lexists(_leaf(e.state, lifecycle_id))
    assert _codeagent_refs(e.repo) == []


def _admission(e, run_id: str, expected: dict[str, str]) -> str:
    """One admission probe: exactly the expected outcome per pre-existing
    entry from its own new pre-run trace; its own new entry is returned.
    The lease is closed in a finally (closing does not terminalize it)."""
    before = _traces(e.state)
    lease = ls.prepare_lifecycle(str(e.repo), run_id=run_id)
    e.leases.append(lease)
    try:
        (trace,) = _traces(e.state) - before
        trigger, outcomes = _trace_outcomes(trace)
        assert trigger == "pre_run"
        assert outcomes == expected
        return lease.lifecycle_id
    finally:
        lease.close()


def _cli(e, *args) -> tuple[int, dict[str, str], str]:
    out = subprocess.run(
        [sys.executable, "-m", "codeagent.cli", "reconcile", "--repo", str(e.repo), *args],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "CODEAGENT_STATE_DIR": str(e.state)},
    )
    entries = {}
    for line in out.stdout.splitlines():
        if line.startswith("entry "):
            lifecycle_id, rest = line[len("entry "):].split(": ", 1)
            entries[lifecycle_id] = rest.split(" ", 1)[0]
    return out.returncode, entries, out.stdout


def _explicit_then_dry_run(e, expected_explicit: dict[str, str]) -> None:
    before = _traces(e.state)
    code, entries, out = _cli(e)
    assert code == 0, out
    assert entries == expected_explicit, out
    (trace,) = _traces(e.state) - before
    assert _trace_outcomes(trace) == ("explicit", expected_explicit)
    before = _traces(e.state)
    code, entries, out = _cli(e, "--dry-run")
    assert code == 0 and "result=CLEAN" in out, out
    assert set(entries.values()) == {"skipped_terminal"} and set(entries) == set(expected_explicit), out
    assert _traces(e.state) == before  # the dry run writes nothing


def _assert_set_a(e, result: dict, *, pgids=(), seeded: tuple[str, ...] = ()) -> None:
    assert result["terminal_reason"] == "cancelled" and result["error_code"] is None, result
    assert result["unconfirmed_stages"] == []
    lifecycle_id = result["lifecycle_id"]
    proj = _projection(e.state, lifecycle_id)
    assert proj["state"] == "COMPLETE"
    assert ls.is_projection_fully_absent_shape(ls._projection_from_dict(proj))
    _assert_physically_absent(e, lifecycle_id, pgids)
    terminal = {lifecycle_id: "skipped_terminal", **{d: "skipped_terminal" for d in seeded}}
    probe = _admission(e, "r-probe", terminal)
    _explicit_then_dry_run(e, {**terminal, probe: "reconciled"})
    e.clean = True


def _events(result, type_name):
    return [ev for ev in result["events"] if ev["type"] == type_name]


def _signal(e, how: str) -> None:
    if how == "SIGINT":
        os.killpg(e.driver.pid, signal.SIGINT)
    else:
        os.kill(e.driver.pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# E1-E12
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["SIGINT", "SIGTERM"], ids=["E1-group-SIGINT", "E2-pid-SIGTERM"])
def test_e1_e2_cancel_while_the_baseline_container_runs(e, how):
    _launch(e, verify_command=BASELINE_BLOCKS)
    pid = _sync_running_attach(e, 1)
    _signal(e, how)
    result = _finish(e)
    (baseline,) = _events(result, "BaselineRecorded")
    assert baseline == {"type": "BaselineRecorded", "outcome": "cancelled", "cleanup_status": "confirmed_absent"}
    assert not any(ev.get("outcome") == "test_failure" for ev in result["events"])
    assert result["iterations_used"] == 0
    assert result["candidates"] == [f"signal:{how}"]
    _assert_set_a(e, result, pgids=(pid,))


@pytest.mark.parametrize("how", ["SIGINT", "SIGTERM"], ids=["E3-group-SIGINT", "E4-pid-SIGTERM"])
def test_e3_e4_cancel_while_the_verification_container_runs(e, how):
    _launch(e, verify_command=VERIFICATION_BLOCKS)
    pid = _sync_running_attach(e, 2)
    _signal(e, how)
    result = _finish(e)
    (verification,) = _events(result, "VerificationCompleted")
    assert verification["outcome"] == "cancelled" and verification["cleanup_status"] == "confirmed_absent"
    assert "exit_code" not in verification
    assert result["iterations_used"] == 1
    assert ["checkpoint_ref", "removing"] in result["published"]
    assert ["checkpoint_ref", "absent"] in result["published"]
    _assert_set_a(e, result, pgids=(pid,))


@pytest.mark.parametrize("how", ["SIGINT", "SIGTERM"], ids=["E5-group-SIGINT", "E6-pid-SIGTERM"])
def test_e5_e6_cancel_during_the_approval_wait(e, how):
    _launch(e, approval="blocking")
    _gate(e, "ready-approval")
    _signal(e, how)
    result = _finish(e)
    assert _events(result, "ApprovalRecorded") == []
    assert not any(ev.get("tool") == "apply_patch" for ev in _events(result, "ToolRequested"))
    assert not any(kind == "checkpoint_ref" for kind, _ in result["published"])
    _assert_set_a(e, result)


def test_e7_serialized_repeated_signals_during_gated_cleanup(e):
    _launch(e, scenario="cleanup_gate", verify_command=BASELINE_BLOCKS)
    pid = _sync_running_attach(e, 1)
    os.killpg(e.driver.pid, signal.SIGINT)
    base = int(_gate(e, "ready-cleanup"))
    assert base == 1
    for k, sig in enumerate((signal.SIGINT, signal.SIGINT, signal.SIGINT, signal.SIGTERM), start=1):
        if sig is signal.SIGINT:
            os.killpg(e.driver.pid, sig)
        else:
            os.kill(e.driver.pid, sig)
        (e.gates / f"ack-request-{k}").write_text("")
        _wait(lambda: (e.gates / f"ack-{k}").exists() or (e.gates / "ack-error").exists(), f"ack-{k}")
        assert not (e.gates / "ack-error").exists(), (e.gates / "ack-error").read_text()
        assert e.driver.poll() is None, "no early exit"
    (e.gates / "release-cleanup").write_text("")
    result = _finish(e)
    assert result["requests_received"] == base + 4
    assert result["candidates"][0] == "signal:SIGINT"
    (baseline,) = _events(result, "BaselineRecorded")
    assert baseline["outcome"] == "cancelled" and baseline["cleanup_status"] == "confirmed_absent"
    _assert_set_a(e, result, pgids=(pid,))


def test_e8_cleanup_unconfirmed_leftover_is_reconciled_at_the_next_admission(e):
    _launch(e, scenario="cleanup_skip", verify_command=VERIFICATION_BLOCKS)
    pid = _sync_running_attach(e, 2)
    os.killpg(e.driver.pid, signal.SIGINT)
    result = _finish(e)
    assert result["terminal_reason"] == "unrecoverable_error"
    assert result["error_code"] == "lifecycle_cleanup_unconfirmed"
    original = result["lifecycle_id"]
    container_id = e.container_ids[-1]
    proj = _projection(e.state, original)
    assert proj["state"] == "CLEANING"
    assert proj["containers"]["verification"] == {"intent": "removing", "id": container_id}
    assert proj["worktree"]["intent"] == "present" and proj["checkpoint_ref"]["intent"] == "present"
    # The real container remains, proven owned by the four labels.
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
        capture_output=True, text=True, check=True,
    )
    labels = json.loads(inspected.stdout)
    state_root_id = proj["state_root_id"]
    assert cl.labels_match(labels, state_root_id=state_root_id, lifecycle_id=original,
                           role=cl.ContainerRole.VERIFICATION)
    assert len(_registered_worktrees(e.repo)) == 1 and os.path.isdir(_leaf(e.state, original))
    assert len(_codeagent_refs(e.repo)) == 1
    pgid_absent(pid)

    # Admission 2 reconciles the dead owner through Amendment 19's
    # verification family (container, then worktree, then ref).
    e2 = _admission(e, "r-admission-2", {original: "reconciled"})
    _assert_physically_absent(e, original)
    assert _projection(e.state, original)["state"] == "RECONCILED"
    # Admission 3 is unblocked and reconciles admission 2's own entry.
    e3 = _admission(e, "r-admission-3", {original: "skipped_terminal", e2: "reconciled"})
    _explicit_then_dry_run(e, {original: "skipped_terminal", e2: "skipped_terminal", e3: "reconciled"})
    e.clean = True


def test_e9_pending_cancellation_during_the_patch(e):
    _launch(e, scenario="patch_signal")
    result = _finish(e)
    assert _events(result, "PatchApplied") and _events(result, "CheckpointCreated")
    assert result["sites"][-1] == "C19"
    assert result["candidates"] == ["signal:SIGINT"]
    _assert_set_a(e, result)


def test_e10_signal_during_admission_reconciliation(e):
    seeded_lease = ls.prepare_lifecycle(str(e.repo), run_id="r-seeded-dead")
    seeded = seeded_lease.lifecycle_id
    seeded_lease.close()  # a dead PREPARING, all-absent entry with a free lock
    before = _traces(e.state)
    _launch(e, scenario="admission")
    _gate(e, "ready-reconcile")
    os.killpg(e.driver.pid, signal.SIGINT)
    _gate(e, "ack-signal")
    (e.gates / "release-reconcile").write_text("")
    result = _finish(e)
    (trace,) = _traces(e.state) - before
    assert _trace_outcomes(trace) == ("pre_run", {seeded: "reconciled"})
    # The new run's worktree was materialized and then disposed (J9).
    worktree = [intent for kind, intent in result["published"] if kind == "worktree"]
    assert worktree == ["creating", "present", "disposing", "absent"]
    assert result["sites"] == ["C1"]
    assert result["iterations_used"] == 0
    _assert_set_a(e, result, seeded=(seeded,))
