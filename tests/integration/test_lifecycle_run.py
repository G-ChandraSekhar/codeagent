"""Milestone 3 Slice 3C-4 (ADR 0004 Amendment 14): the internal lifecycle-
aware run composition, `codeagent._lifecycle_run.run_lifecycle_aware()`.

Test numbers (T1..T45, T17b) map to the 46 named specifications in the
slice plan; T46 (ADR 0004 Amendment 15) and T47 (Amendment 16) were added
later, and T34 was corrected by Amendment 16 from BLOCKED to RECONCILED. Most tests are Docker-free: an injected owner-state
`activate()` failure sends `run()` straight to `_terminate()` (real
evidence capture, real worktree disposal, no baseline), and
`DockerVerifier.__init__` makes no Docker call. Only the end-to-end and
crash-boundary tests (T30-T38, T47) and the T46 regression carry `requires_docker`; the exact set is
pinned by T41.

Every `LifecycleRunCleanupError` assertion also checks the message is
exactly the fixed template for its stages (no injected `SECRET` text, no
path). Every A/B test asserts `LifecycleLease.close` ran exactly once.
"""

from __future__ import annotations

import ast
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeagent import _lifecycle_run as lr
from codeagent import domain
from codeagent import lifecycle_store as ls
from codeagent import reconciliation as rc
from codeagent import state_root as sr
from codeagent import workspace as ws
from codeagent._lifecycle_fs import LifecycleFsError, LifecycleFsFailure
from codeagent.checkpoint_session import CheckpointIntent
from codeagent.controller import EventLog, PlanProposal, SystemClock
from codeagent.errors import ErrorCode
from codeagent.evidence import EvidenceSinkError
from codeagent.executor import DEFAULT_IMAGE
from codeagent.lifecycle_owner import OwnerStatePublicationError, OwnerStatePublicationFailure
from codeagent.patch import PatchOperation
from codeagent.state_locks import LockError, LockFailure
from codeagent.worktree_lifecycle import WorktreeIntent, WorktreePublicationError, WorktreePublicationFailure
from tests.support.fakes import FIXTURE_VERIFY_COMMAND, FakeApprovalProvider, MarkerGatedFakeModel
from tests.support.fixture_repo import real_fixture_repo

Stage = lr.CleanupStage
WP, WPUB, RES, LEASE = Stage.WORKTREE_PHYSICAL, Stage.WORKTREE_PUBLICATION, Stage.RESERVATION, Stage.LEASE

BUG_MARKER = "# BUG:"
BUGGY = (
    "    job.retry_count += 1\n"
    "    if job.idempotency_key in already_processed:\n"
    '        return "duplicate"\n'
    "    already_processed.add(job.idempotency_key)"
)
FIXED = (
    "    if job.idempotency_key in already_processed:\n"
    '        return "duplicate"\n'
    "    job.retry_count += 1\n"
    "    already_processed.add(job.idempotency_key)"
)
PLAN = PlanProposal(
    problem_hypothesis="idempotency key dropped on retry",
    proposed_file_paths=("jobs/worker.py",),
    verification_intent="python3 -B -m unittest tests.test_worker",
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
        "CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available — "
        "this environment is expected to guarantee Docker; failing instead of skipping.",
        pytrace=False,
    )

requires_docker = pytest.mark.skipif(not _docker_available(), reason="requires a running local Docker daemon")


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


def _run_dirs(state: Path) -> list[Path]:
    return sorted(state.glob("repos/*/runs/*"))


def _projection(run_dir: Path) -> dict:
    return json.loads((run_dir / "lifecycle.json").read_text())


def _only_run(state: Path) -> tuple[str, str, dict]:
    (run_dir,) = _run_dirs(state)
    return run_dir.parent.parent.name, run_dir.name, _projection(run_dir)


def _leaf(state: Path, repo_key: str, lifecycle_id: str) -> Path:
    return state / "worktrees" / repo_key / lifecycle_id


def _container_names() -> set[str]:
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real fixture repo, an isolated state root, and the shared spies.
    Teardown removes anything a test deliberately left behind (worktree
    registrations, checkpoint refs, deterministic containers), then asserts
    nothing remains."""
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    with real_fixture_repo() as repo:
        e = SimpleNamespace(
            repo=repo,
            state=state,
            evidence=tmp_path / "evidence",
            tmp=tmp_path,
            lease_closes=0,
            lease_fail=None,  # exception raised after the real close
            reservations=[],
            reservation_exit=None,  # callable(self, exc_type) after the real exit
            worktrees=[],
        )

        real_close = ls.LifecycleLease.close

        def close(self):
            e.lease_closes += 1
            real_close(self)
            if e.lease_fail is not None:
                exc, e.lease_fail = e.lease_fail, None
                raise exc

        monkeypatch.setattr(ls.LifecycleLease, "close", close)

        real_res_enter = sr._WorktreeLeafReservation.__enter__
        real_res_exit = sr._WorktreeLeafReservation.__exit__

        def res_enter(self):
            e.reservations.append(self)
            return real_res_enter(self)

        def res_exit(self, exc_type, exc, tb):
            real_res_exit(self, exc_type, exc, tb)
            if e.reservation_exit is not None:
                e.reservation_exit(self, exc_type)

        monkeypatch.setattr(sr._WorktreeLeafReservation, "__enter__", res_enter)
        monkeypatch.setattr(sr._WorktreeLeafReservation, "__exit__", res_exit)

        real_wt_enter = ws.GitWorktree.__enter__

        def wt_enter(self):
            e.worktrees.append(self)
            return real_wt_enter(self)

        monkeypatch.setattr(ws.GitWorktree, "__enter__", wt_enter)

        try:
            yield e
        finally:
            for path in _registered_worktrees(repo):
                _git(repo, "worktree", "remove", "--force", path, check=False)
            for ref in _codeagent_refs(repo):
                _git(repo, "update-ref", "-d", ref, check=False)
            ids = [d.name for d in _run_dirs(state)]
            if ids and _docker_available():
                for lifecycle_id in ids:
                    for role in ("baseline", "verification"):
                        subprocess.run(["docker", "rm", "-f", f"codeagent-{role}-{lifecycle_id}"], capture_output=True)
                leftovers = {n for n in _container_names() if any(n.endswith(i) for i in ids)}
                assert leftovers == set(), leftovers
            assert _registered_worktrees(repo) == set()
            assert _codeagent_refs(repo) == []


def _invoke(e, *, model=None, image=DEFAULT_IMAGE, event_log=None, evidence_root=None, run_id="r-3c4"):
    return lr.run_lifecycle_aware(
        e.repo,
        run_id=run_id,
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=evidence_root if evidence_root is not None else e.evidence,
        patch_operations=(PatchOperation("jobs/worker.py", BUGGY, FIXED),),
        model=model or MarkerGatedFakeModel(read_path="jobs/worker.py", marker=BUG_MARKER, plan=PLAN),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verify_command=FIXTURE_VERIFY_COMMAND,
        image=image,
        clock=SystemClock(),
        event_log=event_log,
    )


def _fail_activate(monkeypatch):
    def activate(self):
        raise OwnerStatePublicationError(OwnerStatePublicationFailure.NOT_INSTALLED, "injected")

    monkeypatch.setattr(ls.LifecycleOwnerStatePublisher, "activate", activate)


def _raise_in_body(monkeypatch, exc, before=None):
    """Raise `exc` while collaborators are built, i.e. after the worktree
    was entered (no `RunFinished` will exist)."""

    def reader(path):
        if before is not None:
            before()
        raise exc

    monkeypatch.setattr(lr, "WorktreeFileReader", reader)


def _noop_worktree_remove(monkeypatch, e):
    e.removes = 0
    real = ws._run

    def run(repo_path, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            e.removes += 1
            return None
        return real(repo_path, *args, **kwargs)

    monkeypatch.setattr(ws, "_run", run)


def _fail_worktree_publish(monkeypatch, intent):
    real = ls.LifecycleWorktreePublisher.publish

    def publish(self, transition):
        if transition.intent is intent:
            raise WorktreePublicationError(WorktreePublicationFailure.NOT_INSTALLED, "SECRET-publish")
        real(self, transition)

    monkeypatch.setattr(ls.LifecycleWorktreePublisher, "publish", publish)


def _count(monkeypatch, cls, name):
    calls = []
    real = getattr(cls, name)

    def wrapper(self, *a, **k):
        calls.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(cls, name, wrapper)
    return calls


def _no_containers(monkeypatch):
    """For reconciliation rows that genuinely involve no container (the
    `test_worktree_publication.py` precedent): only the reconciler's Docker
    listing is patched, so a Docker-free test can observe admission."""
    monkeypatch.setattr(rc, "_docker_ps_all_id_name_pairs", lambda: ({}, {}))


def _R(msg="SECRET-R"):
    return LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, msg)


def _L(msg="SECRET-L"):
    return ls.LifecycleStoreError(ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED, msg)


def _assert_sanitized(err, stages, e):
    assert err.failed_stages == stages
    assert str(err) == lr._MESSAGE_PREFIX + ", ".join(s.value for s in stages)
    assert "SECRET" not in str(err)
    assert str(e.tmp) not in str(err) and str(e.repo) not in str(err)


def _triples(err_or_failures):
    failures = getattr(err_or_failures, "failures", err_or_failures)
    return [(f.stage, f.exception, f.declared) for f in failures]


def _assert_same_triples(actual, expected):
    assert len(actual) == len(expected)
    for (s, x, d), (es, ex, ed) in zip(actual, expected):
        assert s is es and x is ex and d is ed


# ---------------------------------------------------------------------------
# A. Returned path (R1, R4) — Docker-free
# ---------------------------------------------------------------------------


def _scenario_disposal(e, monkeypatch):
    _noop_worktree_remove(monkeypatch, e)


def _scenario_latched(e, monkeypatch):
    _fail_worktree_publish(monkeypatch, WorktreeIntent.ABSENT)


def _scenario_reservation(e, monkeypatch):
    e.injected_r = _R()

    def inject(self, exc_type):
        if exc_type is None:
            raise e.injected_r

    e.reservation_exit = inject


def _scenario_both(e, monkeypatch):
    _scenario_reservation(e, monkeypatch)
    e.injected_l = _L()
    e.injected_l.__cause__ = e.inner = ValueError("inner")
    e.lease_fail = e.injected_l


def test_t1_controller_disposal_failure_is_never_retried(env, monkeypatch):
    """T1: R1 — `_terminate()`'s failed disposal is reported, never retried,
    and `GitWorktree.__exit__` is never called after `run()` returns."""
    _fail_activate(monkeypatch)
    _scenario_disposal(env, monkeypatch)
    disposes = _count(monkeypatch, ws.GitWorktree, "dispose")
    exits = _count(monkeypatch, ws.GitWorktree, "__exit__")

    result = _invoke(env)

    assert result.finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert len(disposes) == 1 and env.removes == 1
    assert exits == []
    assert env.lease_closes == 1
    assert result.release_confirmed
    (_, _, proj) = _only_run(env.state)
    assert proj["worktree"]["intent"] == "disposing"
    assert _registered_worktrees(env.repo)  # left exactly as _terminate() left it


def test_t2_latched_absent_failure_is_returned_not_reraised(env, monkeypatch):
    """T2: the latched `GitWorktreeLifecycleError` is never re-raised."""
    _fail_activate(monkeypatch)
    _scenario_latched(env, monkeypatch)
    exits = _count(monkeypatch, ws.GitWorktree, "__exit__")

    result = _invoke(env)

    assert result.finished.error.code is ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED
    assert exits == [] and env.lease_closes == 1
    (wt,) = env.worktrees
    assert isinstance(wt.lifecycle_error, ws.GitWorktreeLifecycleError)
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["worktree"]["intent"] == "disposing"
    assert not os.path.lexists(_leaf(env.state, repo_key, lifecycle_id))
    assert _registered_worktrees(env.repo) == set()


def test_t3_reservation_close_failure_is_returned(env, monkeypatch):
    """T3."""
    _fail_activate(monkeypatch)
    _scenario_reservation(env, monkeypatch)
    result = _invoke(env)
    assert result.reservation_release_error is env.injected_r
    assert result.lease_release_error is None
    assert result.unconfirmed_stages == (RES,) and not result.release_confirmed
    assert env.lease_closes == 1


def test_t4_reservation_and_lease_declared_failures_together(env, monkeypatch):
    """T4: both exact instances; each keeps its own `__cause__`."""
    _fail_activate(monkeypatch)
    _scenario_both(env, monkeypatch)
    result = _invoke(env)
    assert result.reservation_release_error is env.injected_r
    assert result.lease_release_error is env.injected_l
    assert env.injected_l.__cause__ is env.inner and env.injected_r.__cause__ is None
    assert result.unconfirmed_stages == (RES, LEASE)
    assert env.lease_closes == 1


@pytest.mark.parametrize(
    "scenario", [_scenario_disposal, _scenario_latched, _scenario_reservation, _scenario_both]
)
def test_t5_nothing_unrepresented_escapes_after_run_finished(env, monkeypatch, scenario):
    """T5: each T1-T4 shape returns normally, carrying the log's RunFinished."""
    _fail_activate(monkeypatch)
    scenario(env, monkeypatch)
    log = EventLog()
    result = _invoke(env, event_log=log)
    assert isinstance(result, lr.LifecycleRunResult)
    assert result.finished is log.events[-1]
    assert env.lease_closes == 1


def _matrix_exceptions():
    x = SimpleNamespace(R=_R(), L=_L(), U=RuntimeError("SECRET-U"), U2=ValueError("SECRET-U2"), B=KeyboardInterrupt())
    return x


# (id, reservation behaviour, lease behaviour, prepare, expected)
# expected: ("return", res_field, lease_field, stages)
#           ("reraise", exc_key)
#           ("error", cause_key, [(stage, exc_key, declared)], stages)
_R4_ROWS = [
    ("R/L", "R", "L", None, ("return", "R", "L", (RES, LEASE))),
    ("Rchain/L", "R", "L", "R_ctx_L", ("return", "R", None, (RES, LEASE))),
    ("U/ok", "U", None, None, ("error", "U", [(RES, "U", False)], (RES,))),
    ("U/L", "U", "L", None, ("error", "U", [(RES, "U", False), (LEASE, "L", True)], (RES, LEASE))),
    ("U/U2", "U", "U2", None, ("error", "U", [(RES, "U", False), (LEASE, "U2", False)], (RES, LEASE))),
    ("B/ok", "B", None, None, ("reraise", "B")),
    ("B/L", "B", "L", None, ("error", "B", [(RES, "B", False), (LEASE, "L", True)], (RES, LEASE))),
    ("B/U2", "B", "U2", None, ("error", "B", [(RES, "B", False), (LEASE, "U2", False)], (RES, LEASE))),
    ("ok/B", None, "B", None, ("reraise", "B")),
    ("R/U", "R", "U", None, ("error", "U", [(RES, "R", True), (LEASE, "U", False)], (RES, LEASE))),
    ("Rreach-B/B", "R", "B", "R_ctx_B", ("error", "B", [(RES, "R", True)], (RES, LEASE))),
    ("ok/U", None, "U", None, ("error", "U", [(LEASE, "U", False)], (LEASE,))),
]


@pytest.mark.parametrize("row", _R4_ROWS, ids=[r[0] for r in _R4_ROWS])
def test_t6_r4_combination_matrix(env, monkeypatch, row):
    """T6: every R4 combination, by identity, with exactly one lease close."""
    _, res_key, lease_key, prepare, expected = row
    x = _matrix_exceptions()
    if prepare == "R_ctx_L":
        x.R.__context__ = x.L
    elif prepare == "R_ctx_B":
        x.R.__context__ = x.B
    _fail_activate(monkeypatch)
    if res_key is not None:

        def inject(self, exc_type):
            if exc_type is None:
                raise getattr(x, res_key)

        env.reservation_exit = inject
    if lease_key is not None:
        env.lease_fail = getattr(x, lease_key)
    log = EventLog()

    kind = expected[0]
    if kind == "return":
        result = _invoke(env, event_log=log)
        _, res_field, lease_field, stages = expected
        assert result.finished is log.events[-1]
        assert result.reservation_release_error is (getattr(x, res_field) if res_field else None)
        assert result.lease_release_error is (getattr(x, lease_field) if lease_field else None)
        assert result.unconfirmed_stages == stages and result.release_confirmed is False
    elif kind == "reraise":
        with pytest.raises(BaseException) as excinfo:
            _invoke(env, event_log=log)
        assert excinfo.value is getattr(x, expected[1])
    else:
        with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
            _invoke(env, event_log=log)
        err = excinfo.value
        _, cause_key, records, stages = expected
        assert err.__cause__ is getattr(x, cause_key)
        assert err.original is None and err.finished is log.events[-1]
        _assert_same_triples(_triples(err), [(s, getattr(x, k), d) for s, k, d in records])
        _assert_sanitized(err, stages, env)
    assert env.lease_closes == 1


def test_t7_distinct_unexpected_failures_keep_their_real_stages(env, monkeypatch):
    """T7 (P1): two distinct unexpected exceptions, each attributed to the
    stage that raised it."""
    a, b = RuntimeError("A"), ValueError("B")
    _fail_activate(monkeypatch)

    def inject(self, exc_type):
        if exc_type is None:
            raise a

    env.reservation_exit = inject
    env.lease_fail = b
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    _assert_same_triples(_triples(err), [(RES, a, False), (LEASE, b, False)])
    assert err.failed_stages == (RES, LEASE) and err.__cause__ is a
    assert "A" not in str(err) and "B" not in str(err)
    _assert_sanitized(err, (RES, LEASE), env)
    assert env.lease_closes == 1


def test_t8_reservation_mode_worktree_owns_no_resource(env):
    """T8: R1's premise — skipping `GitWorktree.__exit__` leaks nothing
    because a reservation-mode worktree owns no descriptor or tempdir."""
    import tempfile

    lease = ls.prepare_lifecycle(str(env.repo), run_id="r-pin")
    try:
        with lease.state_root.reserve_worktree_leaf(lease.repo_key, lease.lifecycle_id) as reservation:
            worktree = ws.GitWorktree(env.repo, "r-pin", reservation=reservation)
            with worktree:
                assert worktree._tempdir is None
                for name, value in vars(worktree).items():
                    assert not isinstance(value, tempfile.TemporaryDirectory), name
                    assert not ("fd" in name.lower() and isinstance(value, int)), name
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# B. Raise path (R5) — Docker-free, body exception after the worktree is entered
# ---------------------------------------------------------------------------


def test_t9_body_plus_worktree_physical_failure(env, monkeypatch):
    """T9."""
    o = RuntimeError("SECRET-O")
    _raise_in_body(monkeypatch, o)
    _noop_worktree_remove(monkeypatch, env)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (wt,) = env.worktrees
    assert err.original is o and err.__cause__ is o and err.finished is None
    _assert_same_triples(_triples(err), [(WP, wt.cleanup_error, True)])
    assert isinstance(wt.cleanup_error, ws.GitWorktreeCleanupError)
    _assert_sanitized(err, (WP,), env)
    assert _only_run(env.state)[2]["worktree"]["intent"] == "disposing"
    assert env.lease_closes == 1


def test_t10_body_plus_latched_worktree_publication_failure(env, monkeypatch):
    """T10."""
    o = RuntimeError("SECRET-O")
    _raise_in_body(monkeypatch, o)
    _fail_worktree_publish(monkeypatch, WorktreeIntent.ABSENT)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (wt,) = env.worktrees
    _assert_same_triples(_triples(err), [(WPUB, wt.lifecycle_error, True)])
    _assert_sanitized(err, (WPUB,), env)
    assert env.lease_closes == 1


def test_t11_body_plus_reservation_close_failure(env, monkeypatch):
    """T11: a reservation failure exists only in the raised error, never in
    `lifecycle.json`."""
    o = RuntimeError("SECRET-O")
    r = _R("SECRET-reservation-only")
    _raise_in_body(monkeypatch, o)

    def inject(self, exc_type):
        if exc_type is not None:
            self.cleanup_error = r

    env.reservation_exit = inject
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (res,) = env.reservations
    assert res.cleanup_error is r
    _assert_same_triples(_triples(err), [(RES, r, True)])
    _assert_sanitized(err, (RES,), env)
    (run_dir,) = _run_dirs(env.state)
    assert "SECRET" not in (run_dir / "lifecycle.json").read_text()
    assert env.lease_closes == 1


def test_t12_body_plus_physical_reservation_and_lease_failures(env, monkeypatch):
    """T12: three records, stage order."""
    o = RuntimeError("SECRET-O")
    r, l_ = _R(), _L()
    _raise_in_body(monkeypatch, o)
    _noop_worktree_remove(monkeypatch, env)

    def inject(self, exc_type):
        self.cleanup_error = r

    env.reservation_exit = inject
    env.lease_fail = l_
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (wt,) = env.worktrees
    _assert_same_triples(_triples(err), [(WP, wt.cleanup_error, True), (RES, r, True), (LEASE, l_, True)])
    assert err.__cause__ is o
    _assert_sanitized(err, (WP, RES, LEASE), env)
    assert env.lease_closes == 1


def test_t13_body_plus_publication_and_lease_failures(env, monkeypatch):
    """T13."""
    o = RuntimeError("SECRET-O")
    l_ = _L()
    _raise_in_body(monkeypatch, o)
    _fail_worktree_publish(monkeypatch, WorktreeIntent.ABSENT)
    env.lease_fail = l_
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (wt,) = env.worktrees
    _assert_same_triples(_triples(err), [(WPUB, wt.lifecycle_error, True), (LEASE, l_, True)])
    _assert_sanitized(err, (WPUB, LEASE), env)
    assert env.lease_closes == 1


@pytest.mark.parametrize("make", [lambda: RuntimeError("SECRET-O"), KeyboardInterrupt], ids=["RuntimeError", "KeyboardInterrupt"])
def test_t14_all_stages_confirmed_reraises_exact_original(env, monkeypatch, make):
    """T14."""
    o = make()
    _raise_in_body(monkeypatch, o)
    with pytest.raises(BaseException) as excinfo:
        _invoke(env)
    assert excinfo.value is o
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["worktree"]["intent"] == "absent"
    assert not os.path.lexists(_leaf(env.state, repo_key, lifecycle_id))
    assert env.lease_closes == 1


def test_t15_keyboard_interrupt_plus_lease_failure_is_dominated(env, monkeypatch):
    """T15: cleanup dominance converts the interrupt; it stays the cause."""
    o = KeyboardInterrupt()
    l_ = _L()
    _raise_in_body(monkeypatch, o)
    env.lease_fail = l_
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    assert err.__cause__ is o and err.original is o
    _assert_same_triples(_triples(err), [(LEASE, l_, True)])
    _assert_sanitized(err, (LEASE,), env)
    assert env.lease_closes == 1


def test_t16_enter_time_publication_fault_is_not_double_reported(env, monkeypatch):
    """T16 (D, enter-time case 1): O is the latched `lifecycle_error`."""
    _fail_worktree_publish(monkeypatch, WorktreeIntent.PRESENT)
    with pytest.raises(ws.GitWorktreeLifecycleError) as excinfo:
        _invoke(env)
    (wt,) = env.worktrees
    assert excinfo.value is wt.lifecycle_error
    assert env.lease_closes == 1
    # `present` failure retains the materialized worktree (Amendment 11).
    assert _registered_worktrees(env.repo)


def test_t17_enter_time_cleanup_error_not_double_reported_but_reservation_is(env, monkeypatch):
    """T17 (D, enter-time case 2): O is the recorded `GitWorktreeCleanupError`;
    a reservation failure alongside it is still reported."""
    injected_wt = ws.GitWorktreeCleanupError("SECRET-enter-cleanup")
    real_cleanup = ws.GitWorktree._cleanup_failed_reservation_worktree

    def cleanup(self, path):
        assert real_cleanup(self, path) is None  # real exact cleanup really ran
        return injected_wt

    monkeypatch.setattr(ws.GitWorktree, "_verify_post_git_reservation", lambda self, path: False)
    monkeypatch.setattr(ws.GitWorktree, "_cleanup_failed_reservation_worktree", cleanup)
    r = _R()

    def inject(self, exc_type):
        self.cleanup_error = r

    env.reservation_exit = inject
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    (wt,) = env.worktrees
    assert err.original is injected_wt and wt.cleanup_error is injected_wt
    _assert_same_triples(_triples(err), [(RES, r, True)])
    _assert_sanitized(err, (RES,), env)
    assert env.lease_closes == 1


def test_t17b_unexpected_worktree_exit_failure_is_worktree_physical(env, monkeypatch):
    """T17b (H2): an unexpected exception escaping `GitWorktree.__exit__`
    is attributed to `WORKTREE_PHYSICAL`."""
    o = RuntimeError("SECRET-O")
    w = RuntimeError("SECRET-W")
    _raise_in_body(monkeypatch, o)
    real_exit = ws.GitWorktree.__exit__

    def exit_(self, *args):
        real_exit(self, *args)
        raise w

    monkeypatch.setattr(ws.GitWorktree, "__exit__", exit_)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    _assert_same_triples(_triples(err), [(WP, w, False)])
    _assert_sanitized(err, (WP,), env)
    assert env.lease_closes == 1


def test_t18_unexpected_reservation_and_lease_failures_on_raise_path(env, monkeypatch):
    """T18."""
    o = RuntimeError("SECRET-O")
    u, v = RuntimeError("SECRET-U"), ValueError("SECRET-V")
    _raise_in_body(monkeypatch, o)

    def inject(self, exc_type):
        raise u

    env.reservation_exit = inject
    env.lease_fail = v
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    assert err.__cause__ is o
    _assert_same_triples(_triples(err), [(RES, u, False), (LEASE, v, False)])
    _assert_sanitized(err, (RES, LEASE), env)
    assert env.lease_closes == 1


def test_t19_stages_run_outside_any_handler(env, monkeypatch):
    """T19 (H1): stage exceptions raised during cleanup carry no implicit
    `__context__` — in particular, never the original exception."""
    o = RuntimeError("SECRET-O")
    u, l_ = RuntimeError("SECRET-U"), _L()
    _raise_in_body(monkeypatch, o)

    def inject(self, exc_type):
        raise u

    env.reservation_exit = inject
    env.lease_fail = l_
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    for failure in excinfo.value.failures:
        assert failure.exception.__context__ is None
        assert failure.exception is not o


# ---------------------------------------------------------------------------
# C. Rule D graph cases (P2) — Docker-free
# ---------------------------------------------------------------------------


def _inject_fields(monkeypatch, env, *, worktree_value=None, reservation_value=None):
    """Set recorded fields *during* each stage call (so R8 attributes them)."""
    if worktree_value is not None:
        real_exit = ws.GitWorktree.__exit__

        def exit_(self, *args):
            real_exit(self, *args)
            self.cleanup_error = worktree_value

        monkeypatch.setattr(ws.GitWorktree, "__exit__", exit_)
    if reservation_value is not None:

        def inject(self, exc_type):
            self.cleanup_error = reservation_value

        env.reservation_exit = inject


def test_t20_self_cycle(env, monkeypatch):
    """T20."""
    w = ws.GitWorktreeCleanupError("SECRET-W")
    w.__cause__ = w
    _raise_in_body(monkeypatch, RuntimeError("SECRET-O"))
    _inject_fields(monkeypatch, env, worktree_value=w)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    _assert_same_triples(_triples(excinfo.value), [(WP, w, True)])
    _assert_sanitized(excinfo.value, (WP,), env)


def test_t21_two_exception_cycle(env, monkeypatch):
    """T21: `b` is reachable from the earlier retained `a`, so it is not
    reported again — but its stage is still unconfirmed (J)."""
    a = ws.GitWorktreeCleanupError("SECRET-A")
    b = _R()
    a.__cause__ = b
    b.__context__ = a
    _raise_in_body(monkeypatch, RuntimeError("SECRET-O"))
    _inject_fields(monkeypatch, env, worktree_value=a, reservation_value=b)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    _assert_same_triples(_triples(excinfo.value), [(WP, a, True)])
    _assert_sanitized(excinfo.value, (WP, RES), env)


def test_t22_same_instance_in_two_fields(env, monkeypatch):
    """T22: reported once, at the first stage."""
    x = ws.GitWorktreeCleanupError("SECRET-X")
    _raise_in_body(monkeypatch, RuntimeError("SECRET-O"))
    _inject_fields(monkeypatch, env, worktree_value=x, reservation_value=x)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    _assert_same_triples(_triples(excinfo.value), [(WP, x, True)])
    _assert_sanitized(excinfo.value, (WP, RES), env)


@pytest.mark.parametrize("direction", ["w_reaches_r", "r_reaches_w"])
def test_t23_reachable_through_an_earlier_retained_candidate(env, monkeypatch, direction):
    """T23: excluded only when reachable *from* an earlier retained record."""
    w = ws.GitWorktreeCleanupError("SECRET-W")
    r = _R()
    if direction == "w_reaches_r":
        w.__cause__ = r
        expected = [(WP, w, True)]
    else:
        r.__cause__ = w
        expected = [(WP, w, True), (RES, r, True)]
    _raise_in_body(monkeypatch, RuntimeError("SECRET-O"))
    _inject_fields(monkeypatch, env, worktree_value=w, reservation_value=r)
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    _assert_same_triples(_triples(excinfo.value), expected)
    _assert_sanitized(excinfo.value, (WP, RES), env)


def test_t24_pre_existing_field_reachable_from_original_is_o_owned(env, monkeypatch):
    """T24 (R8): a field unchanged by identity from the pre-call snapshot and
    reachable from O is O-owned, so the exact O propagates."""
    c = _R()
    o = RuntimeError("SECRET-O")
    o.__context__ = c

    def before():
        (res,) = env.reservations
        res.cleanup_error = c  # assigned before the RESERVATION stage runs

    _raise_in_body(monkeypatch, o, before=before)
    with pytest.raises(RuntimeError) as excinfo:
        _invoke(env)
    assert excinfo.value is o
    (res,) = env.reservations
    assert res.cleanup_error is c  # unchanged by the stage call
    assert env.lease_closes == 1


def test_t25_reachable_traversal_is_cycle_safe(env):
    """T25: direct `_reachable` checks — termination, exact id set."""
    s = RuntimeError("s")
    s.__cause__ = s
    out: set[int] = set()
    lr._reachable(s, out)
    assert out == {id(s)}

    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__context__ = b, a
    out = set()
    lr._reachable(a, out)
    assert out == {id(a), id(b)}

    top, left, right, bottom = (RuntimeError(n) for n in ("top", "left", "right", "bottom"))
    top.__cause__, top.__context__ = left, right
    left.__cause__ = bottom
    right.__context__ = bottom
    out = set()
    lr._reachable(top, out)
    assert out == {id(top), id(left), id(right), id(bottom)}

    out = set()
    lr._reachable(None, out)
    assert out == set()


# ---------------------------------------------------------------------------
# G. Rev 5 additions (Q1, Q2, J) — Docker-free
# ---------------------------------------------------------------------------


def test_t42_two_distinct_failures_at_one_stage_name_it_once(env, monkeypatch):
    """T42 (Q1): every retained record is kept; the stage is named once."""
    o = RuntimeError("SECRET-O")
    r = _R()
    u = RuntimeError("SECRET-U")
    _raise_in_body(monkeypatch, o)

    def inject(self, exc_type):
        self.cleanup_error = r
        raise u

    env.reservation_exit = inject
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    _assert_same_triples(_triples(err), [(RES, u, False), (RES, r, True)])
    assert err.failed_stages == (RES,)
    assert str(err) == lr._MESSAGE_PREFIX + "reservation"
    _assert_sanitized(err, (RES,), env)
    assert env.lease_closes == 1


@pytest.mark.parametrize("direction", ["R_ctx_L", "L_ctx_R"])
def test_t43_reachable_declared_chain_across_release_stages(env, monkeypatch, direction):
    """T43 (Q2 + J): the returned fields come from post-D records — the
    first stage wins and the excluded duplicate never reappears — while
    confirmation still reports both stages."""
    r, l_ = _R(), _L()
    if direction == "R_ctx_L":
        r.__context__ = l_
    else:
        l_.__context__ = r
    _fail_activate(monkeypatch)

    def inject(self, exc_type):
        if exc_type is None:
            raise r

    env.reservation_exit = inject
    env.lease_fail = l_
    result = _invoke(env)
    assert result.reservation_release_error is r
    assert result.lease_release_error is (None if direction == "R_ctx_L" else l_)
    assert result.unconfirmed_stages == (RES, LEASE)
    assert result.release_confirmed is False
    assert env.lease_closes == 1


def test_t44_deduped_unexpected_failure_still_drives_r4(env, monkeypatch):
    """T44 (J): an unexpected `BaseException` excluded by D is never
    swallowed — R8, not D, selects the outcome."""
    b = KeyboardInterrupt()
    r = _R()
    r.__context__ = b
    _fail_activate(monkeypatch)

    def inject(self, exc_type):
        if exc_type is None:
            raise r

    env.reservation_exit = inject
    env.lease_fail = b
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    assert err.__cause__ is b
    _assert_same_triples(_triples(err), [(RES, r, True)])
    _assert_sanitized(err, (RES, LEASE), env)
    assert env.lease_closes == 1


def test_t45_field_set_during_the_call_but_reachable_from_o_is_attributed(env, monkeypatch):
    """T45 (J on R5; contrast with T24): the stage changed the field by
    identity, so it is unconfirmed even though D reports nothing for it."""
    c = _R()
    o = RuntimeError("SECRET-O")
    o.__context__ = c
    _raise_in_body(monkeypatch, o)

    def inject(self, exc_type):
        self.cleanup_error = c

    env.reservation_exit = inject
    with pytest.raises(lr.LifecycleRunCleanupError) as excinfo:
        _invoke(env)
    err = excinfo.value
    assert err.__cause__ is o
    assert err.failures == ()
    _assert_sanitized(err, (RES,), env)
    assert env.lease_closes == 1


# ---------------------------------------------------------------------------
# D. Evidence root (F3) — Docker-free
# ---------------------------------------------------------------------------


def _assert_refused_before_reservation(env, monkeypatch, evidence_root):
    with pytest.raises(EvidenceSinkError):
        _invoke(env, evidence_root=evidence_root)
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert not os.path.lexists(_leaf(env.state, repo_key, lifecycle_id))
    assert env.reservations == []
    assert proj["state"] == "PREPARING" and ls.is_projection_fully_absent_shape(ls._projection_from_dict(proj))
    assert env.lease_closes == 1
    # Locks were released: a fresh admission succeeds and reconciles the
    # PREPARING entry (no container is involved in this row).
    _no_containers(monkeypatch)
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    assert _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)["state"] == "RECONCILED"


@pytest.mark.parametrize("where", ["equal", "descendant", "ancestor"])
def test_t26_evidence_root_vs_source(env, monkeypatch, where):
    """T26."""
    root = {"equal": env.repo, "descendant": env.repo / "sub", "ancestor": env.repo.parent}[where]
    _assert_refused_before_reservation(env, monkeypatch, root)


@pytest.mark.parametrize("where", ["equal", "runs", "worktrees-deep", "ancestor"])
def test_t27_evidence_root_vs_state_root_and_worktree(env, monkeypatch, where):
    """T27: the state root check is a superset of the worktree check."""
    root = {
        "equal": env.state,
        "runs": env.state / "repos",
        "worktrees-deep": env.state / "worktrees" / "k" / "id" / "x",
        "ancestor": env.tmp,
    }[where]
    _assert_refused_before_reservation(env, monkeypatch, root)


def test_t28_evidence_root_with_symlink_component_is_refused(env, monkeypatch):
    """T28."""
    (env.tmp / "real").mkdir()
    (env.tmp / "link").symlink_to(env.tmp / "real")
    _assert_refused_before_reservation(env, monkeypatch, env.tmp / "link" / "ev")


@pytest.mark.parametrize("kind", ["nonexistent-nested", "existing-sibling"])
def test_t29_unrelated_evidence_roots_are_accepted(env, monkeypatch, kind):
    """T29."""
    root = env.tmp / "a" / "b" / "c" if kind == "nonexistent-nested" else env.tmp / "ev"
    if kind == "existing-sibling":
        root.mkdir(mode=0o700)
    _fail_activate(monkeypatch)
    result = _invoke(env, evidence_root=root)
    _, lifecycle_id, _ = _only_run(env.state)
    assert (root / f"{lifecycle_id}.evidence").is_file()
    assert result.release_confirmed


# ---------------------------------------------------------------------------
# E. End to end and crash boundaries — real Docker
# ---------------------------------------------------------------------------


def _assert_fully_clean(env, repo_key, lifecycle_id):
    assert not os.path.lexists(_leaf(env.state, repo_key, lifecycle_id))
    assert _registered_worktrees(env.repo) == set()
    assert _codeagent_refs(env.repo) == []
    names = _container_names()
    assert f"codeagent-baseline-{lifecycle_id}" not in names
    assert f"codeagent-verification-{lifecycle_id}" not in names


@requires_docker
def test_t30_real_happy_path(env):
    """T30."""
    result = _invoke(env)
    assert result.finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    assert result.finished.error is None and result.release_confirmed
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["state"] == "COMPLETE"
    assert ls.is_projection_fully_absent_shape(ls._projection_from_dict(proj))
    _assert_fully_clean(env, repo_key, lifecycle_id)
    assert (env.evidence / f"{lifecycle_id}.evidence").is_file()
    assert env.lease_closes == 1


@requires_docker
def test_t31_second_run_skips_the_first_as_terminal(env):
    """T31."""
    _invoke(env, run_id="r-first")
    (first,) = _run_dirs(env.state)
    result = _invoke(env, run_id="r-second")
    assert result.finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
    recorded = []
    for trace in env.state.glob("repos/*/maintenance/*.jsonl"):
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            if event["event_type"] == "ReconciliationEntryRecorded" and event["lifecycle_id"] == first.name:
                recorded.append(event["outcome"])
    assert recorded == ["skipped_terminal"]


class _RaisingModel:
    def request_read_path(self):
        raise RuntimeError("SECRET-model")

    def propose_plan(self, read_result):  # pragma: no cover - never reached
        raise AssertionError


@requires_docker
def test_t32_model_error_after_baseline_completes_cleanly(env):
    """T32."""
    result = _invoke(env, model=_RaisingModel())
    assert result.finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["state"] == "COMPLETE"
    _assert_fully_clean(env, repo_key, lifecycle_id)


@requires_docker
def test_t33_disposal_failure_without_ref_is_reconciled_next_run(env, monkeypatch):
    """T33: T1's shape, real Docker reconciliation (A13)."""
    _fail_activate(monkeypatch)
    _noop_worktree_remove(monkeypatch, env)
    _invoke(env)
    repo_key, lifecycle_id, _ = _only_run(env.state)
    # The reconciler uses its own bounded Git runner, not `workspace._run`,
    # so the no-op patch above does not affect the real removal below.
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    dead = _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)
    assert dead["state"] == "RECONCILED" and dead["worktree"]["intent"] == "absent"
    _assert_fully_clean(env, repo_key, lifecycle_id)


def _blocked(repo):
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="r-after-crash")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED


@requires_docker
def test_t34_keyboard_interrupt_after_ref_present_is_reconciled_next_run(env, monkeypatch):
    """T34: the ADR 0005 interruption shape. Before ADR 0004 Amendment 16
    the dead entry blocked the next run (a non-absent checkpoint ref was
    never reconciled); its worktree is disposed on the raise path and both
    containers are absent, so the checkpoint-ref row now recovers it.
    Disclosed correction: this test previously pinned `_blocked`."""
    real = ls.LifecycleCheckpointRefPublisher.publish
    ki = KeyboardInterrupt()
    armed = {"on": True}

    def publish(self, transition):
        real(self, transition)
        if armed["on"] and transition.intent is CheckpointIntent.PRESENT:
            armed["on"] = False
            raise ki

    monkeypatch.setattr(ls.LifecycleCheckpointRefPublisher, "publish", publish)
    with pytest.raises(KeyboardInterrupt) as excinfo:
        _invoke(env)
    assert excinfo.value is ki
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["checkpoint_ref"]["intent"] == "present"
    assert _codeagent_refs(env.repo) == [f"refs/codeagent/runs/{lifecycle_id}/checkpoint"]
    assert proj["worktree"]["intent"] == "absent"  # disposed on the raise path
    monkeypatch.setattr(ls.LifecycleCheckpointRefPublisher, "publish", real)
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    dead = _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)
    assert dead["state"] == "RECONCILED"
    assert dead["checkpoint_ref"]["intent"] == "absent"
    _assert_fully_clean(env, repo_key, lifecycle_id)


def _child_run_and_sigkill(repo, state_dir, evidence, point):
    """Module-level (picklable) child: runs the real composition and
    self-SIGKILLs right after the durable write named by `point`."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_c
    from codeagent import _lifecycle_run as lr_c
    from codeagent.container_lifecycle import ContainerIntent, ContainerRole

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    if point == "worktree_absent":
        real = ls_c.LifecycleWorktreePublisher.publish

        def publish(self, transition):
            real(self, transition)
            if transition.intent is WorktreeIntent.ABSENT:
                die()

        ls_c.LifecycleWorktreePublisher.publish = publish
    elif point == "worktree_present":
        real = ls_c.LifecycleWorktreePublisher.publish

        def publish(self, transition):
            real(self, transition)
            if transition.intent is WorktreeIntent.PRESENT:
                die()

        ls_c.LifecycleWorktreePublisher.publish = publish
    elif point == "baseline_present":
        real = ls_c.LifecycleContainerPublisher.publish

        def publish(self, *, role, intent, id):
            real(self, role=role, intent=intent, id=id)
            if role is ContainerRole.BASELINE and intent is ContainerIntent.PRESENT:
                die()

        ls_c.LifecycleContainerPublisher.publish = publish
    else:  # ref_present
        real = ls_c.LifecycleCheckpointRefPublisher.publish

        def publish(self, transition):
            real(self, transition)
            if transition.intent is CheckpointIntent.PRESENT:
                die()

        ls_c.LifecycleCheckpointRefPublisher.publish = publish
    lr_c.run_lifecycle_aware(
        repo,
        run_id=f"r-kill-{point}",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=evidence,
        patch_operations=(PatchOperation("jobs/worker.py", BUGGY, FIXED),),
        model=MarkerGatedFakeModel(read_path="jobs/worker.py", marker=BUG_MARKER, plan=PLAN),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verify_command=FIXTURE_VERIFY_COMMAND,
    )
    die()  # unreachable


def _crash(env, point):
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child_run_and_sigkill, args=(str(env.repo), str(env.state), str(env.evidence), point))
    proc.start()
    proc.join(timeout=180)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("child did not self-SIGKILL within the timeout")
    assert proc.exitcode == -signal.SIGKILL
    return _only_run(env.state)


@requires_docker
def test_t35_sigkill_after_worktree_present_is_reconciled(env):
    """T35."""
    repo_key, lifecycle_id, proj = _crash(env, "worktree_present")
    assert proj["worktree"]["intent"] == "present"
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    dead = _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)
    assert dead["state"] == "RECONCILED"
    _assert_fully_clean(env, repo_key, lifecycle_id)


@requires_docker
def test_t36_sigkill_after_baseline_present_blocks(env):
    """T36: worktree plus container — no reconciliation row exists."""
    repo_key, lifecycle_id, proj = _crash(env, "baseline_present")
    assert proj["containers"]["baseline"]["intent"] == "present" and proj["worktree"]["intent"] == "present"
    assert f"codeagent-baseline-{lifecycle_id}" in _container_names()
    _blocked(env.repo)


@requires_docker
def test_t37_sigkill_after_ref_present_is_reconciled(env):
    """T37 (ADR 0004 Amendment 17; previously blocked): the owner dies with a
    materialized worktree and a present checkpoint ref, containers absent.
    The next run's reconciliation removes the worktree, then the ref, in one
    pass and one cycle."""
    repo_key, lifecycle_id, proj = _crash(env, "ref_present")
    assert proj["checkpoint_ref"]["intent"] == "present" and proj["worktree"]["intent"] == "present"
    assert proj["containers"]["baseline"]["intent"] == "absent"
    assert proj["containers"]["verification"]["intent"] == "absent"
    assert _codeagent_refs(env.repo) == [f"refs/codeagent/runs/{lifecycle_id}/checkpoint"]
    assert _registered_worktrees(env.repo)
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    dead = _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)
    assert dead["state"] == "RECONCILED"
    assert dead["worktree"]["intent"] == "absent"
    assert dead["checkpoint_ref"]["intent"] == "absent"
    assert dead["reconciliation"]["attempts_total"] == 1
    assert not (env.repo / ".git" / "worktrees" / lifecycle_id).exists()
    _assert_fully_clean(env, repo_key, lifecycle_id)


@requires_docker
def test_t47_sigkill_in_teardown_after_worktree_absent_is_reconciled(env):
    """T47 (ADR 0004 Amendment 16): the owner dies inside `_terminate()`
    after the durable worktree `absent` and before the checkpoint-ref
    delete. The dead entry (CLEANING, worktree and containers absent, ref
    present) is recovered by the checkpoint-ref row on the next run."""
    repo_key, lifecycle_id, proj = _crash(env, "worktree_absent")
    assert proj["state"] == "CLEANING"
    assert proj["worktree"]["intent"] == "absent"
    assert proj["containers"]["baseline"]["intent"] == "absent"
    assert proj["containers"]["verification"]["intent"] == "absent"
    assert proj["checkpoint_ref"]["intent"] == "present"
    assert _codeagent_refs(env.repo) == [f"refs/codeagent/runs/{lifecycle_id}/checkpoint"]
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    dead = _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)
    assert dead["state"] == "RECONCILED"
    assert dead["checkpoint_ref"]["intent"] == "absent"
    _assert_fully_clean(env, repo_key, lifecycle_id)


@requires_docker
def test_t38_construction_failure_disposes_and_is_reconciled(env):
    """T38: an undigested image fails after the worktree is entered."""
    with pytest.raises(ValueError):
        _invoke(env, image="python:3.12-slim")
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["worktree"]["intent"] == "absent" and proj["state"] == "PREPARING"
    assert env.lease_closes == 1
    ls.prepare_lifecycle(str(env.repo), run_id="r-after").close()
    assert _projection(env.state / "repos" / repo_key / "runs" / lifecycle_id)["state"] == "RECONCILED"
    _assert_fully_clean(env, repo_key, lifecycle_id)


@requires_docker
def test_t46_post_patch_verification_reads_the_private_0700_leaf(env, monkeypatch):
    """T46 (ADR 0004 Amendment 15 regression): the composition's mount is
    the reserved worktree leaf, mode 0700. Post-patch verification must run
    as the effective host identity and actually import and pass the fixture
    tests from that mount. Run 37138458659 failed exactly here on Linux under
    the former fixed `--user 1000:1000`; the identity check also makes this
    load-bearing on hosts (such as macOS Docker Desktop) that mask mount
    permissions."""
    identity = f"{os.geteuid()}:{os.getegid()}"
    modes = []
    real = ls.LifecycleWorktreePublisher.publish

    def publish(self, transition):
        real(self, transition)
        if transition.intent is WorktreeIntent.PRESENT:
            (run_dir,) = _run_dirs(env.state)
            leaf = _leaf(env.state, run_dir.parent.parent.name, run_dir.name)
            modes.append(os.stat(leaf).st_mode & 0o777)

    monkeypatch.setattr(ls.LifecycleWorktreePublisher, "publish", publish)
    result = lr.run_lifecycle_aware(
        env.repo,
        run_id="r-0700",
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=env.evidence,
        patch_operations=(PatchOperation("jobs/worker.py", BUGGY, FIXED),),
        model=MarkerGatedFakeModel(read_path="jobs/worker.py", marker=BUG_MARKER, plan=PLAN),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verify_command=(
            "sh",
            "-c",
            f'test "$(id -u):$(id -g)" = "{identity}" && exec python3 -B -m unittest tests.test_worker',
        ),
        clock=SystemClock(),
        max_repair_iterations=0,
    )
    assert modes == [0o700]
    assert result.finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED, result.finished
    assert result.finished.error is None and result.release_confirmed
    repo_key, lifecycle_id, proj = _only_run(env.state)
    assert proj["state"] == "COMPLETE"
    _assert_fully_clean(env, repo_key, lifecycle_id)


# ---------------------------------------------------------------------------
# F. Concurrency and scope pins
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "codeagent"


def test_t39_cleanup_runs_outside_every_handler():
    """T39 (H1): the only handlers are `_attempt`'s (return-only) and
    `run_lifecycle_aware`'s single binding; no `finally`, no `ExceptionGroup`."""
    tree = ast.parse((_SRC / "_lifecycle_run.py").read_text())
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    attempt_handlers = {id(h) for h in ast.walk(functions["_attempt"]) if isinstance(h, ast.ExceptHandler)}
    run_handlers = [h for h in ast.walk(functions["run_lifecycle_aware"]) if isinstance(h, ast.ExceptHandler)]
    assert len(run_handlers) == 1
    (handler,) = run_handlers
    assert isinstance(handler.type, ast.Name) and handler.type.id == "BaseException"
    assert len(handler.body) == 1 and isinstance(handler.body[0], ast.Assign)
    assert [t.id for t in handler.body[0].targets] == ["original"]
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node is not handler:
            assert id(node) in attempt_handlers
            assert all(isinstance(stmt, ast.Return) for stmt in node.body)
        if isinstance(node, ast.Try):
            assert node.finalbody == []
        if isinstance(node, ast.Name):
            assert node.id not in ("ExceptionGroup", "BaseExceptionGroup")


def _child_hold_lease(repo, state_dir, ready, release):
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as ls_c

    lease = ls_c.prepare_lifecycle(repo, run_id="r-holder")
    ready.set()
    release.wait(timeout=60)
    lease.close()


def test_t40_concurrent_run_is_refused_before_any_resource(env):
    """T40: T-E1 evidence for this internal path only."""
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_child_hold_lease, args=(str(env.repo), str(env.state), ready, release))
    proc.start()
    try:
        assert ready.wait(timeout=30)
        with pytest.raises(LockError) as excinfo:
            _invoke(env)
        assert excinfo.value.reason is LockFailure.BUSY
        assert len(_run_dirs(env.state)) == 1  # only the holder's
        assert env.reservations == [] and env.worktrees == []
        assert _codeagent_refs(env.repo) == []
    finally:
        release.set()
        proc.join(timeout=30)
    assert proc.exitcode == 0


def _calls_named(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                yield node


def test_t41_scope_and_marker_pins():
    """T41: no bundled module imports the internal composition; only it calls
    `prepare_lifecycle(`/`create_shared_lifecycle_publishers(`; and exactly
    the T30-T38, T46 and T47 tests require Docker."""
    importers, callers = [], {"prepare_lifecycle": set(), "create_shared_lifecycle_publishers": set()}
    for module in sorted(_SRC.glob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                (node.module or "").endswith("_lifecycle_run") or any(a.name == "_lifecycle_run" for a in node.names)
            ):
                importers.append(module.name)
            if isinstance(node, ast.Import) and any(a.name.endswith("_lifecycle_run") for a in node.names):
                importers.append(module.name)
        for name in callers:
            if any(True for _ in _calls_named(tree, name)):
                callers[name].add(module.name)
    assert importers == []
    assert callers == {"prepare_lifecycle": {"_lifecycle_run.py"}, "create_shared_lifecycle_publishers": {"_lifecycle_run.py"}}

    this = sys.modules[__name__]

    def marked(func):
        return any(
            m.name == "skipif" and m.kwargs.get("reason") == "requires a running local Docker daemon"
            for m in getattr(func, "pytestmark", [])
        )

    docker_tests = {n for n in dir(this) if n.startswith("test_") and marked(getattr(this, n))}
    assert docker_tests == {
        n for n in dir(this) if n.startswith(tuple(f"test_t{i}_" for i in (*range(30, 39), 46, 47)))
    }
