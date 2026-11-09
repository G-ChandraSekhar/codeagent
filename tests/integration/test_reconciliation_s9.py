"""ADR 0004 Amendment 20 section 9 with a real owner: the composed run
recreates, and the owner process is really SIGKILLed at the B5, B6, B7 and
B9 boundaries (section 8). The next admission's reconciliation, with real Git
and a real Docker listing, recovers B6/B9 through the section 9 row; B5 and
both B7 variants stay blocked, and forced acknowledgement is their only exit.
The evidence root is never touched."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from codeagent import lifecycle_store as ls
from codeagent import maintenance as mt
from codeagent import reconciliation as rc
from tests.support.fixture_repo import real_fixture_repo


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if not _docker_available() and os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1":
    pytest.fail("CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available", pytrace=False)

requires_docker = pytest.mark.skipif(not _docker_available(), reason="requires a running local Docker daemon")
pytestmark = requires_docker  # every admission pass performs a real `docker ps -a`


def _owner_and_sigkill(repo, state_dir, evidence, point):
    """Module-level (picklable) child: the composed run of Step 7 (a
    post-write patch failure triggers recreation) that self-SIGKILLs at the
    second incarnation's `point`."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import subprocess as sp

    from codeagent import _lifecycle_run as lr_c
    from codeagent import domain, events
    from codeagent import lifecycle_store as ls_c
    from codeagent import workspace as ws_c
    from codeagent.controller import RunController
    from codeagent.worktree_lifecycle import WorktreeIntent
    from tests.integration.test_worktree_recreation import _ContaminatingApplier
    from tests.support.fakes import FakeApprovalProvider, FakeVerifier, MarkerGatedFakeModel, make_plan

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    RunController._recreation_can_continue = lambda self: True
    lr_c.DockerVerifier = lambda *a, **k: FakeVerifier((events.VerificationOutcome.PASSED,))
    lr_c.GitPatchApplier = _ContaminatingApplier
    counts = {"creating": 0, "add": 0, "checkout": 0}

    real_publish = ls_c.LifecycleWorktreePublisher.publish

    def publish(self, transition):
        second = False
        if transition.intent is WorktreeIntent.CREATING:
            counts["creating"] += 1
            second = counts["creating"] == 2
        if second and point == "b5":
            die()  # after R1 (the leaf exists), before `creating(C)` is durable
        real_publish(self, transition)
        if second and point == "b6":
            die()  # after `creating(C)`, before `add` writes anything

    ls_c.LifecycleWorktreePublisher.publish = publish

    real_run = ws_c._run

    def run(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            counts["add"] += 1
            result = real_run(cwd, *args, **kwargs)
            if counts["add"] == 2 and point in ("b7a", "b7b"):
                # A deterministic stand-in for Git's partial state (Amendment 20 section 11).
                leaf = Path(args[-2])
                common = Path(sp.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                     capture_output=True, text=True, check=True).stdout.strip())
                (admin,) = [p for p in (common / "worktrees").iterdir() if p.name.startswith(leaf.name)]
                (admin / "gitdir").unlink() if point == "b7a" else (leaf / ".git").unlink()
                die()
            return result
        if args[:1] == ("checkout",):
            counts["checkout"] += 1
            if counts["checkout"] == 2 and point == "b9":
                return sp.CompletedProcess(args, 1, "", "")
        return real_run(cwd, *args, **kwargs)

    ws_c._run = run
    if point == "after_c30":
        # D5-B: the recreation completes (RECREATED), C30 passes and the run
        # returns to EXPLORE; die at the next pass's first model request.
        real_respond = MarkerGatedFakeModel.respond

        def respond(self, context):
            if context.pass_index >= 1:
                die()
            return real_respond(self, context)

        MarkerGatedFakeModel.respond = respond
    if point == "b9":
        # The enter-time cleanup has run and the reservation closed (the leaf
        # is gone); die before the confirmed `creating -> absent`.
        lr_c.observe_worktree_absent = lambda **k: die()

    lr_c.run_lifecycle_aware(
        Path(repo), run_id=f"r-s9-{point}", task_statement="fix", approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=Path(evidence),
        model=MarkerGatedFakeModel(read_path="jobs/worker.py", marker="# BUG:", plan=make_plan()),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
    )
    die()  # unreachable


def _assert_admitted(repo, run_id: str) -> None:
    """Only for scenarios whose contract requires the dead entry to reconcile
    so the next admission is admitted: a reconciliation refusal is that
    contract's failure. Any other error propagates unchanged."""
    try:
        lease = ls.prepare_lifecycle(str(repo), run_id=run_id)
    except ls.LifecycleStoreError as exc:
        if exc.reason is not ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED:
            raise
        raise AssertionError("admission was refused: reconciliation blocked") from None
    lease.close()


def _git(repo, *args) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


@pytest.fixture
def owner(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    with real_fixture_repo() as repo:
        yield type("Owner", (), {"repo": repo, "state": state, "evidence": tmp_path / "evidence"})


def _crash(o, point):
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_owner_and_sigkill, args=(str(o.repo), str(o.state), str(o.evidence), point))
    proc.start()
    proc.join(timeout=180)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("owner child did not self-SIGKILL within the timeout")
    assert proc.exitcode == -signal.SIGKILL
    (run_dir,) = list((o.state / "repos").glob("*/runs/*"))
    o.run_dir, o.lifecycle_id, o.repo_key = run_dir, run_dir.name, run_dir.parent.parent.name
    o.leaf = o.state / "worktrees" / o.repo_key / o.lifecycle_id
    o.ref = f"refs/codeagent/runs/{o.lifecycle_id}/checkpoint"
    common = Path(_git(o.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    o.admin_root = common / "worktrees"
    return ls_json(o)


def ls_json(o):
    import json

    return json.loads((o.run_dir / ls.LIFECYCLE_JSON_FILENAME).read_text())


def _tree(*roots):
    snap = []
    for root in roots:
        if not os.path.lexists(root):
            snap.append((str(root), None))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            st = os.lstat(dirpath)
            snap.append((dirpath, st.st_ino, st.st_mode))
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                snap.append((p, os.lstat(p).st_mode, Path(p).read_bytes()))
    return snap


def _admin(o):
    return sorted(p for p in o.admin_root.glob(f"{o.lifecycle_id}*")) if o.admin_root.is_dir() else []


def _snapshot(o):
    """Everything a blocked pass must leave byte-identical, plus the evidence root."""
    return (
        (o.run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes(),
        _tree(o.leaf, *_admin(o)),
        _git(o.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/codeagent"),
        _git(o.repo, "worktree", "list", "--porcelain"),
        _tree(o.evidence),
    )


def _maintenance_files(o):
    return sorted((o.state / "repos" / o.repo_key / rc.MAINTENANCE_DIRNAME).glob("*"))


def _assert_fully_clean(o, evidence_before):
    dead = ls_json(o)
    assert dead["state"] == "RECONCILED"
    assert dead["reconciliation"]["attempts_total"] == 1
    assert dead["worktree"] == {"intent": "absent", "expected_head": None}
    assert dead["checkpoint_ref"]["intent"] == "absent"
    assert not os.path.lexists(o.leaf) and _admin(o) == []
    assert _git(o.repo, "for-each-ref", "refs/codeagent") == ""
    assert os.path.realpath(o.leaf) not in _git(o.repo, "worktree", "list", "--porcelain")
    assert _tree(o.evidence) == evidence_before  # reconciliation never touches it


def _assert_crashed_shape(proj, worktree):
    assert proj["state"] == "ACTIVE"
    assert proj["worktree"]["intent"] == worktree
    assert proj["checkpoint_ref"]["intent"] == "present"
    assert proj["containers"]["baseline"]["intent"] == proj["containers"]["verification"]["intent"] == "absent"


def _dry_run_writes_nothing(o, expected_entry):
    before, files = _snapshot(o), _maintenance_files(o)
    report = mt.run_reconcile(str(o.repo), dry_run=True)
    assert report.result is mt.ReconcileResult.BLOCKED and report.maintenance_id is None
    (entry,) = [e for e in report.entries if e.lifecycle_id == o.lifecycle_id]
    assert entry.outcome is expected_entry
    assert _snapshot(o) == before and _maintenance_files(o) == files


@pytest.mark.parametrize("via", ["admission", "explicit"])
@pytest.mark.parametrize("point, leaf_left", [("b6", True), ("b9", False)])
def test_s9_owner_sigkill_b6_b9_is_reconciled_by_the_section_9_row(owner, point, leaf_left, via):
    o = owner
    proj = _crash(o, point)
    _assert_crashed_shape(proj, "creating")
    assert proj["worktree"]["expected_head"] == proj["checkpoint_ref"]["accepted_sha"]  # creating(C) beside present(C)
    assert o.leaf.is_dir() is leaf_left and (o.leaf.exists() or not leaf_left)
    if leaf_left:
        assert os.listdir(o.leaf) == [] and (os.lstat(o.leaf).st_mode & 0o777) == 0o700
    assert _admin(o) == [] and os.path.realpath(o.leaf) not in _git(o.repo, "worktree", "list", "--porcelain")
    evidence = _tree(o.evidence)
    assert any(".recreation-01." in str(p[0]) for p in evidence)  # the series member was published before recreation
    _dry_run_writes_nothing(o, rc.PlanEntryOutcome.PENDING)
    if via == "admission":
        _assert_admitted(o.repo, "r-after")
    else:
        report = mt.run_reconcile(str(o.repo), dry_run=False)
        assert report.result is mt.ReconcileResult.CLEAN
        (entry,) = [e for e in report.entries if e.lifecycle_id == o.lifecycle_id]
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert entry.checkpoint_ref_gate_observation == "candidate_accepted"
        assert entry.worktree_leaf_outcome == ("removed" if leaf_left else "already_absent")
    _assert_fully_clean(o, evidence)


@pytest.mark.parametrize(
    "point, worktree",
    [("b5", "absent"), ("b7a", "creating"), ("b7b", "creating")],
)
def test_s9_owner_sigkill_b5_b7_stays_blocked_until_forced_acknowledgement(owner, point, worktree):
    o = owner
    proj = _crash(o, point)
    _assert_crashed_shape(proj, worktree)
    if point == "b5":
        assert o.leaf.is_dir() and os.listdir(o.leaf) == [] and _admin(o) == []
    else:
        assert len(_admin(o)) == 1
        listed = os.path.realpath(o.leaf) in _git(o.repo, "worktree", "list", "--porcelain")
        assert listed is (point == "b7b")  # (a) admin-present but unlisted; (b) listed, no `.git` file
        assert (o.leaf / ".git").exists() is (point == "b7a")
    left = _snapshot(o)

    _dry_run_writes_nothing(o, rc.PlanEntryOutcome.PENDING)
    caught = None
    try:
        ls.prepare_lifecycle(str(o.repo), run_id="r-blocked")
    except ls.LifecycleStoreError as exc:
        caught = exc
    assert caught is not None and caught.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED
    assert _snapshot(o)[1:] == left[1:]  # the leaf, admin entry, refs, registration, evidence
    assert ls_json(o)["worktree"] == proj["worktree"] and ls_json(o)["checkpoint_ref"] == proj["checkpoint_ref"]

    normal = mt.run_abandon(str(o.repo), o.lifecycle_id, acknowledge_unresolved=False, reason=None)
    assert normal.outcome is mt.AbandonOutcome.REFUSED
    forced = mt.run_abandon(str(o.repo), o.lifecycle_id, acknowledge_unresolved=True, reason="s9 residual")
    assert forced.outcome is mt.AbandonOutcome.RECORDED_UNRESOLVED
    lease = ls.prepare_lifecycle(str(o.repo), run_id="r-after")
    try:
        assert [a.lifecycle_id for a in lease.unresolved_acknowledged] == [o.lifecycle_id]
    finally:
        lease.close()
    assert _snapshot(o)[1:] == left[1:]  # nothing was repaired


def test_s9_owner_sigkill_after_a_recreated_continuation_is_reconciled(owner):
    """ADR 0004 Amendment 21 §21.10: an owner SIGKILLed after a RECREATED
    continuation (C30 passed, the next pass begun) leaves n+1 `present(C)`
    beside the ref `present(C)`; the next admission reconciles it through
    Amendment 17's row. The recreation artifact stays in the evidence root."""
    o = owner
    proj = _crash(o, "after_c30")
    _assert_crashed_shape(proj, "present")
    assert proj["worktree"]["expected_head"] == proj["checkpoint_ref"]["accepted_sha"]
    assert o.leaf.is_dir() and os.path.realpath(o.leaf) in _git(o.repo, "worktree", "list", "--porcelain")
    assert (o.evidence / f"{o.lifecycle_id}.recreation-01.evidence").exists()
    evidence_before = _tree(o.evidence)
    _assert_admitted(o.repo, "r-after-c30")
    _assert_fully_clean(o, evidence_before)
