"""ADR 0004 Amendment 20, D5/C-1 Step 7: the composition-owned workspace
recreator, against real Git, a real lease, the real shared publishers and
evidence sink, and real `GitWorktree` incarnations. No Docker: the composed
runs below replace only the verifier.

Since D5-B (ADR 0004 Amendment 21), `RunController._recreation_can_continue()`
is the production capability result, so the composed runs below recreate and
continue without any test seam.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeagent import _lifecycle_run as lr
from codeagent import domain, events
from codeagent import lifecycle_store as ls
from codeagent import state_root as sr
from codeagent import workspace as ws
from codeagent import worktree_absence as wa
from codeagent._lifecycle_fs import LifecycleFsError, LifecycleFsFailure
from codeagent.checkpoint_ref import CheckpointRef, CheckpointRefError, CheckpointRefFailure, MutationOutcome, RefObservation
from codeagent.checkpoint_session import CheckpointIntent, CheckpointSession
from codeagent.container_lifecycle import ContainerIntent
from codeagent.controller import EventLog, PatchResult, RunController
from codeagent.errors import ErrorCode, OperationalError
from codeagent.evidence import EvidenceReceipt, FilesystemEvidenceSink, parse_artifact_strict
from codeagent.workspace import GitWorktree, GitWorktreeCleanupError
from codeagent.worktree_lifecycle import WorktreeIntent, WorktreePublicationError, WorktreePublicationFailure
from tests.support.fakes import FakeApprovalProvider, FakeVerifier, MarkerGatedFakeModel, make_plan
from tests.support.fixture_repo import real_fixture_repo


def _code(finished):
    """The terminal error code; a missing error is an explicit assertion failure."""
    assert finished.error is not None, f"no error on {finished.terminal_reason}"
    return finished.error.code

O = events.RecreationOutcome
_IDENTITY = ["-c", "user.name=CodeAgent Test", "-c", "user.email=codeagent-test@example.invalid"]


def _git(repo, *args, check=True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *_IDENTITY, *args], capture_output=True, text=True, check=check, env=env
    ).stdout.strip()


def _registered(repo) -> set[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    paths = {os.path.realpath(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")}
    return paths - {os.path.realpath(repo)}


def _admin_names(lease) -> list[str]:
    admin = Path(lease.identity.canonical_common_dir) / "worktrees"
    return sorted(p.name for p in admin.iterdir()) if admin.exists() else []


# ---------------------------------------------------------------------------
# Harness: incarnation 1 is present(H0); one accepted commit C; dirty edits.
# ---------------------------------------------------------------------------


class Harness(SimpleNamespace):
    def recreator(self, **overrides):
        kwargs = dict(
            lease=self.lease, publishers=self.publishers, sink=self.sink, session=self.session,
            checkpoint_ref=self.ref, source=self.source, run_id="r-rec", worktree=self.worktree,
            reservation=self.reservation, max_recreations=3,
        )
        kwargs.update(overrides)
        rec = lr._CompositionRecreator(**kwargs)
        self.rec = rec
        return rec

    def installed(self):
        return self.writer.refresh()


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state"))
    with real_fixture_repo() as repo:
        lease = ls.prepare_lifecycle(str(repo), run_id="r-rec")
        writer, initial = lease.open_projection_writer()
        publishers = ls.create_shared_lifecycle_publishers(writer, initial)
        publishers.owner_publisher.activate()
        source = Path(initial.source_repo_path)
        reservation = lease.state_root.reserve_worktree_leaf(
            lease.repo_key, lease.lifecycle_id, canonical_common_dir=lease.identity.canonical_common_dir
        )
        reservation.__enter__()
        worktree = GitWorktree(source, "r-rec", reservation=reservation, worktree_publisher=publishers.worktree_publisher)
        path = worktree.__enter__()
        ref = CheckpointRef(source, lease.lifecycle_id)
        session = CheckpointSession(ref, transition_publisher=publishers.checkpoint_ref_publisher)
        h0 = worktree.initial_commit
        session.establish(h0)
        (path / "jobs" / "worker.py").write_text((path / "jobs" / "worker.py").read_text() + "# accepted\n")
        _git(path, "commit", "-qam", "accepted")
        c = _git(path, "rev-parse", "HEAD")
        session.advance(c)
        (path / "jobs" / "worker.py").write_text((path / "jobs" / "worker.py").read_text() + "# contaminating\n")
        sink = FilesystemEvidenceSink(tmp_path / "evidence")
        harness = Harness(
            repo=repo, tmp=tmp_path, lease=lease, writer=writer, publishers=publishers, source=source,
            reservation=reservation, worktree=worktree, ref=ref, session=session, sink=sink, h0=h0, c=c,
            leaf=Path(reservation.path), rec=None,
        )
        try:
            yield harness
        finally:
            _teardown(harness)


def _teardown(h) -> None:
    rec = h.rec
    worktree = rec.worktree if rec is not None else h.worktree
    entered = rec.worktree_entered if rec is not None else True
    reservation = rec.reservation if rec is not None else h.reservation
    # Teardown contains every failure, including a BaseException a test
    # injected, so it can never abort pytest's later finalizers (monkeypatch
    # undo) and leak a patch into another test.
    if worktree is not None and entered and worktree.disposition == "active" and worktree.lifecycle_error is None:
        try:
            worktree.dispose()
        except BaseException:  # noqa: BLE001 - forced below
            pass
    for path in list(_registered(h.repo)):
        _git(h.repo, "worktree", "remove", "--force", path, check=False)
    if reservation is not None:
        try:
            reservation.__exit__(None, None, None)
        except BaseException:  # noqa: BLE001 - teardown only
            pass
    admin = Path(h.lease.identity.canonical_common_dir) / "worktrees"
    if admin.exists():
        shutil.rmtree(admin)
    if os.path.lexists(h.leaf):
        shutil.rmtree(h.leaf) if h.leaf.is_dir() else os.unlink(h.leaf)
    _git(h.repo, "update-ref", "-d", f"refs/codeagent/runs/{h.lease.lifecycle_id}/checkpoint", check=False)
    h.lease.close()
    assert _registered(h.repo) == set()
    assert _git(h.repo, "for-each-ref", "refs/codeagent") == ""


def _record_publications(h, order):
    publisher = h.publishers.worktree_publisher
    real = publisher.publish
    installed = []

    def publish(transition):
        real(transition)
        record = h.installed().worktree
        installed.append((record.intent, record.expected_head))
        order.append(f"publish:{transition.intent.value}")

    publisher.publish = publish
    return installed


def _spy_sink(h, order, receipt=None, raises=None):
    real = h.sink.capture
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        order.append("evidence")
        if raises is not None:
            raise raises
        return receipt if receipt is not None else real(**kwargs)

    h.sink.capture = capture
    return calls


def _finished_event(result, sequence=1):
    """Every result must be a legal WorkspaceRecreationFinished."""
    return events.WorkspaceRecreationFinished(
        run_id="r", sequence=0, timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        state=domain.RunState.EXECUTE, iteration=0, recreation_sequence=sequence, outcome=result.outcome,
        artifact_id=result.artifact_id, sha256_payload=result.sha256_payload, bytes_written=result.bytes_written,
        evidence_complete=result.evidence_complete, error=result.error,
    )


def _checked(fn):
    """Call `fn`; an escaped ordinary exception is an explicit assertion
    failure, never an incidental test error."""
    caught = None
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - converted into an explicit assertion below
        caught = exc
    assert caught is None, f"unexpected {type(caught).__name__}"


def _begin(rec, h):
    return _checked(lambda: rec.begin(trigger_code=ErrorCode.PATCH_APPLICATION_FAILED, checkpoint_id=h.c))


def _recreate(rec, k):
    return _checked(lambda: rec.recreate(k))


# ---------------------------------------------------------------------------
# begin(): Q2-Q6
# ---------------------------------------------------------------------------


def _snapshot(h, rec):
    return (h.installed(), rec.worktree, rec.reservation, rec.worktree_entered, rec._minted, rec._pending,
            tuple(rec.earlier_reservation_failures), h.worktree.disposition, h.reservation.consumed_and_open,
            sorted(os.listdir(h.leaf)), _admin_names(h.lease))


def test_begin_mints_one_only_after_every_gate(h) -> None:
    order: list = []
    calls = _spy_sink(h, order)
    rec = h.recreator()
    before = _snapshot(h, rec)
    assert _begin(rec, h) == 1
    assert rec._pending == (1, h.c) and rec._minted == 1
    assert calls == [] and order == []
    after = _snapshot(h, rec)
    assert after[:4] == before[:4] and after[6:] == before[6:]


def _q3(field, value):
    def mutate(projection):
        return dataclasses.replace(projection, **{field: value})
    return mutate


_C_ABSENT = ls.ContainerAttribution(intent=ContainerIntent.ABSENT, id=None)


def _refusals():
    return {
        "q2-intent": lambda h, rec, mp: mp.setattr(h.session, "_transition", dataclasses.replace(h.session._transition, intent=CheckpointIntent.ADVANCING, expected_old_sha=h.c, proposed_new_sha=h.h0)),
        "q2-mismatch": "mismatch",
        "q3-state": lambda h, rec, mp: _proxy_projection(h, rec, mp, _q3("state", ls.LifecycleState.CLEANING)),
        "q3-failure": lambda h, rec, mp: _proxy_projection(h, rec, mp, _q3("failure", ls.FailureDetail(phase="p", detail="d"))),
        "q3-baseline": lambda h, rec, mp: _proxy_projection(h, rec, mp, _q3("baseline", ls.ContainerAttribution(intent=ContainerIntent.CREATING, id=None))),
        "q3-verification": lambda h, rec, mp: _proxy_projection(h, rec, mp, _q3("verification", ls.ContainerAttribution(intent=ContainerIntent.CREATING, id=None))),
        "q3-ref-intent": lambda h, rec, mp: _proxy_projection(h, rec, mp, lambda p: dataclasses.replace(p, checkpoint_ref=dataclasses.replace(p.checkpoint_ref, intent=CheckpointIntent.ABSENT, accepted_sha=None))),
        "q3-ref-sha": lambda h, rec, mp: _proxy_projection(h, rec, mp, lambda p: dataclasses.replace(p, checkpoint_ref=dataclasses.replace(p.checkpoint_ref, accepted_sha=h.h0))),
        "q3-worktree-intent": lambda h, rec, mp: _proxy_projection(h, rec, mp, lambda p: dataclasses.replace(p, worktree=dataclasses.replace(p.worktree, intent=WorktreeIntent.CREATING))),
        "q3-worktree-head": lambda h, rec, mp: _proxy_projection(h, rec, mp, lambda p: dataclasses.replace(p, worktree=dataclasses.replace(p.worktree, expected_head=h.c))),
        "q4-preserved": lambda h, rec, mp: mp.setattr(h.worktree, "_disposition", "preserved"),
        "q4-latched": lambda h, rec, mp: mp.setattr(h.worktree, "lifecycle_error", object()),
        "q4-no-path": lambda h, rec, mp: mp.setattr(h.worktree, "path", None),
        "q4-reservation-closed": lambda h, rec, mp: mp.setattr(h.reservation, "_descriptors_closed", True),
        "q4-not-entered": lambda h, rec, mp: setattr(rec, "worktree_entered", False),
        "q5-observe-fails": lambda h, rec, mp: mp.setattr(h.ref, "observe", lambda: (_ for _ in ()).throw(CheckpointRefError(CheckpointRefFailure.OBSERVATION_FAILED, "x", outcome=MutationOutcome.UNKNOWN))),
        "q5-other-oid": lambda h, rec, mp: mp.setattr(h.ref, "observe", lambda: RefObservation(present=True, oid=h.h0)),
        "q5-absent": lambda h, rec, mp: mp.setattr(h.ref, "observe", lambda: RefObservation(present=False)),
        "q6-budget-zero": "budget0",
        "q6-format-bound": "bound",
        "q6-smaller-budget": "budget2",
    }


def _proxy_projection(h, rec, mp, mutate):
    class Proxy:
        def __init__(self, real):
            self._real = real

        @property
        def current(self):
            return mutate(self._real.current)

        def __getattr__(self, name):
            return getattr(self._real, name)

    mp.setattr(rec, "_publisher", Proxy(h.publishers.worktree_publisher))


@pytest.mark.parametrize("case", list(_refusals()))
def test_every_q2_q6_refusal_returns_none_and_mutates_nothing(h, monkeypatch, case) -> None:
    order: list = []
    calls = _spy_sink(h, order)
    action = _refusals()[case]
    rec = h.recreator(max_recreations=0 if action == "budget0" else (99 if action == "bound" else (2 if action == "budget2" else 3)))
    if action == "bound":
        rec._minted = 99
    elif action == "budget2":
        rec._minted = 2
    elif callable(action):
        action(h, rec, monkeypatch)
    minted = rec._minted
    reserve_calls = []
    monkeypatch.setattr(h.lease.state_root, "reserve_worktree_leaf", lambda *a, **k: reserve_calls.append(a))
    before = _snapshot(h, rec)
    checkpoint = "f" * 40 if action == "mismatch" else h.c
    assert rec.begin(trigger_code=ErrorCode.PATCH_APPLICATION_FAILED, checkpoint_id=checkpoint) is None
    assert rec._minted == minted and rec._pending is None
    assert calls == [] and order == [] and reserve_calls == []
    assert _snapshot(h, rec) == before


def test_q6_allows_the_last_budgeted_and_last_format_sequence(h) -> None:
    rec = h.recreator(max_recreations=99)
    rec._minted = 98
    assert _begin(rec, h) == 99
    rec._pending = None
    assert _begin(rec, h) is None
    small = h.recreator(max_recreations=2)
    small._minted = 1
    assert _begin(small, h) == 2


def _raised(fn) -> BaseException | None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the type is asserted by the caller
        return exc
    return None


def _admit(repo, run_id) -> None:
    """The next admission must succeed; a store refusal (for example a blocked
    reconciliation) is an explicit assertion failure, anything else propagates."""
    try:
        lease = ls.prepare_lifecycle(str(repo), run_id=run_id)
    except ls.LifecycleStoreError as exc:
        raise AssertionError(f"the next admission was refused: {exc.reason}") from exc
    lease.close()


def test_begin_refuses_a_second_mint_while_one_is_pending(h) -> None:
    rec = h.recreator()
    assert _begin(rec, h) == 1
    assert isinstance(_raised(lambda: rec.begin(trigger_code=ErrorCode.PATCH_APPLICATION_FAILED, checkpoint_id=h.c)), RuntimeError)
    assert rec._minted == 1


@pytest.mark.parametrize("sequence", [2, 0, True, 1.0, "1"])
def test_recreate_refuses_a_sequence_that_was_not_just_minted(h, sequence) -> None:
    order: list = []
    calls = _spy_sink(h, order)
    rec = h.recreator()
    assert _begin(rec, h) == 1
    assert isinstance(_raised(lambda: rec.recreate(sequence)), RuntimeError)
    assert rec._pending == (1, h.c) and calls == []


def test_recreate_refuses_replay(h) -> None:
    rec = h.recreator()
    _begin(rec, h)
    assert _recreate(rec, 1).outcome is O.RECREATED
    assert isinstance(_raised(lambda: rec.recreate(1)), RuntimeError)


# ---------------------------------------------------------------------------
# Full real recreation (RG1)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _held_leaf_identity(reservation):
    """A duplicate of the reservation's leaf descriptor, closed exactly once on
    every exit path. Holding it keeps the old incarnation's inode allocated, so
    `samestat` against the recreated path cannot be fooled by a filesystem that
    reuses a freed inode number (Linux CI run 42130282872)."""
    fd = os.dup(reservation.fileno())
    try:
        yield fd
    finally:
        os.close(fd)


def _fd_closed(fd) -> bool:
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def test_held_leaf_identity_survives_the_reservation_and_closes_on_failure(h) -> None:
    with pytest.raises(AssertionError, match="later assertion"):
        with _held_leaf_identity(h.reservation) as fd:
            held = fd
            assert os.path.samestat(os.fstat(fd), os.stat(h.leaf, follow_symlinks=False))
            raise AssertionError("later assertion")
    assert _fd_closed(held)
    assert h.reservation.consumed_and_open, "the reservation's own descriptor was touched"


def test_full_recreation_at_c(h) -> None:
    with _held_leaf_identity(h.reservation) as old_fd:
        _full_recreation_at_c(h, old_fd)


def _full_recreation_at_c(h, old_fd) -> None:
    order: list = []
    installed = _record_publications(h, order)
    calls = _spy_sink(h, order)
    rec = h.recreator()
    exits = []
    real_exit = h.reservation.__exit__
    h.reservation.__exit__ = lambda *a: (exits.append(a), real_exit(*a))[1]
    assert _begin(rec, h) == 1
    result = _recreate(rec, 1)
    assert result.outcome is O.RECREATED and result.error is None
    new = result.workspace
    assert new is rec.worktree and new is not h.worktree and rec.worktree_entered
    assert rec.reservation is not h.reservation and rec.reservation.consumed_and_open
    assert exits == [(None, None, None)] and not h.reservation.consumed_and_open
    # Phase E before disposing(H); the complete installed sequence.
    assert order[0] == "evidence" and order[1] == "publish:disposing"
    assert installed == [
        (WorktreeIntent.DISPOSING, h.h0), (WorktreeIntent.ABSENT, None),
        (WorktreeIntent.CREATING, h.c), (WorktreeIntent.PRESENT, h.c),
    ]
    assert h.installed().checkpoint_ref.intent is CheckpointIntent.PRESENT
    assert h.installed().checkpoint_ref.accepted_sha == h.c
    # The shared sink, kind recreation, base C, trusted format.
    assert calls == [dict(
        worktree_path=h.leaf, source_repo_path=h.source, initial_commit=h.c,
        lifecycle_id=h.lease.lifecycle_id, recreation_sequence=1, object_format=h.lease.object_format,
    )]
    artifact = h.tmp / "evidence" / f"{h.lease.lifecycle_id}.recreation-01.evidence"
    assert artifact.is_file(), "the recreation artifact was not published"
    header, payload = parse_artifact_strict(artifact.read_bytes(), object_format=h.lease.object_format)
    assert (header["kind"], header["sequence"], header["base_commit"]) == ("recreation", 1, h.c)
    assert b"contaminating" in payload and b"+# accepted" not in payload
    assert result.artifact_id == f"{h.lease.lifecycle_id}.recreation-01" and result.evidence_complete is True
    # The new incarnation: HEAD C, clean, same path, a distinct directory, one admin entry.
    assert Path(new.path) == h.leaf
    old_identity = os.fstat(old_fd)  # still usable after the old reservation closed
    assert not os.path.samestat(old_identity, os.stat(h.leaf, follow_symlinks=False))
    assert _git(new.path, "rev-parse", "HEAD") == h.c and _git(new.path, "status", "--porcelain") == ""
    assert "contaminating" not in (new.path / "jobs" / "worker.py").read_text()
    assert _admin_names(h.lease) == [h.lease.lifecycle_id]
    assert _git(h.repo, "rev-parse", f"refs/codeagent/runs/{h.lease.lifecycle_id}/checkpoint") == h.c
    _finished_event(result)


def test_two_successive_recreations(h) -> None:
    rec = h.recreator()
    for k in (1, 2):
        assert rec.worktree.path is not None
        (rec.worktree.path / "jobs" / "worker.py").write_text("dirty again\n")
        assert _begin(rec, h) == k
        result = _recreate(rec, k)
        assert result is not None and result.outcome is O.RECREATED
    assert (h.tmp / "evidence").is_dir(), "no recreation artifact was captured"
    names = sorted(p.name for p in (h.tmp / "evidence").iterdir())
    assert names == [f"{h.lease.lifecycle_id}.recreation-01.evidence", f"{h.lease.lifecycle_id}.recreation-02.evidence"]


# ---------------------------------------------------------------------------
# Phase E
# ---------------------------------------------------------------------------

_DIGEST = "e" * 64


def _receipts(lid):
    def err(code):
        return OperationalError(code=code, error_id="x", message="fixed")
    aid = f"{lid}.recreation-01"
    return {
        "incomplete": EvidenceReceipt(False, False, aid, _DIGEST, 5, err(ErrorCode.EVIDENCE_INCOMPLETE)),
        "durability": EvidenceReceipt(False, True, aid, _DIGEST, 5, err(ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED)),
        "collision": EvidenceReceipt(False, False, None, None, None, err(ErrorCode.EVIDENCE_ARTIFACT_COLLISION)),
        "failed": EvidenceReceipt(False, False, None, None, None, err(ErrorCode.EVIDENCE_CAPTURE_FAILED)),
    }


@pytest.mark.parametrize("kind", ["incomplete", "durability", "collision", "failed", "raises"])
def test_unconfirmed_evidence_never_reaches_phase_d(h, kind) -> None:
    order: list = []
    installed = _record_publications(h, order)
    receipt = None if kind == "raises" else _receipts(h.lease.lifecycle_id)[kind]
    _spy_sink(h, order, receipt=receipt, raises=RuntimeError("sink broke") if kind == "raises" else None)
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.EVIDENCE_NOT_CONFIRMED and result.workspace is None
    if kind == "raises":
        assert result.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED and result.artifact_id is None
    else:
        assert result.error is receipt.error
        assert (result.artifact_id is not None) == (kind in ("incomplete", "durability"))
    assert order == ["evidence"] and installed == []
    assert rec.worktree is h.worktree and h.worktree.disposition == "active"
    assert rec.reservation is h.reservation and h.reservation.consumed_and_open
    _finished_event(result)


def test_real_untracked_contamination_is_incomplete_evidence(h) -> None:
    (h.worktree.path / "new_file.py").write_text("untracked\n")
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.EVIDENCE_NOT_CONFIRMED and result.error.code is ErrorCode.EVIDENCE_INCOMPLETE
    assert result.evidence_complete is False and h.worktree.disposition == "active"


# ---------------------------------------------------------------------------
# Phase D
# ---------------------------------------------------------------------------


def test_disposal_failure_leaves_n_current(h, monkeypatch) -> None:
    monkeypatch.setattr(h.worktree, "dispose", lambda: (_ for _ in ()).throw(GitWorktreeCleanupError("x")))
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.DISPOSAL_FAILED and result.workspace is None
    assert result.error.code is ErrorCode.WORKSPACE_RECREATION_FAILED and result.evidence_complete is True
    assert rec.worktree is h.worktree and rec.reservation is h.reservation and h.reservation.consumed_and_open
    _finished_event(result)


def test_real_leftover_admin_entry_fails_disposal_before_absent(h, monkeypatch) -> None:
    order: list = []
    installed = _record_publications(h, order)
    leftover = Path(h.lease.identity.canonical_common_dir) / "worktrees" / h.lease.lifecycle_id
    real_run = ws._run

    def run(cwd, *args, **kwargs):
        result = real_run(cwd, *args, **kwargs)
        if args[:2] == ("worktree", "remove"):
            leftover.mkdir(parents=True, exist_ok=True)
        return result

    monkeypatch.setattr(ws, "_run", run)
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    monkeypatch.setattr(ws, "_run", real_run)
    assert result.outcome is O.DISPOSAL_FAILED
    assert installed == [(WorktreeIntent.DISPOSING, h.h0)]
    assert rec.reservation is h.reservation and h.reservation.consumed_and_open
    leftover.rmdir()


def test_old_reservation_close_failure_is_retained_and_nothing_is_reserved(h, monkeypatch) -> None:
    failure = LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, "close failed")
    real_exit = h.reservation.__exit__
    attempts = []

    def failing_exit(*a):
        attempts.append(a)
        real_exit(*a)
        raise failure

    h.reservation.__exit__ = failing_exit
    reserve_calls = []
    real_reserve = h.lease.state_root.reserve_worktree_leaf
    monkeypatch.setattr(h.lease.state_root, "reserve_worktree_leaf", lambda *a, **k: reserve_calls.append(a) or real_reserve(*a, **k))
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.FAILED_NOTHING_OWNED
    assert rec.earlier_reservation_failures == [failure] and len(attempts) == 1
    assert rec.reservation is None and reserve_calls == []
    assert h.installed().worktree.intent is WorktreeIntent.ABSENT
    _finished_event(result)


# ---------------------------------------------------------------------------
# Phase R
# ---------------------------------------------------------------------------


def test_foreign_directory_at_the_path_is_failed_nothing_owned_and_untouched(h, monkeypatch) -> None:
    real_reserve = h.lease.state_root.reserve_worktree_leaf

    def planted(*a, **k):
        h.leaf.mkdir()
        (h.leaf / "foreign.txt").write_text("not ours")
        return real_reserve(*a, **k)

    monkeypatch.setattr(h.lease.state_root, "reserve_worktree_leaf", planted)
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.FAILED_NOTHING_OWNED and rec.reservation is None
    assert (h.leaf / "foreign.txt").is_file()
    assert (h.leaf / "foreign.txt").read_text() == "not ours"


@pytest.mark.parametrize("leaf_state, expected", [("absent", O.FAILED_NOTHING_OWNED), ("present", O.UNCONFIRMED), ("uninspectable", O.UNCONFIRMED)])
def test_reservation_substrate_failure_is_classified_by_a_fresh_observation(h, monkeypatch, leaf_state, expected) -> None:
    def failing(*a, **k):
        if leaf_state == "present":
            h.leaf.mkdir()
        raise LifecycleFsError(LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")

    monkeypatch.setattr(h.lease.state_root, "reserve_worktree_leaf", failing)
    if leaf_state == "uninspectable":
        monkeypatch.setattr(
            h.lease.state_root, "observe_materialized_worktree_leaf",
            lambda *a: (_ for _ in ()).throw(LifecycleFsError(LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")),
        )
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is expected and rec.reservation is None
    _finished_event(result)


# ---------------------------------------------------------------------------
# Phase C
# ---------------------------------------------------------------------------


def test_construction_refusal_is_failed_nothing_owned(h, monkeypatch) -> None:
    monkeypatch.setattr(lr, "GitWorktree", lambda *a, **k: (_ for _ in ()).throw(ws.GitWorktreeError("refused")))
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.FAILED_NOTHING_OWNED and rec.reservation is None
    assert not os.path.lexists(h.leaf) and rec.worktree is h.worktree


def _fail_publish_once(h, intent, reason):
    publisher = h.publishers.worktree_publisher
    real = publisher.publish
    state = {"done": False}

    def publish(transition):
        if transition.intent is intent and not state["done"]:
            state["done"] = True
            raise WorktreePublicationError(reason, "forced")
        real(transition)

    publisher.publish = publish


def test_creating_not_installed_is_failed_nothing_owned(h) -> None:
    rec = h.recreator()
    _begin(rec, h)
    _fail_publish_once(h, WorktreeIntent.CREATING, WorktreePublicationFailure.NOT_INSTALLED)
    result = _recreate(rec, 1)
    assert result.outcome is O.FAILED_NOTHING_OWNED and rec.reservation is None
    assert not os.path.lexists(h.leaf) and h.installed().worktree.intent is WorktreeIntent.ABSENT


def test_creating_durability_unconfirmed_is_unconfirmed(h) -> None:
    rec = h.recreator()
    _begin(rec, h)
    _fail_publish_once(h, WorktreeIntent.CREATING, WorktreePublicationFailure.DURABILITY_UNCONFIRMED)
    result = _recreate(rec, 1)
    assert result.outcome is O.UNCONFIRMED and rec.reservation is None
    _finished_event(result)


def _fail_checkout(monkeypatch):
    real = ws._run

    def run(cwd, *args, **kwargs):
        if args[:1] == ("checkout",):
            return subprocess.CompletedProcess(args, 1, "", "")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ws, "_run", run)


def test_git_failure_after_creating_publishes_confirmed_creating_to_absent(h, monkeypatch) -> None:
    order: list = []
    installed = _record_publications(h, order)
    rec = h.recreator()
    _begin(rec, h)
    _fail_checkout(monkeypatch)
    result = _recreate(rec, 1)
    assert result.outcome is O.FAILED_NOTHING_OWNED and result.workspace is None
    assert installed[-2:] == [(WorktreeIntent.CREATING, h.c), (WorktreeIntent.ABSENT, None)]
    assert rec.reservation is None and not os.path.lexists(h.leaf) and _registered(h.repo) == set()
    assert _admin_names(h.lease) == []


def test_unconfirmed_absence_never_publishes_creating_to_absent(h, monkeypatch) -> None:
    order: list = []
    installed = _record_publications(h, order)
    monkeypatch.setattr(
        lr,
        "observe_worktree_absent",
        lambda **k: wa.AbsenceObservation(wa.Absence.PRESENT, "the recomputed worktree leaf is present"),
    )
    rec = h.recreator()
    _begin(rec, h)
    _fail_checkout(monkeypatch)
    result = _recreate(rec, 1)
    assert result.outcome is O.UNCONFIRMED
    assert installed[-1] == (WorktreeIntent.CREATING, h.c)


def test_creating_to_absent_publication_failure_is_unconfirmed(h, monkeypatch) -> None:
    rec = h.recreator()
    _begin(rec, h)
    _fail_checkout(monkeypatch)
    publisher = h.publishers.worktree_publisher
    real = publisher.publish
    seen = []

    def publish(transition):
        seen.append(transition.intent)
        if transition.intent is WorktreeIntent.ABSENT and WorktreeIntent.CREATING in seen:
            raise WorktreePublicationError(WorktreePublicationFailure.NOT_INSTALLED, "forced")
        real(transition)

    publisher.publish = publish
    assert _recreate(rec, 1).outcome is O.UNCONFIRMED


def test_enter_time_cleanup_failure_is_unconfirmed(h, monkeypatch) -> None:
    rec = h.recreator()
    _begin(rec, h)
    _fail_checkout(monkeypatch)
    monkeypatch.setattr(
        GitWorktree, "_cleanup_failed_reservation_worktree", lambda self, p: GitWorktreeCleanupError("cleanup unconfirmed")
    )
    assert _recreate(rec, 1).outcome is O.UNCONFIRMED


def test_present_failure_is_unconfirmed_and_keeps_the_retained_incarnation_current(h) -> None:
    rec = h.recreator()
    _begin(rec, h)
    _fail_publish_once(h, WorktreeIntent.PRESENT, WorktreePublicationFailure.DURABILITY_UNCONFIRMED)
    result = _recreate(rec, 1)
    assert result.outcome is O.UNCONFIRMED and result.workspace is None
    assert rec.worktree is not h.worktree and rec.worktree_entered is False
    assert rec.reservation is not None and rec.reservation.consumed_and_open
    assert os.path.realpath(h.leaf) in _registered(h.repo)
    _finished_event(result)


def test_entry_gate_failure_is_adopted_then_failed(h, monkeypatch) -> None:
    real_gate = GitWorktree.entry_gate
    monkeypatch.setattr(
        GitWorktree, "entry_gate",
        lambda self, c: (_ for _ in ()).throw(ws.GitWorktreeError("dirty")) if self is not h.worktree else real_gate(self, c),
    )
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.ADOPTED_THEN_FAILED and result.workspace is rec.worktree is not h.worktree
    assert rec.worktree_entered and rec.reservation.consumed_and_open
    assert result.error.code is ErrorCode.WORKSPACE_RECREATION_FAILED
    _finished_event(result)


def test_materialization_uses_c_not_the_source_head(h) -> None:
    assert _git(h.repo, "rev-parse", "HEAD") == h.h0 != h.c
    rec = h.recreator()
    _begin(rec, h)
    result = _recreate(rec, 1)
    assert result.outcome is O.RECREATED and result.workspace is not None
    assert result.workspace.initial_commit == h.c


# ---------------------------------------------------------------------------
# Composed runs through the Step 6 seam: ownership and Amendment 14 R2
# ---------------------------------------------------------------------------

class _ContaminatingApplier:
    """Writes into the worktree, then reports a post-write failure."""

    def __init__(self, path):
        self._path = Path(path)

    def apply(self, run_id, iteration, approved_paths, operations):
        target = self._path / "jobs" / "worker.py"
        target.write_text(target.read_text() + "# contaminated\n")
        return PatchResult(
            success=False, operation_count=0, changed_paths=(), diff_bytes=0,
            error=OperationalError(code=ErrorCode.PATCH_APPLICATION_FAILED, error_id=f"{run_id}-patch", message="post-write"),
        )


class _ContaminateOnceApplier(lr.GitPatchApplier):
    """The real applier, except that the first call in a test writes into the
    worktree and reports a post-write failure (T4); later calls apply for
    real. `calls` records each call's worktree path."""

    calls: list = []

    def apply(self, run_id, iteration, approved_paths, operations):
        _ContaminateOnceApplier.calls.append(self._worktree_path)
        if len(_ContaminateOnceApplier.calls) == 1:
            return _ContaminatingApplier(self._worktree_path).apply(run_id, iteration, approved_paths, operations)
        return super().apply(run_id, iteration, approved_paths, operations)


@pytest.fixture
def composed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lr, "DockerVerifier", lambda *a, **k: FakeVerifier((events.VerificationOutcome.PASSED,)))
    monkeypatch.setattr(lr, "GitPatchApplier", _ContaminatingApplier)
    counts = {"dispose": {}, "exit": {}}
    real_dispose, real_exit = GitWorktree.dispose, sr._WorktreeLeafReservation.__exit__

    def dispose(self):
        counts["dispose"][id(self)] = counts["dispose"].get(id(self), 0) + 1
        return real_dispose(self)

    def exit_(self, *a):
        counts["exit"][id(self)] = counts["exit"].get(id(self), 0) + 1
        return real_exit(self, *a)

    monkeypatch.setattr(GitWorktree, "dispose", dispose)
    monkeypatch.setattr(sr._WorktreeLeafReservation, "__exit__", exit_)
    created = []
    real_init = lr._CompositionRecreator.__init__

    def init(self, **kwargs):
        created.append(self)
        real_init(self, **kwargs)

    monkeypatch.setattr(lr._CompositionRecreator, "__init__", init)
    with real_fixture_repo() as repo:
        yield SimpleNamespace(repo=repo, tmp=tmp_path, counts=counts, created=created)


def _compose(c, event_log=None, **kwargs):
    plan = make_plan()
    return lr.run_lifecycle_aware(
        c.repo, run_id="r-comp", task_statement="fix", approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=c.tmp / "evidence", model=MarkerGatedFakeModel(read_path="jobs/worker.py", marker="# BUG:", plan=plan),
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)), event_log=event_log, **kwargs,
    )


def _state(c):
    run_dirs = [p for p in (c.tmp / "state" / "repos").glob("*/runs/*")]
    assert len(run_dirs) == 1, "expected exactly one run directory"
    (run_dir,) = run_dirs
    import json

    return json.loads((run_dir / "lifecycle.json").read_text()), run_dir.name


def _recreation(log):
    return [e for e in log.events if isinstance(e, (events.WorkspaceRecreationStarted, events.WorkspaceRecreationFinished))]


def test_composed_recreation_tears_down_only_the_current_objects(composed) -> None:
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    (rec,) = composed.created
    # D5-B (ADR 0004 Amendment 21; migrated from the pre-N4 "no continuation
    # yet" form): the applier contaminates every attempt, so each RECREATED
    # continues through C30 until the repair budget (3) ends the run.
    finished_events = [e for e in _recreation(log) if isinstance(e, events.WorkspaceRecreationFinished)]
    assert [e.outcome for e in finished_events] == [O.RECREATED] * 3
    assert [e.recreation_sequence for e in finished_events] == [1, 2, 3]
    assert result.finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert result.release_confirmed
    projection, lid = _state(composed)
    assert projection["state"] == "COMPLETE" and projection["worktree"]["intent"] == "absent"
    # Each of the four incarnations disposed exactly once; each reservation closed exactly once.
    assert sorted(composed.counts["dispose"].values()) == [1, 1, 1, 1]
    assert sorted(composed.counts["exit"].values()) == [1, 1, 1, 1]
    assert _registered(composed.repo) == set() and _git(composed.repo, "for-each-ref", "refs/codeagent") == ""
    for k in (1, 2, 3):
        assert (composed.tmp / "evidence" / f"{lid}.recreation-{k:02d}.evidence").exists()
    assert (composed.tmp / "evidence" / f"{lid}.evidence").exists()
    events.validate_recreation_trace(log.events)


class _FailingLog(EventLog):
    def __init__(self, event_type, exc):
        super().__init__()
        self._type, self._exc, self.attempts = event_type, exc, 0

    def append(self, event):
        if isinstance(event, self._type):
            self.attempts += 1
            raise self._exc
        super().append(event)


def test_composed_finished_emission_failure_tears_down_the_new_incarnation(composed) -> None:
    log = _FailingLog(events.WorkspaceRecreationFinished, RuntimeError("emission failed"))
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    assert log.attempts == 1 and result.finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert sorted(composed.counts["dispose"].values()) == [1, 1]
    assert sorted(composed.counts["exit"].values()) == [1, 1]
    assert _state(composed)[0]["state"] == "COMPLETE" and _registered(composed.repo) == set()


def test_composed_started_emission_failure_keeps_the_original_objects(composed) -> None:
    log = _FailingLog(events.WorkspaceRecreationStarted, RuntimeError("emission failed"))
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    (rec,) = composed.created
    assert log.attempts == 1 and rec._minted == 1
    assert rec._pending is not None and rec._pending[0] == 1  # minted, never consumed: recreate() never ran
    assert list((composed.tmp / "evidence").glob("*.recreation-*")) == []
    assert list(composed.counts["dispose"].values()) == [1] and list(composed.counts["exit"].values()) == [1]
    assert result.finished.error.code is ErrorCode.UNCLASSIFIED_FAILURE
    assert _state(composed)[0]["state"] == "COMPLETE"


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


def test_composed_keyboard_interrupt_at_finished_applies_r2_to_the_current_objects(composed) -> None:
    interrupt = KeyboardInterrupt()
    log = _FailingLog(events.WorkspaceRecreationFinished, interrupt)
    caught = None
    try:
        _compose(composed, log)
    except BaseException as exc:  # noqa: BLE001 - the exact instance is asserted
        caught = exc
    assert caught is interrupt
    (rec,) = composed.created
    # The new incarnation (current, entered) was exited on the raise path;
    # the disposed earlier one was never disposed again.
    assert sorted(composed.counts["dispose"].values()) == [1, 1]
    assert sorted(composed.counts["exit"].values()) == [1, 1]
    assert _registered(composed.repo) == set()
    projection, lid = _state(composed)
    assert projection["worktree"]["intent"] == "absent" and projection["checkpoint_ref"]["intent"] == "present"
    # The ref left by the raise path is reconciled by the next admission (Amendment 16).
    _assert_admitted(composed.repo, "r-after")
    assert _git(composed.repo, "for-each-ref", "refs/codeagent") == ""


def test_composed_interrupt_after_second_creating_is_reconciled_by_section_9(composed, monkeypatch) -> None:
    """ADR 0004 Amendment 20 section 9 (Step 8): an interrupt right after the
    second incarnation's `creating(C)` leaves `creating(C)` beside
    `present(C)` with an unregistered (here, already removed) leaf. Before
    section 9 the next admission was blocked; now it reconciles."""
    real = ls.LifecycleWorktreePublisher.publish
    interrupt = KeyboardInterrupt()
    seen = {"creating": 0}

    def publish(self, transition):
        real(self, transition)
        if transition.intent is WorktreeIntent.CREATING:
            seen["creating"] += 1
            if seen["creating"] == 2:
                raise interrupt

    monkeypatch.setattr(ls.LifecycleWorktreePublisher, "publish", publish)
    caught = None
    try:
        _compose(composed, EventLog())
    except BaseException as exc:  # noqa: BLE001 - the exact instance is asserted
        caught = exc
    assert caught is interrupt
    projection, lid = _state(composed)
    assert projection["worktree"]["intent"] == "creating" and projection["checkpoint_ref"]["intent"] == "present"
    assert projection["worktree"]["expected_head"] == projection["checkpoint_ref"]["accepted_sha"]
    assert _registered(composed.repo) == set()
    # Phase E published recreation member 01 before the first incarnation was disposed.
    assert (composed.tmp / "evidence" / f"{lid}.recreation-01.evidence").exists()
    evidence = sorted((p.name, p.read_bytes()) for p in (composed.tmp / "evidence").iterdir())
    monkeypatch.setattr(ls.LifecycleWorktreePublisher, "publish", real)
    _assert_admitted(composed.repo, "r-after")
    dead = json.loads(next((composed.tmp / "state" / "repos").glob(f"*/runs/{lid}/lifecycle.json")).read_text())
    assert dead["state"] == "RECONCILED" and dead["reconciliation"]["attempts_total"] == 1
    assert dead["worktree"]["intent"] == "absent" and dead["checkpoint_ref"]["intent"] == "absent"
    assert _git(composed.repo, "for-each-ref", "refs/codeagent") == "" and _registered(composed.repo) == set()
    assert not list((composed.tmp / "state" / "worktrees").glob(f"*/{lid}"))
    assert sorted((p.name, p.read_bytes()) for p in (composed.tmp / "evidence").iterdir()) == evidence


def test_composed_old_reservation_close_failure_is_reported_once_under_reservation(composed, monkeypatch) -> None:
    failure = LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, "close failed")
    original_exit = sr._WorktreeLeafReservation.__exit__
    state = {"first": True}

    def exit_(self, *a):
        result = original_exit(self, *a)
        if state["first"]:
            state["first"] = False
            raise failure
        return result

    monkeypatch.setattr(sr._WorktreeLeafReservation, "__exit__", exit_)
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    (rec,) = composed.created
    recreation = _recreation(log)
    assert recreation and recreation[-1].outcome is O.FAILED_NOTHING_OWNED
    assert rec.earlier_reservation_failures == [failure] and rec.reservation is None
    assert result.unconfirmed_stages == (lr.CleanupStage.RESERVATION,)
    assert result.reservation_release_error is failure
    assert sorted(composed.counts["exit"].values()) == [1]


# ---------------------------------------------------------------------------
# Step 7 correction: a reservation-close BaseException is retained for R2
# ---------------------------------------------------------------------------

_CLOSE_SENTINEL = "SENTINEL-close-text /private/secret/path"


class _CloseInterrupt(BaseException):
    """A non-`Exception` raised by an earlier reservation's close."""


def _compose_catching(c, event_log=None, **kwargs):
    caught = result = None
    try:
        result = _compose(c, event_log, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - the exact instance is asserted by the caller
        caught = exc
    return result, caught


def _fail_first_reservation_close(monkeypatch, failure):
    original_exit = sr._WorktreeLeafReservation.__exit__
    state = {"first": True}

    def exit_(self, *a):
        result = original_exit(self, *a)
        if state["first"]:
            state["first"] = False
            raise failure
        return result

    monkeypatch.setattr(sr._WorktreeLeafReservation, "__exit__", exit_)


def _count_lease_closes(monkeypatch):
    counts = {"n": 0}
    real = ls.LifecycleLease.close

    def close(self):
        counts["n"] += 1
        return real(self)

    monkeypatch.setattr(ls.LifecycleLease, "close", close)
    return counts


def test_old_reservation_close_base_exception_is_retained_not_raised(h) -> None:
    interrupt = _CloseInterrupt(_CLOSE_SENTINEL)
    real_exit = h.reservation.__exit__
    attempts = []

    def failing_exit(*a):
        attempts.append(a)
        real_exit(*a)
        raise interrupt

    h.reservation.__exit__ = failing_exit
    rec = h.recreator()
    assert _begin(rec, h) == 1
    caught = result = None
    try:
        result = rec.recreate(1)
    except BaseException as exc:  # noqa: BLE001 - asserted below
        caught = exc
    assert caught is None, "the close BaseException escaped recreate()"
    assert result.outcome is O.FAILED_NOTHING_OWNED
    assert len(rec.earlier_reservation_failures) == 1 and rec.earlier_reservation_failures[0] is interrupt
    assert rec.reservation is None and len(attempts) == 1
    assert _CLOSE_SENTINEL not in result.error.message
    _finished_event(result)
    assert len(attempts) == 1  # never retried


def test_composed_sole_retained_base_exception_is_reraised_at_the_boundary(composed, monkeypatch) -> None:
    interrupt = _CloseInterrupt(_CLOSE_SENTINEL)
    _fail_first_reservation_close(monkeypatch, interrupt)
    leases = _count_lease_closes(monkeypatch)
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert result is None and caught is interrupt
    (rec,) = composed.created
    assert rec.earlier_reservation_failures == [interrupt]
    assert _recreation(log)[-1].outcome is O.FAILED_NOTHING_OWNED  # the controller reached Finished
    assert any(isinstance(e, events.RunFinished) for e in log.events)
    assert leases["n"] == 1
    assert all(_CLOSE_SENTINEL not in repr(e) for e in log.events)


@pytest.mark.parametrize("earlier_kind", ["declared", "base_exception"])
def test_composed_earlier_close_failure_plus_lease_failure_keeps_both(composed, monkeypatch, earlier_kind) -> None:
    earlier = (
        LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, _CLOSE_SENTINEL)
        if earlier_kind == "declared" else _CloseInterrupt(_CLOSE_SENTINEL)
    )
    _fail_first_reservation_close(monkeypatch, earlier)
    lease_failure = ls.LifecycleStoreError(ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED, "lease close failed")
    real_close = ls.LifecycleLease.close
    closes = {"n": 0}

    def close(self):
        closes["n"] += 1
        real_close(self)
        raise lease_failure

    monkeypatch.setattr(ls.LifecycleLease, "close", close)
    result, caught = _compose_catching(composed)
    assert closes["n"] == 1
    if earlier_kind == "declared":
        assert caught is None
        assert result.unconfirmed_stages == (lr.CleanupStage.RESERVATION, lr.CleanupStage.LEASE)
        assert result.reservation_release_error is earlier and result.lease_release_error is lease_failure
    else:
        assert isinstance(caught, lr.LifecycleRunCleanupError)
        assert caught.failed_stages == (lr.CleanupStage.RESERVATION, lr.CleanupStage.LEASE)
        assert [f.exception for f in caught.failures] == [earlier, lease_failure]
        assert caught.__cause__ is earlier and caught.finished is not None
        assert _CLOSE_SENTINEL not in str(caught)


def test_composed_raise_path_keeps_the_interrupt_original_and_the_close_failure_attributed(composed, monkeypatch) -> None:
    earlier = LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, _CLOSE_SENTINEL)
    _fail_first_reservation_close(monkeypatch, earlier)
    leases = _count_lease_closes(monkeypatch)
    interrupt = KeyboardInterrupt()
    log = _FailingLog(events.WorkspaceRecreationFinished, interrupt)
    result, caught = _compose_catching(composed, log)
    assert result is None and isinstance(caught, lr.LifecycleRunCleanupError)
    assert caught.original is interrupt and caught.__cause__ is interrupt
    assert caught.failed_stages == (lr.CleanupStage.RESERVATION,)
    assert [f.exception for f in caught.failures] == [earlier]
    assert log.attempts == 1 and leases["n"] == 1
    assert _CLOSE_SENTINEL not in str(caught)
    (rec,) = composed.created
    assert rec.earlier_reservation_failures == [earlier] and rec.reservation is None
    _assert_admitted(composed.repo, "r-after")  # the dead entry reconciles


# ---------------------------------------------------------------------------
# The recreation-budget API (accepted: internal, default 3, exact 0..99)
# ---------------------------------------------------------------------------


def test_recreation_budget_defaults_to_three_and_the_format_bound_is_99() -> None:
    import inspect

    param = inspect.signature(lr.run_lifecycle_aware).parameters["max_recreations"]
    assert param.default == 3 and param.kind is inspect.Parameter.KEYWORD_ONLY
    assert lr.RECREATION_FORMAT_BOUND == 99 == events.RECREATION_SEQUENCE_MAX


@pytest.mark.parametrize("value", [-1, 100, True, False, 1.0, 3.0, "3", None], ids=repr)
def test_an_invalid_recreation_budget_is_refused_before_anything_happens(tmp_path, monkeypatch, value) -> None:
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(tmp_path / "state"))
    prepared = []
    monkeypatch.setattr(lr, "prepare_lifecycle", lambda *a, **k: prepared.append(a))
    constructed = []
    monkeypatch.setattr(lr, "GitWorktree", lambda *a, **k: constructed.append(a))
    caught = None
    try:
        lr.run_lifecycle_aware(
            tmp_path / "no-repo", run_id="r-budget", task_statement="t", approval_mode=domain.ApprovalMode.INTERACTIVE,
            evidence_root=tmp_path / "evidence", model=None, approval=None, max_recreations=value,
        )
    except Exception as exc:  # noqa: BLE001 - the exact type and message are asserted
        caught = exc
    assert type(caught) is ValueError and str(caught) == "max_recreations must be an integer from 0 to 99"
    assert prepared == [] and constructed == []
    assert not (tmp_path / "state").exists() and not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("value", [0, 99])
def test_the_recreation_budget_bounds_are_accepted(composed, value) -> None:
    log = EventLog()
    result, caught = _compose_catching(composed, log, max_recreations=value)
    assert caught is None, f"unexpected {type(caught).__name__}"
    (rec,) = composed.created
    assert rec._budget == value
    outcomes = [e.outcome for e in _recreation(log) if isinstance(e, events.WorkspaceRecreationFinished)]
    if value == 0:
        assert outcomes == [] and _code(result.finished) is ErrorCode.PATCH_APPLICATION_FAILED  # Q6 refused
    else:
        # D5-B: each RECREATED continues; the repair budget (3) ends the run.
        assert outcomes == [O.RECREATED] * 3
        assert result.finished.terminal_reason is domain.TerminalReason.BUDGET_EXCEEDED
    assert result.release_confirmed


@pytest.mark.parametrize("repair, recreations, expected", [(4, None, 3), (1, 5, 5), (0, 2, 2)])
def test_the_recreation_budget_is_independent_of_the_repair_budget(composed, repair, recreations, expected) -> None:
    kwargs = {"max_repair_iterations": repair}
    if recreations is not None:
        kwargs["max_recreations"] = recreations
    _, caught = _compose_catching(composed, **kwargs)
    assert caught is None, f"unexpected {type(caught).__name__}"
    assert len(composed.created) == 1
    (rec,) = composed.created
    assert rec._budget == expected


def test_q6_applies_both_the_budget_and_the_format_bound(h) -> None:
    under_budget = h.recreator(max_recreations=2)
    under_budget._minted = 2
    assert _begin(under_budget, h) is None and under_budget._minted == 2
    at_bound = h.recreator(max_recreations=99)
    at_bound._minted = 99
    assert _begin(at_bound, h) is None and at_bound._minted == 99
    last = h.recreator(max_recreations=99)
    last._minted = 98
    assert _begin(last, h) == 99


# ===========================================================================
# D5-B (ADR 0004 Amendment 21): factory, ordering, C29 and latency, real Git
# ===========================================================================


def _one(items):
    """Exactly one element, by explicit assertion (strict mutation rule)."""
    items = list(items)
    assert len(items) == 1, f"expected exactly one, got {len(items)}"
    return items[0]

from codeagent.cancellation import CancellationSource  # noqa: E402
from codeagent.repo_tools import RepoTools  # noqa: E402
from codeagent.tool_schema import ToolCleanupUnconfirmedError  # noqa: E402


def _capture_controller(monkeypatch):
    box: list = []

    class Recording(RunController):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.initial_workspace = self._workspace
            box.append(self)

    monkeypatch.setattr(lr, "RunController", Recording)
    return box


def _failing_factory(monkeypatch, exc):
    """RepoTools whose second construction (the C3a factory call) raises."""
    made: list = []

    class Factory(RepoTools):
        def __init__(self, path):
            made.append(Path(path))
            if len(made) == 2:
                raise exc
            super().__init__(path)

    monkeypatch.setattr(lr, "RepoTools", Factory)
    return made


def test_factory_failure_is_adopted_then_failed(composed, monkeypatch) -> None:
    """B17, B18: an ordinary factory failure adopts n+1, terminates through
    WORKSPACE_RECREATION_FAILED, and n+1 is torn down exactly once."""
    box = _capture_controller(monkeypatch)
    made = _failing_factory(monkeypatch, ValueError("factory failed"))
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    done = _one([e for e in _recreation(log) if isinstance(e, events.WorkspaceRecreationFinished)])
    assert done.outcome is O.ADOPTED_THEN_FAILED
    assert _code(result.finished) is ErrorCode.WORKSPACE_RECREATION_FAILED
    assert len(box) == 1
    controller = box[0]
    assert len(made) == 2 and controller._workspace is not controller.initial_workspace
    assert controller._tools is __import__("codeagent").controller._RETIRED_TOOLS
    assert sorted(composed.counts["dispose"].values()) == [1, 1]
    assert sorted(composed.counts["exit"].values()) == [1, 1]
    assert _registered(composed.repo) == set()


def test_factory_cleanup_unconfirmed_retained_composed(composed, monkeypatch) -> None:
    """B35 (with the real recreator): a factory cleanup failure travels by
    identity into the controller's retained list; no tool event; no text."""
    box = _capture_controller(monkeypatch)
    injected = ToolCleanupUnconfirmedError((OSError("SENTINEL-FACTORY-91"),), None)
    _failing_factory(monkeypatch, injected)
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert caught is None, f"unexpected {type(caught).__name__}"
    assert len(box) == 1
    controller = box[0]
    assert len(controller.tool_cleanup_failures) == 1 and controller.tool_cleanup_failures[-1] is injected
    assert _code(result.finished) is ErrorCode.WORKSPACE_RECREATION_FAILED
    requested = [e for e in log.events if isinstance(e, events.ToolRequested)]
    completed = [e for e in log.events if isinstance(e, events.ToolCompleted)]
    assert len(completed) == len(requested)
    assert all("SENTINEL-FACTORY-91" not in repr(e) for e in log.events)
    assert sorted(composed.counts["dispose"].values()) == [1, 1]


def test_factory_interrupt_takes_raise_path(composed, monkeypatch) -> None:
    """B19: an interrupt from the factory propagates as the exact instance;
    the trace ends with an unmatched Started; R2 acts on n+1."""
    interrupt = KeyboardInterrupt()
    _failing_factory(monkeypatch, interrupt)
    log = EventLog()
    result, caught = _compose_catching(composed, log)
    assert caught is interrupt and result is None
    assert [type(e).__name__ for e in _recreation(log)] == ["WorkspaceRecreationStarted"]
    assert not [e for e in log.events if isinstance(e, events.RunFinished)]
    assert sorted(composed.counts["exit"].values()) == [1, 1]
    assert _registered(composed.repo) == set()
    # The raise path (Amendment 14) never runs _terminate(), so the checkpoint
    # ref is a durable leftover; the next admission reconciles it.
    assert _git(composed.repo, "for-each-ref", "refs/codeagent") != ""
    _admit(composed.repo, "r-after-interrupt")
    assert _git(composed.repo, "for-each-ref", "refs/codeagent") == ""


def test_factory_runs_after_present_and_entry_gate(composed, monkeypatch) -> None:
    """B21: C3a's factory call follows n+1's durable `present` and C3's entry
    gate."""
    order: list = []
    real_publish = ls.LifecycleWorktreePublisher.publish
    real_gate = GitWorktree.entry_gate

    def publish(self, transition):
        real_publish(self, transition)
        order.append(("publish", transition.intent))

    def gate(self, expected):
        order.append(("entry_gate", None))
        return real_gate(self, expected)

    class Factory(RepoTools):
        def __init__(self, path):
            order.append(("factory", None))
            super().__init__(path)

    monkeypatch.setattr(ls.LifecycleWorktreePublisher, "publish", publish)
    monkeypatch.setattr(GitWorktree, "entry_gate", gate)
    monkeypatch.setattr(lr, "RepoTools", Factory)
    _compose_catching(composed, max_recreations=1)
    factories = [i for i, (kind, _) in enumerate(order) if kind == "factory"]
    assert len(factories) == 2
    second = factories[1]
    assert order[second - 2:second] == [("publish", WorktreeIntent.PRESENT), ("entry_gate", None)]


def test_unused_minted_sequence_is_never_reissued(h) -> None:
    """B31: a minted sequence that was never passed to recreate() is never
    handed out again."""
    rec = h.recreator()
    assert _begin(rec, h) == 1
    assert isinstance(_raised(lambda: rec.begin(trigger_code=ErrorCode.PATCH_APPLICATION_FAILED, checkpoint_id=h.c)), RuntimeError)
    assert rec._minted == 1


def test_composed_c29_cancellation_real_git(composed, monkeypatch) -> None:
    """C29 with the real recreator: cancellation requested during Q5's ref
    observation is observed right after begin(); k is consumed with no event,
    artifact or mutation, and teardown acts on n."""
    source = CancellationSource()
    armed = {"on": False}

    class Applier(_ContaminatingApplier):
        def apply(self, *a):
            armed["on"] = True
            return super().apply(*a)

    real_observe = lr.CheckpointRef.observe

    def observe(self):
        if armed["on"]:
            armed["on"] = False
            source.request("test")
        return real_observe(self)

    monkeypatch.setattr(lr, "GitPatchApplier", Applier)
    monkeypatch.setattr(lr.CheckpointRef, "observe", observe)
    log = EventLog()
    result, caught = _compose_catching(composed, log, cancellation=source.token)
    assert caught is None, f"unexpected {type(caught).__name__}"
    assert result.finished.terminal_reason is domain.TerminalReason.CANCELLED
    assert _recreation(log) == []
    rec = _one(composed.created)
    assert rec._minted == 1
    lid = _state(composed)[1]
    assert not (composed.tmp / "evidence" / f"{lid}.recreation-01.evidence").exists()
    assert (composed.tmp / "evidence" / f"{lid}.evidence").exists()
    assert sorted(composed.counts["dispose"].values()) == [1]
    assert sorted(composed.counts["exit"].values()) == [1]
    assert _registered(composed.repo) == set() and _git(composed.repo, "for-each-ref", "refs/codeagent") == ""
    _admit(composed.repo, "r-after-c29")


# The exact number of Git subprocesses one successful recreation launches
# (Phases E, D, R and C plus the C3 entry gate), pinned so the documented
# cancellation-latency bound (Amendment 21 §21.3a) cannot drift silently.
RECREATION_GIT_LAUNCHES = 21


def test_recreation_git_invocation_count_is_pinned(composed, monkeypatch) -> None:
    launches: list = []
    active = {"on": False}
    real_popen = subprocess.Popen

    class Counting(real_popen):
        def __init__(self, args, *a, **k):
            if active["on"] and Path(str(args[0])).name == "git":
                launches.append(tuple(args[:4]))
            super().__init__(args, *a, **k)

    real_recreate = lr._CompositionRecreator.recreate

    def recreate(self, k):
        active["on"] = True
        try:
            return real_recreate(self, k)
        finally:
            active["on"] = False

    monkeypatch.setattr(subprocess, "Popen", Counting)
    monkeypatch.setattr(lr._CompositionRecreator, "recreate", recreate)
    log = EventLog()
    _compose_catching(composed, log, max_recreations=1)
    assert [e.outcome for e in _recreation(log) if isinstance(e, events.WorkspaceRecreationFinished)] == [O.RECREATED]
    assert len(launches) == RECREATION_GIT_LAUNCHES, launches
