"""The neutral worktree-absence observer (ADR 0004 Amendment 20 section 9):
fixed categories only, an inspection failure is never absence, and the
listing parser is shared unchanged with reconciliation."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from codeagent import worktree_absence as wa
from codeagent._lifecycle_fs import LifecycleFsError, LifecycleFsFailure
from codeagent.state_root import AdminScanResult, MaterializedLeafObservation

_ID = "a" * 32
_ROOT = "/state"
_EXPECTED = os.path.join(_ROOT, "worktrees", "k", _ID)


def _observe(*, paths=frozenset(), scan=AdminScanResult.NONE_FOUND, leaf=MaterializedLeafObservation.ABSENT):
    calls = []

    def registered(root):
        calls.append("list")
        if isinstance(paths, BaseException):
            raise paths
        return set(paths)

    def admin(common, lid):
        calls.append("admin")
        assert (common, lid) == ("/common", _ID)
        if isinstance(scan, BaseException):
            raise scan
        return scan

    def observe_leaf(repo_key, lid):
        calls.append("leaf")
        if isinstance(leaf, BaseException):
            raise leaf
        return leaf

    state_root = SimpleNamespace(path=_ROOT, observe_materialized_worktree_leaf=observe_leaf)
    result = wa.observe_worktree_absent(
        lifecycle_id=_ID,
        state_root=state_root,
        identity=SimpleNamespace(repo_key="k", canonical_common_dir="/common"),
        context=SimpleNamespace(working_tree_root="/repo"),
        registered_paths=registered,
        admin_scan=admin,
    )
    return result, calls


def test_confirmed_only_after_all_three_observations():
    result, calls = _observe()
    assert result == wa.AbsenceObservation(wa.Absence.CONFIRMED, None)
    assert calls == ["list", "admin", "leaf"]


_FS_UNAVAILABLE = LifecycleFsError(LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "x")


@pytest.mark.parametrize(
    "kwargs, result, calls",
    [
        (dict(paths=wa.WorktreeListingError("x")), wa.Absence.UNAVAILABLE, ["list"]),
        (dict(paths={_EXPECTED}), wa.Absence.PRESENT, ["list"]),
        (dict(scan=AdminScanResult.MATCH_FOUND), wa.Absence.PRESENT, ["list", "admin"]),
        (dict(scan=AdminScanResult.UNEXPECTED_SUBSTRATE), wa.Absence.PRESENT, ["list", "admin"]),
        (dict(scan=AdminScanResult.LIMIT_EXCEEDED), wa.Absence.UNAVAILABLE, ["list", "admin"]),
        (dict(scan=AdminScanResult.INSPECTION_FAILED), wa.Absence.UNAVAILABLE, ["list", "admin"]),
        (dict(scan=_FS_UNAVAILABLE), wa.Absence.UNAVAILABLE, ["list", "admin"]),
        (dict(scan=LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")), wa.Absence.UNAVAILABLE, ["list", "admin"]),
        (dict(leaf=MaterializedLeafObservation.UNKNOWN), wa.Absence.UNAVAILABLE, ["list", "admin", "leaf"]),
        (dict(leaf=_FS_UNAVAILABLE), wa.Absence.UNAVAILABLE, ["list", "admin", "leaf"]),
    ],
)
def test_every_non_confirmation_stops_at_its_first_failing_observation(kwargs, result, calls):
    observation, seen = _observe(**kwargs)
    assert observation.result is result and observation.detail
    assert seen == calls


@pytest.mark.parametrize("leaf", [m for m in MaterializedLeafObservation if m not in (
    MaterializedLeafObservation.ABSENT, MaterializedLeafObservation.UNKNOWN)])
def test_any_present_leaf_is_present(leaf):
    observation, _ = _observe(leaf=leaf)
    assert observation.result is wa.Absence.PRESENT


def test_an_unregistered_listing_of_other_paths_still_confirms():
    observation, _ = _observe(paths={"/elsewhere", _EXPECTED + "x"})
    assert observation.result is wa.Absence.CONFIRMED


def test_details_are_fixed_and_carry_no_input():
    # Each case is a non-confirmation, whose contract requires a fixed detail.
    for kwargs in (dict(paths={_EXPECTED}), dict(scan=AdminScanResult.MATCH_FOUND), dict(leaf=_FS_UNAVAILABLE)):
        observation, _ = _observe(**kwargs)
        assert observation.result is not wa.Absence.CONFIRMED
        assert observation.detail is not None
        assert _ID not in observation.detail and _ROOT not in observation.detail


def test_registered_paths_from_listing_rejects_any_duplicate_path():
    record = b"worktree /a\x00HEAD " + b"1" * 40 + b"\x00detached\x00\x00"
    assert wa.registered_paths_from_listing(record) == {"/a"}
    caught = None
    try:
        wa.registered_paths_from_listing(record + record)
    except wa.WorktreeListingError as exc:
        caught = exc
    assert caught is not None
