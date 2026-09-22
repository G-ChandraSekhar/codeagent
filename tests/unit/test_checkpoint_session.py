"""Tests for the in-memory checkpoint transition record (slice 2B-1).

Two layers, deliberately separated:

- `CheckpointTransition` is pure data. Its ADR 0004 section 5 table is
  tested exhaustively against a generated set of every intent x
  field-presence combination, so a future edit that quietly widens the
  accepted shapes fails here.
- `CheckpointSession`'s collapse rules are tested against a recording
  double that raises exactly the `(reason, outcome)` pairs the real
  primitive produces, plus real-repository round trips proving the
  double is not lying about the primitive's behavior.

The suite also pins what this slice deliberately does *not* do: no
filesystem state, no locks, no refs beyond the one wrapped primitive's,
no events, no Docker, no controller behavior.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from codeagent import checkpoint_session as checkpoint_session_module
from codeagent.checkpoint_ref import (
    CheckpointRef,
    CheckpointRefError,
    CheckpointRefFailure,
    MutationOutcome,
    new_lifecycle_id,
)
from codeagent.checkpoint_session import (
    ABSENT_TRANSITION,
    CheckpointIntent,
    CheckpointSession,
    CheckpointSessionError,
    CheckpointSessionFailure,
    CheckpointTransition,
)

A = "a" * 40
B = "b" * 40
C = "c" * 40
A256 = "a" * 64

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent Test",
    "-c",
    "user.email=codeagent-test@example.invalid",
]


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=check,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True, env=_clean_env()
    )
    (path / "file.txt").write_text("one\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-qm", "first")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo: Path, text: str) -> str:
    (repo / "file.txt").write_text(text)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", f"commit {text.strip()}")
    return _head(repo)


def _ref_value(repo: Path, ref_name: str) -> str:
    return _git(repo, "for-each-ref", "--format=%(objectname)", ref_name).stdout.strip()


class _RecordingRef:
    """A `CheckpointRef` stand-in that records every call and raises a
    scripted `CheckpointRefError`. Anything the session touches beyond
    the three mutations and the two read-only properties raises, so an
    accidental extra Git interaction fails loudly."""

    def __init__(self, *, failure: CheckpointRefError | None = None, hex_length: int = 40) -> None:
        self.calls: list[tuple] = []
        self._failure = failure
        self.object_format = _FakeObjectFormat(hex_length)

    lifecycle_id = "0123456789abcdef0123456789abcdef"
    ref_name = "refs/codeagent/runs/0123456789abcdef0123456789abcdef/checkpoint"

    def _maybe_fail(self) -> None:
        if self._failure is not None:
            raise self._failure

    def create(self, new_oid: str) -> None:
        self.calls.append(("create", new_oid))
        self._maybe_fail()

    def advance(self, *, expected_old_oid: str, new_oid: str) -> None:
        self.calls.append(("advance", expected_old_oid, new_oid))
        self._maybe_fail()

    def delete(self, *, expected_oid: str) -> None:
        self.calls.append(("delete", expected_oid))
        self._maybe_fail()


class _FakeObjectFormat:
    def __init__(self, hex_length: int) -> None:
        self.hex_length = hex_length
        self.value = "sha1" if hex_length == 40 else "sha256"


def _error(reason: CheckpointRefFailure, outcome: MutationOutcome) -> CheckpointRefError:
    return CheckpointRefError(reason, "scripted failure", outcome=outcome)


# --------------------------------------------------------------------
# CheckpointTransition: ADR 0004 section 5's table, exhaustively
# --------------------------------------------------------------------

_VALID_COMBINATIONS = [
    (CheckpointIntent.ABSENT, None, None, None),
    (CheckpointIntent.CREATING, None, None, A),
    (CheckpointIntent.PRESENT, A, None, None),
    (CheckpointIntent.ADVANCING, A, A, B),
    (CheckpointIntent.REMOVING, A, A, None),
]


@pytest.mark.parametrize(
    ("intent", "accepted", "expected_old", "proposed_new"), _VALID_COMBINATIONS
)
def test_valid_transition_combinations_are_accepted(
    intent: CheckpointIntent,
    accepted: str | None,
    expected_old: str | None,
    proposed_new: str | None,
) -> None:
    record = CheckpointTransition(
        intent=intent,
        accepted_sha=accepted,
        expected_old_sha=expected_old,
        proposed_new_sha=proposed_new,
    )
    assert record.intent is intent
    assert record.accepted_sha == accepted
    assert record.expected_old_sha == expected_old
    assert record.proposed_new_sha == proposed_new


def _all_field_presence_combinations():
    """Every intent crossed with every (accepted, expected_old,
    proposed_new) presence pattern — 5 x 8 = 40 combinations, of which
    exactly 5 are legal."""
    for intent in CheckpointIntent:
        for accepted in (None, A):
            for expected_old in (None, A):
                for proposed_new in (None, B):
                    yield intent, accepted, expected_old, proposed_new


@pytest.mark.parametrize(
    ("intent", "accepted", "expected_old", "proposed_new"),
    list(_all_field_presence_combinations()),
)
def test_every_field_presence_combination_is_accepted_only_if_the_table_allows_it(
    intent: CheckpointIntent,
    accepted: str | None,
    expected_old: str | None,
    proposed_new: str | None,
) -> None:
    # Legality is decided by the *presence* pattern the table requires,
    # not by which particular object id occupies a slot. The generator
    # always uses A for accepted/expected_old and B for proposed_new,
    # so the `advancing` row's "expected_old == accepted" and
    # "proposed_new != accepted" constraints hold automatically for the
    # one legal presence pattern.
    presence = (accepted is not None, expected_old is not None, proposed_new is not None)
    expected_presence = {
        CheckpointIntent.ABSENT: (False, False, False),
        CheckpointIntent.CREATING: (False, False, True),
        CheckpointIntent.PRESENT: (True, False, False),
        CheckpointIntent.ADVANCING: (True, True, True),
        CheckpointIntent.REMOVING: (True, True, False),
    }[intent]
    if presence == expected_presence:
        CheckpointTransition(
            intent=intent,
            accepted_sha=accepted,
            expected_old_sha=expected_old,
            proposed_new_sha=proposed_new,
        )
        return
    with pytest.raises(ValueError):
        CheckpointTransition(
            intent=intent,
            accepted_sha=accepted,
            expected_old_sha=expected_old,
            proposed_new_sha=proposed_new,
        )


def test_advancing_requires_expected_old_to_equal_accepted() -> None:
    with pytest.raises(ValueError, match="expected_old_sha must equal"):
        CheckpointTransition(
            intent=CheckpointIntent.ADVANCING,
            accepted_sha=A,
            expected_old_sha=C,
            proposed_new_sha=B,
        )


def test_advancing_requires_a_different_proposed_new() -> None:
    with pytest.raises(ValueError, match="must differ"):
        CheckpointTransition(
            intent=CheckpointIntent.ADVANCING,
            accepted_sha=A,
            expected_old_sha=A,
            proposed_new_sha=A,
        )


def test_removing_requires_expected_old_to_equal_accepted() -> None:
    with pytest.raises(ValueError, match="expected_old_sha must equal"):
        CheckpointTransition(
            intent=CheckpointIntent.REMOVING, accepted_sha=A, expected_old_sha=C
        )


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 40, "a" * 39, "a" * 41, "a" * 63, 12345])
def test_malformed_object_ids_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=bad)


def test_both_git_object_id_lengths_are_structurally_accepted() -> None:
    assert CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=A).accepted_sha == A
    assert (
        CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=A256).accepted_sha
        == A256
    )


def test_intent_must_be_a_checkpoint_intent() -> None:
    with pytest.raises(ValueError, match="must be a CheckpointIntent"):
        CheckpointTransition(intent="present")  # type: ignore[arg-type]


def test_transition_is_immutable() -> None:
    record = CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=A)
    with pytest.raises(Exception):
        record.accepted_sha = B  # type: ignore[misc]


def test_is_transitional_flags_exactly_the_three_transitional_intents() -> None:
    transitional = {
        intent
        for intent, accepted, old, new in _VALID_COMBINATIONS
        if CheckpointTransition(
            intent=intent, accepted_sha=accepted, expected_old_sha=old, proposed_new_sha=new
        ).is_transitional
    }
    assert transitional == {
        CheckpointIntent.CREATING,
        CheckpointIntent.ADVANCING,
        CheckpointIntent.REMOVING,
    }


def test_intent_values_are_pinned() -> None:
    assert {i.value for i in CheckpointIntent} == {
        "absent",
        "creating",
        "present",
        "advancing",
        "removing",
    }


# --------------------------------------------------------------------
# Session: the happy paths and the write-ahead record
# --------------------------------------------------------------------


def test_a_new_session_starts_absent() -> None:
    session = CheckpointSession(_RecordingRef())
    assert session.transition == ABSENT_TRANSITION
    assert session.intent is CheckpointIntent.ABSENT
    assert session.accepted_sha is None


def test_establish_applied_collapses_to_present() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)

    session.establish(A)

    assert ref.calls == [("create", A)]
    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.PRESENT, accepted_sha=A
    )


def test_advance_applied_collapses_to_present_new() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)

    session.advance(B)

    assert ref.calls == [("create", A), ("advance", A, B)]
    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.PRESENT, accepted_sha=B
    )


def test_delete_applied_collapses_to_absent() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)

    session.delete()

    assert ref.calls == [("create", A), ("delete", A)]
    assert session.transition == ABSENT_TRANSITION


def test_session_exposes_the_wrapped_refs_identity() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    assert session.lifecycle_id == ref.lifecycle_id
    assert session.ref_name == ref.ref_name


# --------------------------------------------------------------------
# Session: every operation x outcome collapse row
# --------------------------------------------------------------------

_NON_COLLAPSING = [
    MutationOutcome.UNEXPECTED,
    MutationOutcome.SYMBOLIC,
    MutationOutcome.UNKNOWN,
]


@pytest.mark.parametrize("outcome", _NON_COLLAPSING)
def test_establish_non_collapsing_outcomes_stay_creating(outcome: MutationOutcome) -> None:
    failure = _error(CheckpointRefFailure.UNEXPECTED_VALUE, outcome)
    session = CheckpointSession(_RecordingRef(failure=failure))

    with pytest.raises(CheckpointRefError) as excinfo:
        session.establish(A)

    assert excinfo.value is failure  # the occurrence propagates unchanged
    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.CREATING, proposed_new_sha=A
    )


def test_establish_unchanged_collapses_to_absent() -> None:
    failure = _error(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, MutationOutcome.UNCHANGED
    )
    session = CheckpointSession(_RecordingRef(failure=failure))

    with pytest.raises(CheckpointRefError):
        session.establish(A)

    assert session.transition == ABSENT_TRANSITION


@pytest.mark.parametrize("outcome", _NON_COLLAPSING)
def test_advance_non_collapsing_outcomes_stay_advancing(outcome: MutationOutcome) -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    ref._failure = _error(CheckpointRefFailure.UNEXPECTED_VALUE, outcome)

    with pytest.raises(CheckpointRefError):
        session.advance(B)

    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.ADVANCING,
        accepted_sha=A,
        expected_old_sha=A,
        proposed_new_sha=B,
    )


def test_advance_unchanged_collapses_back_to_present_old() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    ref._failure = _error(
        CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, MutationOutcome.UNCHANGED
    )

    with pytest.raises(CheckpointRefError):
        session.advance(B)

    # The proposed commit is unaccepted; the previous checkpoint stands.
    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.PRESENT, accepted_sha=A
    )


@pytest.mark.parametrize(
    "outcome",
    [MutationOutcome.UNCHANGED, MutationOutcome.UNEXPECTED, MutationOutcome.SYMBOLIC, MutationOutcome.UNKNOWN],
)
def test_every_non_applied_delete_outcome_stays_removing(outcome: MutationOutcome) -> None:
    """Unlike create/advance, a delete never collapses on UNCHANGED:
    a ref still present is precisely an unconfirmed cleanup."""
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    ref._failure = _error(CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, outcome)

    with pytest.raises(CheckpointRefError):
        session.delete()

    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.REMOVING, accepted_sha=A, expected_old_sha=A
    )


# --------------------------------------------------------------------
# Session: TRANSACTION_CLEANUP_UNCONFIRMED never permits a collapse
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [MutationOutcome.APPLIED, MutationOutcome.UNCHANGED, MutationOutcome.UNKNOWN],
)
def test_cleanup_unconfirmed_never_collapses_establish(outcome: MutationOutcome) -> None:
    """Even an error that claims UNCHANGED (or APPLIED) must not
    collapse the record when the child process could not be confirmed
    dead — its lock may still be held. The session applies this rule
    itself rather than trusting the primitive to have applied it."""
    failure = _error(CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED, outcome)
    session = CheckpointSession(_RecordingRef(failure=failure))

    with pytest.raises(CheckpointRefError):
        session.establish(A)

    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.CREATING, proposed_new_sha=A
    )


@pytest.mark.parametrize(
    "outcome",
    [MutationOutcome.APPLIED, MutationOutcome.UNCHANGED, MutationOutcome.UNKNOWN],
)
def test_cleanup_unconfirmed_never_collapses_advance(outcome: MutationOutcome) -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    ref._failure = _error(CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED, outcome)

    with pytest.raises(CheckpointRefError):
        session.advance(B)

    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.ADVANCING,
        accepted_sha=A,
        expected_old_sha=A,
        proposed_new_sha=B,
    )


def test_an_error_claiming_applied_never_collapses_a_record() -> None:
    """APPLIED is signalled by a normal return and nothing else. An
    error asserting it would be a collaborator bug, and acting on it
    would be guessing."""
    failure = _error(CheckpointRefFailure.OBSERVATION_FAILED, MutationOutcome.APPLIED)
    session = CheckpointSession(_RecordingRef(failure=failure))

    with pytest.raises(CheckpointRefError):
        session.establish(A)

    assert session.intent is CheckpointIntent.CREATING


# --------------------------------------------------------------------
# Session: incompatible operations from transitional states
# --------------------------------------------------------------------


def _stuck_session(intent: CheckpointIntent) -> CheckpointSession:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    if intent is CheckpointIntent.CREATING:
        ref._failure = _error(CheckpointRefFailure.OBSERVATION_FAILED, MutationOutcome.UNKNOWN)
        with pytest.raises(CheckpointRefError):
            session.establish(A)
    elif intent is CheckpointIntent.ADVANCING:
        session.establish(A)
        ref._failure = _error(CheckpointRefFailure.OBSERVATION_FAILED, MutationOutcome.UNKNOWN)
        with pytest.raises(CheckpointRefError):
            session.advance(B)
    elif intent is CheckpointIntent.REMOVING:
        session.establish(A)
        ref._failure = _error(CheckpointRefFailure.OBSERVATION_FAILED, MutationOutcome.UNKNOWN)
        with pytest.raises(CheckpointRefError):
            session.delete()
    assert session.intent is intent
    ref._failure = None
    ref.calls.clear()
    return session


@pytest.mark.parametrize(
    "intent",
    [CheckpointIntent.CREATING, CheckpointIntent.ADVANCING, CheckpointIntent.REMOVING],
)
def test_establish_is_refused_from_every_transitional_state(intent: CheckpointIntent) -> None:
    session = _stuck_session(intent)
    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish(C)
    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION
    assert session.intent is intent  # unchanged: nothing was guessed


@pytest.mark.parametrize(
    "intent",
    [CheckpointIntent.CREATING, CheckpointIntent.ADVANCING, CheckpointIntent.REMOVING],
)
def test_advance_is_refused_from_every_transitional_state(intent: CheckpointIntent) -> None:
    session = _stuck_session(intent)
    with pytest.raises(CheckpointSessionError) as excinfo:
        session.advance(C)
    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION
    assert session.intent is intent


@pytest.mark.parametrize(
    "intent",
    [CheckpointIntent.CREATING, CheckpointIntent.ADVANCING, CheckpointIntent.REMOVING],
)
def test_delete_is_refused_from_every_transitional_state(intent: CheckpointIntent) -> None:
    """Deleting from `creating`/`advancing` would mean guessing which
    candidate value the ref holds. `removing` is refused for a subtler
    reason: this record does not retain whether it arose from a
    confirmed-unchanged failure or an unknown process/lock outcome, and
    only the first could be retried blindly. Distinguishing them needs
    a fresh inspection of the live ref — ADR 0004 section 8 dead-run
    reconciliation (Milestone 3), which owns any later retry."""
    session = _stuck_session(intent)
    ref = session._ref  # the recording double, to prove no Git call

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.delete()

    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION
    assert ref.calls == []  # refused before reaching CheckpointRef
    assert session.intent is intent  # unchanged: nothing was guessed


def test_establish_is_refused_when_already_present() -> None:
    session = CheckpointSession(_RecordingRef())
    session.establish(A)
    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish(B)
    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION


def test_advance_is_refused_before_establishment() -> None:
    session = CheckpointSession(_RecordingRef())
    with pytest.raises(CheckpointSessionError) as excinfo:
        session.advance(A)
    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION


def test_advance_to_the_same_sha_is_refused() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    ref.calls.clear()

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.advance(A)

    assert excinfo.value.reason is CheckpointSessionFailure.INCOMPATIBLE_OPERATION
    assert ref.calls == []  # refused before any mutation was attempted
    assert session.intent is CheckpointIntent.PRESENT


def test_delete_from_absent_is_a_no_op_with_no_git_call() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)

    session.delete()

    assert ref.calls == []
    assert session.transition == ABSENT_TRANSITION


def test_malformed_object_ids_are_refused_before_any_mutation() -> None:
    ref = _RecordingRef()
    session = CheckpointSession(ref)

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish("not-a-sha")

    assert excinfo.value.reason is CheckpointSessionFailure.MALFORMED_OID
    assert ref.calls == []
    assert session.intent is CheckpointIntent.ABSENT


def test_object_ids_are_validated_against_the_repositorys_own_format() -> None:
    """A 40-character id is malformed in a SHA-256 repository: the
    generic hex shape is not sufficient."""
    ref = _RecordingRef(hex_length=64)
    session = CheckpointSession(ref)

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish(A)

    assert excinfo.value.reason is CheckpointSessionFailure.MALFORMED_OID
    assert "sha256" in excinfo.value.message
    assert ref.calls == []

    session.establish(A256)
    assert session.accepted_sha == A256


def test_all_zero_oid_refused_by_session_establish_sha1() -> None:
    # ADR 0004 section 5: the zero OID is a Git-argv-only value
    # (checkpoint_ref.ObjectFormat.zero_oid), never an operator-
    # supplied or persisted transition value -- checked here at the
    # CheckpointSession boundary, not only in CheckpointTransition's
    # own __post_init__.
    ref = _RecordingRef()
    session = CheckpointSession(ref)

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish("0" * 40)

    assert excinfo.value.reason is CheckpointSessionFailure.MALFORMED_OID
    assert ref.calls == []
    assert session.intent is CheckpointIntent.ABSENT


def test_all_zero_oid_refused_by_session_establish_sha256() -> None:
    ref = _RecordingRef(hex_length=64)
    session = CheckpointSession(ref)

    with pytest.raises(CheckpointSessionError) as excinfo:
        session.establish("0" * 64)

    assert excinfo.value.reason is CheckpointSessionFailure.MALFORMED_OID
    assert ref.calls == []


def test_all_zero_oid_refused_by_checkpoint_transition_directly_sha1() -> None:
    # The shared validation boundary itself: CheckpointTransition's own
    # __post_init__, independent of any CheckpointSession.
    with pytest.raises(ValueError):
        CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha="0" * 40)


def test_all_zero_oid_refused_by_checkpoint_transition_directly_sha256() -> None:
    with pytest.raises(ValueError):
        CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha="0" * 64)


def test_ordinary_nonzero_oids_remain_accepted_sha1_and_sha256() -> None:
    # Confirms the zero-OID fix does not overreach.
    t1 = CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=A)
    assert t1.accepted_sha == A
    t2 = CheckpointTransition(intent=CheckpointIntent.PRESENT, accepted_sha=A256)
    assert t2.accepted_sha == A256


# --------------------------------------------------------------------
# Against a real repository and a real CheckpointRef
# --------------------------------------------------------------------


def test_full_lifecycle_against_a_real_repository(repo: Path) -> None:
    session = CheckpointSession(CheckpointRef(repo, new_lifecycle_id()))
    first = _head(repo)

    session.establish(first)
    assert _ref_value(repo, session.ref_name) == first
    assert session.accepted_sha == first

    second = _commit(repo, "two\n")
    session.advance(second)
    assert _ref_value(repo, session.ref_name) == second
    assert session.accepted_sha == second

    session.delete()
    assert _ref_value(repo, session.ref_name) == ""
    assert session.transition == ABSENT_TRANSITION


def test_a_real_pre_existing_ref_leaves_the_record_creating(repo: Path) -> None:
    """The real primitive's UNEXPECTED outcome, not a scripted one."""
    lifecycle_id = new_lifecycle_id()
    ref_name = f"refs/codeagent/runs/{lifecycle_id}/checkpoint"
    first = _head(repo)
    _git(repo, "update-ref", ref_name, first)

    session = CheckpointSession(CheckpointRef(repo, lifecycle_id))
    with pytest.raises(CheckpointRefError) as excinfo:
        session.establish(first)

    assert excinfo.value.outcome is MutationOutcome.UNEXPECTED
    assert session.intent is CheckpointIntent.CREATING
    # Refused, never overwritten.
    assert _ref_value(repo, ref_name) == first


def test_a_real_losing_advance_collapses_back_to_the_previous_checkpoint(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof that the real primitive's UNCHANGED outcome
    drives the documented collapse, with no second observation."""
    import codeagent.checkpoint_ref as checkpoint_ref_module

    session = CheckpointSession(CheckpointRef(repo, new_lifecycle_id()))
    first = _head(repo)
    session.establish(first)
    second = _commit(repo, "two\n")

    def failing_commit(self):
        raise CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
            "the git ref transaction did not finish within its time limit",
        )

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "commit", failing_commit)

    with pytest.raises(CheckpointRefError) as excinfo:
        session.advance(second)

    assert excinfo.value.reason is CheckpointRefFailure.GIT_COMMAND_TIMEOUT
    assert excinfo.value.outcome is MutationOutcome.UNCHANGED
    assert session.transition == CheckpointTransition(
        intent=CheckpointIntent.PRESENT, accepted_sha=first
    )
    assert _ref_value(repo, session.ref_name) == first


# --------------------------------------------------------------------
# Scope: what this slice deliberately does NOT introduce
# --------------------------------------------------------------------


def test_module_imports_nothing_beyond_its_narrow_dependencies() -> None:
    """Structural proof of the unwired boundary: no events, errors,
    controller, workspace, executor, patch, subprocess, os, pathlib,
    json, fcntl or tempfile — no persistence, locks, or Git of its
    own."""
    source = Path(checkpoint_session_module.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "re", "dataclasses", "enum", "codeagent.checkpoint_ref"}


def test_session_creates_no_filesystem_state_and_no_other_refs(repo: Path, tmp_path: Path) -> None:
    """A full lifecycle leaves the repository with no extra refs and no
    stray files beyond what the wrapped primitive's own ref implies."""
    def snapshot_refs() -> str:
        return _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout

    def snapshot_tree(root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts}

    refs_before = snapshot_refs()
    worktree_before = snapshot_tree(repo)
    cwd_before = snapshot_tree(tmp_path)

    session = CheckpointSession(CheckpointRef(repo, new_lifecycle_id()))
    first = _head(repo)
    session.establish(first)
    # Exactly one ref added, and it is the owned one.
    added = set(snapshot_refs().splitlines()) - set(refs_before.splitlines())
    assert added == {f"{session.ref_name} {first}"}

    session.delete()

    assert snapshot_refs() == refs_before
    assert snapshot_tree(repo) == worktree_before
    assert snapshot_tree(tmp_path) == cwd_before


def test_session_makes_no_git_calls_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every Git effect goes through the wrapped primitive. With a
    double in place, no subprocess is ever spawned."""
    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("checkpoint_session must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    ref = _RecordingRef()
    session = CheckpointSession(ref)
    session.establish(A)
    session.advance(B)
    session.delete()

    assert ref.calls == [("create", A), ("advance", A, B), ("delete", B)]


def test_session_public_surface_is_narrow() -> None:
    """No persistence, lock, reconciliation, event or Docker method has
    crept onto the session."""
    public = {name for name, _ in inspect.getmembers(CheckpointSession) if not name.startswith("_")}
    assert public == {
        "establish",
        "advance",
        "delete",
        "transition",
        "intent",
        "accepted_sha",
        "lifecycle_id",
        "ref_name",
    }
