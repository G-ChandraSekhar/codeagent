"""Focused, Docker-free (except two real-subprocess lock tests) tests
for spike_s5.py's own harness logic -- not part of the main CodeAgent
test suite, not collected by pyproject.toml's `testpaths = ["tests"]`.
Run directly:

    pytest spikes/s5/test_spike_s5.py

These test the spike's pure/mockable logic (identity computation,
manifest atomicity, preflight validation, reconciliation ordering,
control-document evaluation, lock semantics). The real six-scenario
experiment (real Docker, real Git, real signals) is run manually and
its evidence retained separately under spikes/s5/evidence/.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import spike_s5 as s  # noqa: E402


SESSION = "abc123def456"
SOURCE_REPO = "/tmp/does-not-need-to-exist-for-these-tests"


# --------------------------------------------------------------------
# compute_identity / identity_mismatches / path containment / labels
# --------------------------------------------------------------------


def test_compute_identity_produces_expected_shape(tmp_path) -> None:
    token = "0123456789ab"
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, token)
    assert identity.run_token == token
    assert identity.lock_path == tmp_path.resolve() / "locks" / f"{token}.lock"
    assert identity.manifest_path == tmp_path.resolve() / "manifest" / f"{token}.json"
    assert identity.worktree_path == tmp_path.resolve() / "worktrees" / token
    assert identity.container_name == f"codeagent-spike-s5-{token}"
    assert identity.labels == {
        "codeagent.spike": "s5",
        "codeagent.s5_session": SESSION,
        "codeagent.s5_run": token,
    }


def test_compute_identity_is_deterministic(tmp_path) -> None:
    token = "0123456789ab"
    a = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, token)
    b = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, token)
    assert a == b


@pytest.mark.parametrize("bad_token", ["", "short", "UPPERCASE12", "has-dash1234", "0123456789abZ", "0" * 11, "0" * 13])
def test_compute_identity_rejects_malformed_run_token(tmp_path, bad_token: str) -> None:
    with pytest.raises(ValueError):
        s.compute_identity(tmp_path, SOURCE_REPO, SESSION, bad_token)


def test_identity_mismatches_detects_each_field_independently(tmp_path) -> None:
    token = "0123456789ab"
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, token)
    good_manifest = {
        "lock_path": str(identity.lock_path),
        "worktree_path": str(identity.worktree_path),
        "container_name": identity.container_name,
        "labels": identity.labels,
        "source_repo_path": identity.source_repo_path,
    }
    assert s.identity_mismatches(identity, good_manifest) == []

    for field in ("lock_path", "worktree_path", "container_name", "labels", "source_repo_path"):
        tampered = dict(good_manifest)
        tampered[field] = "tampered-value"
        assert field in s.identity_mismatches(identity, tampered)


def test_path_is_contained_rejects_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert s.path_is_contained(root / "worktrees" / "x", root) is True
    assert s.path_is_contained(tmp_path / "outside", root) is False
    assert s.path_is_contained(root / ".." / "escaped", root) is False


def test_labels_match_subset_and_exact() -> None:
    expected = {"codeagent.spike": "s5", "codeagent.s5_run": "abc"}
    observed_extra = {**expected, "unrelated": "x"}
    assert s.labels_match_subset(observed_extra, expected) is True
    assert s.labels_match_exact(observed_extra, expected) is True  # "unrelated" is filtered, not codeagent.*
    assert s.labels_match_subset({}, expected) is False
    assert s.labels_match_exact({}, {}) is True


# --------------------------------------------------------------------
# Atomic manifest read/write
# --------------------------------------------------------------------


def test_atomic_write_and_read_round_trip(tmp_path) -> None:
    path = tmp_path / "manifest" / "abc.json"
    s.atomic_write_json(path, {"state": "READY", "n": 1})
    assert s.read_manifest(path) == {"state": "READY", "n": 1}
    assert not (path.parent / (path.name + ".tmp")).exists()  # renamed away


def test_read_manifest_raises_on_missing_file(tmp_path) -> None:
    with pytest.raises(s.ManifestCorruptError):
        s.read_manifest(tmp_path / "missing.json")


def test_read_manifest_raises_on_malformed_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    with pytest.raises(s.ManifestCorruptError):
        s.read_manifest(path)


def test_safe_read_manifest_returns_none_on_corruption(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert s.safe_read_manifest(path) is None


# --------------------------------------------------------------------
# evaluate_control_document: fail-closed control-action evaluation
# --------------------------------------------------------------------


def test_evaluate_control_document_absent() -> None:
    assert s.evaluate_control_document(None, "tok", False) == ("absent", None, None)


def test_evaluate_control_document_accepts_continue_and_cancel() -> None:
    assert s.evaluate_control_document({"run_token": "tok", "action": "continue"}, "tok", False) == (
        "accept",
        "continue",
        None,
    )
    assert s.evaluate_control_document({"run_token": "tok", "action": "cancel"}, "tok", False) == (
        "accept",
        "cancel",
        None,
    )


def test_evaluate_control_document_rejects_malformed() -> None:
    outcome, action, reason = s.evaluate_control_document({"action": "continue"}, "tok", False)
    assert outcome == "reject" and action is None and reason == "malformed"


def test_evaluate_control_document_rejects_foreign_run_token() -> None:
    outcome, action, reason = s.evaluate_control_document({"run_token": "other", "action": "continue"}, "tok", False)
    assert outcome == "reject" and reason == "foreign_run_token"


def test_evaluate_control_document_rejects_unsupported_action() -> None:
    outcome, action, reason = s.evaluate_control_document({"run_token": "tok", "action": "explode"}, "tok", False)
    assert outcome == "reject" and reason == "unsupported_action"


def test_evaluate_control_document_rejects_duplicate() -> None:
    outcome, action, reason = s.evaluate_control_document({"run_token": "tok", "action": "continue"}, "tok", True)
    assert outcome == "reject" and reason == "duplicate"


# --------------------------------------------------------------------
# preflight(): all combinations of live observation, independent of
# manifest flags; mismatch/technical-failure classification.
# --------------------------------------------------------------------


def _identity(tmp_path):
    return s.compute_identity(tmp_path, SOURCE_REPO, SESSION, "0123456789ab")


def _base_manifest(identity) -> dict:
    return {
        "lock_path": str(identity.lock_path),
        "worktree_path": str(identity.worktree_path),
        "container_name": identity.container_name,
        "labels": identity.labels,
        "source_repo_path": identity.source_repo_path,
        "container_id": None,
        "worktree_created": True,
        "container_created": True,
    }


def test_preflight_neither_present(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert pf.ok and pf.worktree_action == "none" and pf.container_action == "none"


def test_preflight_worktree_only(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert pf.ok and pf.worktree_action == "remove" and pf.container_action == "none"


def test_preflight_container_only(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    pf = s.preflight(identity, _base_manifest(identity))
    assert pf.ok and pf.worktree_action == "none" and pf.container_action == "remove"


def test_preflight_both_present(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    pf = s.preflight(identity, _base_manifest(identity))
    assert pf.ok and pf.worktree_action == "remove" and pf.container_action == "remove"


@pytest.mark.parametrize(
    "worktree_created_flag,container_created_flag",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_preflight_ignores_stale_flags_worktree_present_flag_false(
    tmp_path, monkeypatch, worktree_created_flag, container_created_flag
) -> None:
    """Correction 2/6: live observation controls action regardless of
    what the manifest's own flags claim."""
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    manifest = _base_manifest(identity)
    manifest["worktree_created"] = worktree_created_flag
    manifest["container_created"] = container_created_flag
    pf = s.preflight(identity, manifest)
    # Both resources are LIVE, so both must be scheduled for removal
    # no matter what the (possibly stale) flags say.
    assert pf.ok and pf.worktree_action == "remove" and pf.container_action == "remove"


def test_preflight_flag_true_but_resource_absent(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    manifest = _base_manifest(identity)
    manifest["worktree_created"] = True
    manifest["container_created"] = True
    pf = s.preflight(identity, manifest)
    assert pf.ok and pf.worktree_action == "none" and pf.container_action == "none"


def test_preflight_worktree_registration_directory_mismatch_is_mismatch(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert not pf.ok and pf.worktree_action == "mismatch"


def test_preflight_unregistered_but_directory_exists_is_mismatch(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=True))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert not pf.ok and pf.worktree_action == "mismatch"


def test_preflight_container_label_mismatch_is_mismatch_never_removed(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "s5"})
    )
    pf = s.preflight(identity, _base_manifest(identity))
    assert not pf.ok and pf.container_action == "mismatch"


def test_preflight_container_id_mismatch_is_mismatch(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="wrong-id", labels=identity.labels)
    )
    manifest = _base_manifest(identity)
    manifest["container_id"] = "expected-id"
    pf = s.preflight(identity, manifest)
    assert not pf.ok and pf.container_action == "mismatch"


def test_preflight_container_id_null_allowed_when_labels_match(tmp_path, monkeypatch) -> None:
    """SIGKILL immediately after `docker create` can leave container_id
    null in the manifest -- discovery by exact name + label match alone
    must still be allowed."""
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="any-id", labels=identity.labels)
    )
    manifest = _base_manifest(identity)
    manifest["container_id"] = None
    pf = s.preflight(identity, manifest)
    assert pf.ok and pf.container_action == "remove"


def test_preflight_worktree_path_escape_is_mismatch(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    object.__setattr__(identity, "worktree_path", Path("/etc/escaped"))  # simulate a tampered manifest identity
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert not pf.ok and pf.worktree_action == "mismatch"


def test_preflight_inspection_failure_is_not_ok(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=False, error="git failed"))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    pf = s.preflight(identity, _base_manifest(identity))
    assert not pf.ok


# --------------------------------------------------------------------
# process_entry: full lock -> preflight -> ordered mutation -> state
# --------------------------------------------------------------------


def _make_dead_manifest(identity) -> dict:
    m = _base_manifest(identity)
    m.update({"state": "READY", "reconciliation_attempts": 0, "failure_history": []})
    return m


def test_process_entry_neither_resource_reconciled_with_zero_removal_calls(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)

    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciled"
    assert calls == []
    assert s.read_manifest(identity.manifest_path)["state"] == "RECONCILED"


def test_process_entry_container_first_ordering(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)

    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciled"
    assert calls == ["container", "worktree"]


def test_process_entry_container_failure_short_circuits_worktree(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or False)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)

    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciliation_failed"
    assert result["phase"] == "post_mutation_cleanup"
    assert calls == ["container"]  # worktree removal never attempted
    manifest = s.read_manifest(identity.manifest_path)
    assert manifest["state"] == "RECONCILIATION_FAILED"
    assert manifest["worktree_removal_skipped"] is True


def test_process_entry_worktree_failure_after_container_success(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: False)

    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciliation_failed"
    assert result["phase"] == "post_mutation_cleanup"
    assert s.read_manifest(identity.manifest_path)["state"] == "RECONCILIATION_FAILED"


def test_process_entry_preflight_failure_mutates_neither_resource(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=True, directory_exists=True))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "s5"})
    )
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)

    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciliation_failed"
    assert result["phase"] == "pre_mutation_validation"
    assert calls == []
    manifest = s.read_manifest(identity.manifest_path)
    assert manifest["state"] == "RECONCILIATION_FAILED"


def test_process_entry_snapshot_is_original_not_orphaned(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    manifest = _make_dead_manifest(identity)
    result = s.process_entry(identity, manifest)
    assert result["snapshot"]["state"] == "READY"  # original, not ORPHANED/RECONCILING


def test_process_entry_snapshot_is_deep_copy_not_aliased(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    manifest = _make_dead_manifest(identity)
    manifest["failure_history"] = []
    result = s.process_entry(identity, manifest)
    snapshot = result["snapshot"]
    # Mutate the returned manifest's list; the retained snapshot must be unaffected.
    result["manifest"]["failure_history"].append({"tampered": True})
    assert snapshot["failure_history"] == []


def test_process_entry_active_lock_is_skipped(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    fd = s.open_lock_file(identity.lock_path)
    assert s.try_lock_nonblocking(fd) is True
    try:
        calls = []
        monkeypatch.setattr(s, "observe_worktree", lambda repo, path: calls.append("wt") or s.WorktreeObservation(ok=True))
        monkeypatch.setattr(s, "observe_container", lambda name: calls.append("c") or s.ContainerObservation(ok=True, present=False))
        result = s.process_entry(identity, _make_dead_manifest(identity))
        assert result["action"] == "skipped_active"
        assert calls == []  # no inspection at all while lock is held
    finally:
        s.release_lock(fd)


def test_process_entry_retry_preserves_failure_history_and_increments_attempts(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "s5"})
    )
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))

    manifest = _make_dead_manifest(identity)
    first = s.process_entry(identity, manifest)
    assert first["action"] == "reconciliation_failed"
    after_first = s.read_manifest(identity.manifest_path)
    assert after_first["reconciliation_attempts"] == 1
    assert len(after_first["failure_history"]) == 1

    # Second pass: same broken container identity -> fails again, history grows.
    second = s.process_entry(identity, after_first)
    assert second["action"] == "reconciliation_failed"
    after_second = s.read_manifest(identity.manifest_path)
    assert after_second["reconciliation_attempts"] == 2
    assert len(after_second["failure_history"]) == 2
    assert after_second["failure_history"][0] == after_first["failure_history"][0]  # preserved, not overwritten


def test_process_entry_retry_can_succeed_after_prior_failure(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "s5"})
    )
    manifest = _make_dead_manifest(identity)
    first = s.process_entry(identity, manifest)
    assert first["action"] == "reconciliation_failed"
    after_first = s.read_manifest(identity.manifest_path)

    # Now the container's labels "heal" (e.g. corrected out-of-band) -- retry succeeds.
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels=identity.labels)
    )
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)
    second = s.process_entry(identity, after_first)
    assert second["action"] == "reconciled"
    final = s.read_manifest(identity.manifest_path)
    assert final["state"] == "RECONCILED"
    assert final["failure_history"] == after_first["failure_history"]  # still preserved


# --------------------------------------------------------------------
# reconcile(): top-level orchestration
# --------------------------------------------------------------------


def _write_manifest_direct(identity, **fields) -> None:
    base = _base_manifest(identity)
    base.update({"reconciliation_attempts": 0, "failure_history": []})
    base.update(fields)
    s.atomic_write_json(identity.manifest_path, base)


def test_reconcile_skips_complete_with_zero_lock_or_inspection(tmp_path, monkeypatch) -> None:
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, "aaaaaaaaaaaa")
    _write_manifest_direct(identity, state="COMPLETE")

    def _fail_if_called(*a, **k):
        raise AssertionError("must not be called for a terminal entry")

    monkeypatch.setattr(s, "open_lock_file", _fail_if_called)
    monkeypatch.setattr(s, "observe_worktree", _fail_if_called)
    monkeypatch.setattr(s, "observe_container", _fail_if_called)

    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "skipped_terminal"
    assert s.read_manifest(identity.manifest_path)["state"] == "COMPLETE"  # unchanged


def test_reconcile_skips_reconciled_with_zero_lock_or_inspection(tmp_path, monkeypatch) -> None:
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, "bbbbbbbbbbbb")
    _write_manifest_direct(identity, state="RECONCILED")

    def _fail_if_called(*a, **k):
        raise AssertionError("must not be called for a terminal entry")

    monkeypatch.setattr(s, "open_lock_file", _fail_if_called)
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "skipped_terminal"


@pytest.mark.parametrize("scenario_state", ["COMPLETE"])
def test_complete_manifest_cannot_be_converted_by_repeated_reconciliation(tmp_path, scenario_state) -> None:
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, "cccccccccccc")
    _write_manifest_direct(identity, state=scenario_state)
    for _ in range(3):
        s.reconcile(tmp_path, SOURCE_REPO, SESSION)
        assert s.read_manifest(identity.manifest_path)["state"] == scenario_state


def test_reconcile_reports_technical_failure_for_malformed_manifest_json(tmp_path) -> None:
    manifest_dir = tmp_path.resolve() / "manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "dddddddddddd.json").write_text("{not json")
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "technical_failure"


def test_reconcile_reports_technical_failure_for_malformed_run_token_filename(tmp_path) -> None:
    manifest_dir = tmp_path.resolve() / "manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "not-a-valid-token.json").write_text("{}")
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "technical_failure"


def test_reconcile_reports_technical_failure_for_tampered_identity_field(tmp_path, monkeypatch) -> None:
    identity = s.compute_identity(tmp_path, SOURCE_REPO, SESSION, "eeeeeeeeeeee")
    _write_manifest_direct(identity, state="READY", container_name="tampered-name")

    def _fail_if_called(*a, **k):
        raise AssertionError("must not attempt a lock on a tampered entry")

    monkeypatch.setattr(s, "open_lock_file", _fail_if_called)
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "technical_failure"


def test_reconcile_records_stray_tmp_without_authority(tmp_path) -> None:
    manifest_dir = tmp_path.resolve() / "manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "ffffffffffff.json.tmp").write_text('{"state": "PREPARING"}')
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["results"][0]["action"] == "informational_leftover_tmp"


def test_reconcile_directory_listing_failure_is_technical_failure(tmp_path, monkeypatch) -> None:
    manifest_dir = tmp_path.resolve() / "manifest"
    manifest_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError("boom")))
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    assert result["ok"] is False


# --------------------------------------------------------------------
# reconciliation_attempts: incremented exactly once per acquired
# attempt, including a first-attempt success; failure_history is
# preserved unchanged across a later successful retry.
# --------------------------------------------------------------------


def test_reconcile_attempts_is_one_on_ordinary_first_attempt_success(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["action"] == "reconciled"
    assert result["manifest"]["reconciliation_attempts"] == 1


def test_reconcile_attempts_increments_exactly_once_per_retry(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(
        s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "s5"})
    )
    manifest = _make_dead_manifest(identity)
    for expected_attempts in (1, 2, 3):
        result = s.process_entry(identity, manifest)
        assert result["action"] == "reconciliation_failed"
        manifest = s.read_manifest(identity.manifest_path)
        assert manifest["reconciliation_attempts"] == expected_attempts


def test_reconcile_attempts_does_not_double_count_across_orphaned_reconciling_reconciled(tmp_path, monkeypatch) -> None:
    """A single acquired attempt writes ORPHANED -> RECONCILING ->
    RECONCILED (three writes), but must only count as ONE attempt."""
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    result = s.process_entry(identity, _make_dead_manifest(identity))
    assert result["manifest"]["reconciliation_attempts"] == 1


# --------------------------------------------------------------------
# reconcile_run_has_problem: the standalone --reconcile command's exit
# status must fail loudly on technical_failure/reconciliation_failed
# while treating skipped_terminal/skipped_active/reconciled as success.
# --------------------------------------------------------------------


def test_reconcile_run_has_problem_false_for_all_successful_actions() -> None:
    result = {
        "ok": True,
        "results": [
            {"action": "skipped_terminal"},
            {"action": "skipped_active"},
            {"action": "reconciled"},
            {"action": "informational_leftover_tmp"},
        ],
    }
    assert s.reconcile_run_has_problem(result) is False


def test_reconcile_run_has_problem_true_for_technical_failure() -> None:
    result = {"ok": True, "results": [{"action": "skipped_terminal"}, {"action": "technical_failure"}]}
    assert s.reconcile_run_has_problem(result) is True


def test_reconcile_run_has_problem_true_for_reconciliation_failed() -> None:
    result = {"ok": True, "results": [{"action": "reconciliation_failed"}]}
    assert s.reconcile_run_has_problem(result) is True


def test_reconcile_run_has_problem_true_when_listing_itself_failed() -> None:
    result = {"ok": False, "error": "cannot list manifest directory", "results": []}
    assert s.reconcile_run_has_problem(result) is True


def test_reconcile_command_exits_nonzero_on_technical_failure(tmp_path) -> None:
    manifest_dir = tmp_path.resolve() / "manifest"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "not-a-valid-token.json").write_text("{}")
    proc = subprocess.run(
        [sys.executable, str(s.THIS_FILE), "--reconcile", str(tmp_path), SOURCE_REPO, SESSION],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    parsed = json.loads(proc.stdout)  # still parseable structured evidence despite the nonzero exit
    assert parsed["results"][0]["action"] == "technical_failure"


def test_reconcile_command_exits_zero_when_nothing_to_do(tmp_path) -> None:
    proc = subprocess.run(
        [sys.executable, str(s.THIS_FILE), "--reconcile", str(tmp_path), SOURCE_REPO, SESSION],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ok": True, "results": []}


# --------------------------------------------------------------------
# Fail-closed child lifecycle (Correction 1): perform_child_cleanup is
# pulled out of child_main specifically so these are directly testable
# without driving the full control-file/signal wait loop.
# --------------------------------------------------------------------


def _child_manifest(identity) -> dict:
    return {
        "run_token": identity.run_token,
        "session_id": identity.session_id,
        "source_repo_path": identity.source_repo_path,
        "lock_path": str(identity.lock_path),
        "worktree_path": str(identity.worktree_path),
        "container_name": identity.container_name,
        "container_id": "cid",
        "labels": identity.labels,
        "pid": 12345,
        "scenario": "normal_completion",
        "worktree_created": True,
        "container_created": True,
        "reconciliation_attempts": 0,
        "failure_history": [],
        "created_at": s.now_iso(),
        "state": "READY",
    }


def test_perform_child_cleanup_container_removed_before_worktree(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)
    rc = s.perform_child_cleanup(identity, _child_manifest(identity), "continue")
    assert rc == 0
    assert calls == ["container", "worktree"]
    assert s.read_manifest(identity.manifest_path)["state"] == "COMPLETE"


def test_perform_child_cleanup_container_failure_never_produces_complete_or_touches_worktree(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    calls = []
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: calls.append("container") or False)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: calls.append("worktree") or True)
    rc = s.perform_child_cleanup(identity, _child_manifest(identity), "continue")
    assert rc != 0
    assert calls == ["container"]  # worktree removal never attempted
    manifest = s.read_manifest(identity.manifest_path)
    assert manifest["state"] != "COMPLETE"
    assert manifest["state"] not in s.TERMINAL_STATES  # never permanently unreconcilable
    assert manifest["failure_phase"] == "cleanup_container_failed"


def test_perform_child_cleanup_worktree_failure_never_produces_complete(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: False)
    rc = s.perform_child_cleanup(identity, _child_manifest(identity), "continue")
    assert rc != 0
    manifest = s.read_manifest(identity.manifest_path)
    assert manifest["state"] != "COMPLETE"
    assert manifest["state"] not in s.TERMINAL_STATES
    assert manifest["failure_phase"] == "cleanup_worktree_failed"


def test_perform_child_cleanup_failure_state_is_not_permanently_skipped_by_reconciliation(tmp_path, monkeypatch) -> None:
    """A child that failed to clean up must still be reachable by a
    later reconciliation pass -- never permanently skipped."""
    identity = _identity(tmp_path)
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: False)
    monkeypatch.setattr(s, "remove_worktree_and_confirm", lambda repo, path: True)
    s.perform_child_cleanup(identity, _child_manifest(identity), "continue")

    # Now a fresh reconciliation pass must actually process this entry
    # (not skip it as terminal) -- it's dead (no lock held), so it
    # should be picked up and independently inspected/resolved.
    monkeypatch.setattr(s, "observe_worktree", lambda repo, path: s.WorktreeObservation(ok=True, registered=False, directory_exists=False))
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    result = s.reconcile(tmp_path, SOURCE_REPO, SESSION)
    entry = result["results"][0]
    assert entry["action"] not in ("skipped_terminal",)
    assert entry["action"] == "reconciled"  # both resources genuinely absent by the time reconciliation ran


def test_child_docker_start_failure_leaves_container_created_never_ready_or_complete(tmp_path, monkeypatch) -> None:
    """Drives the real child_main through PREPARING -> ... ->
    CONTAINER_CREATED with every subprocess call mocked, then fails
    `docker start` -- must never reach READY or COMPLETE."""

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        if "rev-parse" in joined:
            return _FakeCompleted(0, stdout="deadbeefcafefeed\n")
        if "worktree" in joined and "add" in joined:
            return _FakeCompleted(0)
        if "docker" in argv and "create" in argv:
            return _FakeCompleted(0, stdout="containeridabc123\n")
        if "docker" in argv and "start" in argv:
            return _FakeCompleted(1, stderr="simulated start failure")
        return _FakeCompleted(0)

    monkeypatch.setattr(s, "run", fake_run)
    scratch_root = tmp_path
    token = "aaaaaaaaaaaa"
    rc = s.child_main("normal_completion", SESSION, token, scratch_root, SOURCE_REPO)
    assert rc != 0

    identity = s.compute_identity(scratch_root, SOURCE_REPO, SESSION, token)
    manifest = s.read_manifest(identity.manifest_path)
    assert manifest["state"] == "CONTAINER_CREATED"
    assert manifest["state"] not in ("READY", "COMPLETE")
    assert manifest["failure_phase"] == "start_failed"
    assert manifest["container_id"] == "containeridabc123"  # persisted before start was ever attempted


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --------------------------------------------------------------------
# Real-lock tests: mutual exclusion (same-process, tests the
# reconciler-vs-reconciler exclusion mechanism only) and a genuine
# cross-process test (separate dedicated subprocess -- the only thing
# that actually proves cross-process/SIGKILL lifecycle behavior).
# --------------------------------------------------------------------


def test_flock_mutual_exclusion_same_process_two_descriptors(tmp_path) -> None:
    """Tests the locking primitive's mutual-exclusion property itself
    (relevant to two reconcilers racing) -- NOT a claim about
    cross-process child-liveness, which is covered separately below."""
    lock_path = tmp_path / "excl.lock"
    fd1 = s.open_lock_file(lock_path)
    fd2 = s.open_lock_file(lock_path)
    try:
        assert s.try_lock_nonblocking(fd1) is True
        assert s.try_lock_nonblocking(fd2) is False  # second reconciler cannot proceed
        s.release_lock(fd1)
        assert s.try_lock_nonblocking(fd2) is True
    finally:
        try:
            s.release_lock(fd2)
        except Exception:
            pass


def test_flock_cross_process_busy_while_child_lives_then_acquirable_after_sigkill(tmp_path) -> None:
    lock_path = tmp_path / "cross.lock"
    acquired_marker = Path(str(lock_path) + ".acquired")
    proc = subprocess.Popen([sys.executable, str(s.THIS_FILE), "--hold-lock", str(lock_path)])
    try:
        # Wait for the child's own confirmation that IT has acquired the
        # lock, before asserting anything about busy-ness -- otherwise
        # the parent's first probe can race the child's startup and
        # spuriously acquire the still-free lock itself.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not acquired_marker.exists():
            time.sleep(0.02)
        assert acquired_marker.exists(), "dedicated child never confirmed acquiring the lock"

        probe = s.open_lock_file(lock_path)
        busy = not s.try_lock_nonblocking(probe)
        if not busy:
            s.release_lock(probe)
        else:
            probe.close()
        assert busy, "lock must be busy while the dedicated child holds it"

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

        probe2 = s.open_lock_file(lock_path)
        assert s.try_lock_nonblocking(probe2) is True, "lock must be acquirable once that exact child is dead"
        s.release_lock(probe2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------
# Emergency-cleanup identity logic (container-kind entries only --
# no Docker required, observe_container is monkeypatched).
# --------------------------------------------------------------------


def test_emergency_cleanup_removes_matching_entry(monkeypatch) -> None:
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={}))
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)
    inventory = [{"kind": "container", "name": "x", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["all_clean"] is True
    assert result["results"][0]["action"] == "removed"


def test_emergency_cleanup_refuses_on_id_drift(monkeypatch) -> None:
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="different-id", labels={}))
    inventory = [{"kind": "container", "name": "x", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["results"][0]["action"] == "refused_identity_drift"
    assert result["all_clean"] is False


def test_emergency_cleanup_refuses_on_label_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        s,
        "observe_container",
        lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={"codeagent.spike": "unexpected"}),
    )
    inventory = [{"kind": "container", "name": "x", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["results"][0]["action"] == "refused_identity_drift"


def test_emergency_cleanup_unlabeled_canary_matches_empty_expected(monkeypatch) -> None:
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=True, id="cid", labels={}))
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)
    inventory = [{"kind": "container", "name": "unlabeled", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["results"][0]["action"] == "removed"  # no session label required


def test_emergency_cleanup_already_absent_is_clean(monkeypatch) -> None:
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=True, present=False))
    inventory = [{"kind": "container", "name": "x", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["results"][0]["action"] == "already_absent"
    assert result["all_clean"] is True


def test_emergency_cleanup_inspection_failure_is_technical_failure(monkeypatch) -> None:
    monkeypatch.setattr(s, "observe_container", lambda name: s.ContainerObservation(ok=False, present=False, error="boom"))
    inventory = [{"kind": "container", "name": "x", "id": "cid", "expected_labels": {}}]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)
    assert result["results"][0]["action"] == "technical_failure"
    assert result["all_clean"] is False


# --------------------------------------------------------------------
# Unconditional outer-harness cleanup (Correction: run_full_experiment
# must still run emergency cleanup, targeting only its own recorded
# inventory, when an unexpected exception occurs mid-run -- and must
# never let that turn into overall_verdict PASS). Fully Docker/Git-free
# via a small in-memory fake Docker registry driven through the single
# `s.run` seam.
# --------------------------------------------------------------------


class _FakeDockerRegistry:
    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}

    def create(self, name: str, labels: dict) -> None:
        self.containers[name] = {"Id": f"fakeid-{name}", "Config": {"Labels": dict(labels)}}

    def rm(self, name: str) -> None:
        self.containers.pop(name, None)

    def ps_names_text(self) -> str:
        return "\n".join(self.containers.keys())

    def inspect_json(self, name: str) -> str | None:
        if name not in self.containers:
            return None
        return json.dumps([self.containers[name]])


def _parse_labels_from_argv(argv: list[str]) -> dict:
    labels = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--label":
            k, v = argv[i + 1].split("=", 1)
            labels[k] = v
            i += 2
        else:
            i += 1
    return labels


def _make_fake_docker_git_run(registry: "_FakeDockerRegistry", fail_on_container_name: str):
    """A single fake for `s.run` covering every git/docker invocation
    `run_full_experiment` makes during canary setup, plus everything
    `emergency_cleanup`'s own observe/remove helpers need -- no real
    Docker daemon or git binary behavior is relied on beyond argv
    shape. `fail_on_container_name` simulates one canary's `docker run
    -d` failing outright (nonzero exit), which is exactly how
    `docker_run_canary` already turns a real Docker failure into a
    raised RuntimeError -- no separate fault-injection hook is needed."""

    def fake_run(argv, **kw):
        if argv[0] == "git":
            if "rev-parse" in argv:
                return _FakeCompleted(0, stdout="deadbeefcafefeed\n")
            if "status" in argv:
                return _FakeCompleted(0, stdout="")
            if "worktree" in argv and "list" in argv:
                return _FakeCompleted(0, stdout="worktree /fake/fixture-repo\nHEAD deadbeef\nbranch refs/heads/main\n\n")
            return _FakeCompleted(0)  # init / config / add / commit
        if argv[0] == "docker":
            if "version" in argv:
                return _FakeCompleted(0, stdout="99.0.0\n")
            if argv[1] == "info":
                return _FakeCompleted(0, stdout='"2"\n')
            if argv[1] == "pull":
                return _FakeCompleted(0)
            if argv[1:3] == ["run", "-d"]:
                name = argv[argv.index("--name") + 1]
                if name == fail_on_container_name:
                    return _FakeCompleted(1, stderr="simulated canary creation failure")
                registry.create(name, _parse_labels_from_argv(argv))
                return _FakeCompleted(0, stdout=f"fakeid-{name}\n")
            if argv[1] == "ps":
                names = registry.ps_names_text()
                return _FakeCompleted(0, stdout=(names + "\n") if names else "")
            if argv[1] == "inspect":
                data = registry.inspect_json(argv[2])
                if data is None:
                    return _FakeCompleted(1, stderr="no such container")
                return _FakeCompleted(0, stdout=data)
            if argv[1] == "rm":
                registry.rm(argv[-1])
                return _FakeCompleted(0)
        return _FakeCompleted(0)

    return fake_run


def test_run_full_experiment_unconditional_cleanup_on_exception(tmp_path, monkeypatch) -> None:
    """Canary C's creation fails outright (simulating an unexpected
    Docker failure). Canaries A and B are already real (fake) Docker
    resources by that point. The exception must still trigger
    emergency cleanup targeting exactly A and B (never a name/label
    sweep), the scratch root must still be removed, the original
    exception must propagate, and no PASS can be produced."""
    registry = _FakeDockerRegistry()
    canary_c_name_holder: dict = {}

    # Intercept docker_run_canary's third call (canary C) by name
    # pattern: canary C's name always starts with "codeagent-spike-s5-".
    # We don't know its exact random suffix ahead of time, so the fake
    # `run` fails the FIRST "docker run -d" call whose --name starts
    # with that prefix, which is deterministically canary C given
    # creation order (A, B, C, then D's child subprocess separately).
    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1:3] == ["run", "-d"]:
            name = argv[argv.index("--name") + 1]
            if name.startswith("codeagent-spike-s5-") and "fail" not in canary_c_name_holder:
                canary_c_name_holder["fail"] = name
                return _FakeCompleted(1, stderr="simulated canary C creation failure")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake harness content for this test only\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)

    original_emergency_cleanup = s.emergency_cleanup
    cleanup_calls: list[list[dict]] = []

    def spying_emergency_cleanup(inventory, source_repo):
        cleanup_calls.append(copy.deepcopy([{"kind": e["kind"], "name": e.get("name")} for e in inventory]))
        return original_emergency_cleanup(inventory, source_repo)

    monkeypatch.setattr(s, "emergency_cleanup", spying_emergency_cleanup)

    with pytest.raises(RuntimeError, match="canary"):
        s.run_full_experiment()

    # Cleanup ran exactly once, unconditionally, with exactly the two
    # resources actually recorded before the exception (A and B) --
    # canary C was never appended to inventory since it never
    # succeeded, and D's child was never even launched.
    assert len(cleanup_calls) == 1
    recorded_kinds_names = cleanup_calls[0]
    assert len(recorded_kinds_names) == 2
    assert all(e["kind"] == "container" for e in recorded_kinds_names)

    # The real emergency_cleanup ran against the fake registry and
    # actually removed both recorded resources -- proving cleanup
    # targets exactly the recorded inventory, not a name/label sweep
    # (canary C's name was never in the registry to begin with).
    assert registry.containers == {}

    # Evidence was still written despite the raised exception, and
    # overall_verdict can never be PASS.
    evidence_dirs = list((fake_spike_dir / "evidence").rglob("summary.json"))
    assert len(evidence_dirs) == 1
    summary = json.loads(evidence_dirs[0].read_text())
    assert summary["overall_verdict"] != "PASS"
    assert summary["experiment_exception"] is not None
    assert "canary" in summary["experiment_exception"]

    emergency_evidence = json.loads((evidence_dirs[0].parent / "emergency_cleanup_results.json").read_text())
    assert emergency_evidence["all_clean"] is True
    assert len(emergency_evidence["results"]) == 2


class _FakePopen:
    """A minimal stand-in for subprocess.Popen: reports as already
    exited, so emergency cleanup's `proc.wait(timeout=...)` returns
    immediately rather than actually running a child process."""

    _next_pid = 9000

    def __init__(self) -> None:
        _FakePopen._next_pid += 1
        self.pid = _FakePopen._next_pid
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        pass

    def send_signal(self, sig) -> None:
        pass


def test_canary_inventory_snapshot_is_independent_of_later_inventory_growth() -> None:
    """Regression test for a real bug found by the fifth live macOS
    run: `inventory` (used for emergency cleanup) keeps growing as
    scenario/sigkill children are appended to it after canary setup,
    but the CANARY snapshot comparison must only ever compare the
    original four canaries -- never spuriously report `canaries_
    unchanged: False` just because unrelated, later-appended entries
    exist in the (different, growing) `inventory` list. This mirrors
    `run_full_experiment`'s own `canary_inventory = list(inventory)`
    snapshot-at-a-point-in-time technique."""
    inventory = [{"kind": "container", "name": "canary-a", "id": "1", "expected_labels": {}}]
    canary_inventory = list(inventory)

    # Simulate scenario/sigkill children being appended to `inventory`
    # AFTER the canary snapshot was taken -- exactly what happens for
    # real between canary D's setup and the scenario loop.
    inventory.append({"kind": "container", "name": "scenario-child-container", "id": "2", "expected_labels": {}})

    assert canary_inventory == [{"kind": "container", "name": "canary-a", "id": "1", "expected_labels": {}}]
    assert len(inventory) == 2
    assert len(canary_inventory) == 1


# --------------------------------------------------------------------
# Real try/except/finally: a failure writing evidence during
# finalization (a "secondary failure") must never replace the original
# experiment exception, and must never prevent LATER finalization
# phases from running.
# --------------------------------------------------------------------


def _run_full_experiment_with_canary_failure_and_broken_write(tmp_path, monkeypatch, broken_filename: str) -> Path:
    """Shared setup: canary C's creation fails (a genuine ORIGINAL
    experiment exception), AND writing `broken_filename` as evidence
    also raises during finalization -- proving the original exception
    is what the caller observes, never the secondary write failure."""
    registry = _FakeDockerRegistry()
    canary_c_name_holder: dict = {}

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1:3] == ["run", "-d"]:
            name = argv[argv.index("--name") + 1]
            if name.startswith("codeagent-spike-s5-") and "fail" not in canary_c_name_holder:
                canary_c_name_holder["fail"] = name
                return _FakeCompleted(1, stderr="simulated canary C creation failure")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake harness content for this test only\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)

    original_write_text = Path.write_text

    def faulty_write_text(self, data, *a, **kw):
        if self.name == broken_filename:
            raise OSError(f"simulated disk failure writing {broken_filename}")
        return original_write_text(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", faulty_write_text)

    with pytest.raises(RuntimeError, match="canary"):
        s.run_full_experiment()

    return fake_spike_dir


def test_run_full_experiment_survives_broken_emergency_cleanup_evidence_write(tmp_path, monkeypatch) -> None:
    fake_spike_dir = _run_full_experiment_with_canary_failure_and_broken_write(
        tmp_path, monkeypatch, "emergency_cleanup_results.json"
    )
    run_dir = next((fake_spike_dir / "evidence").rglob("summary.json")).parent

    # The broken write itself left no file...
    assert not (run_dir / "emergency_cleanup_results.json").exists()
    # ...but every LATER finalization phase still ran and wrote its evidence.
    assert (run_dir / "fixture_worktree_check.json").exists()
    assert (run_dir / "baseline_final_comparison.json").exists()
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "run.log").exists()

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["scratch_root_removed_confirmed"] is True  # scratch removal still attempted
    assert summary["baseline_final_listings_ok"] is True  # final inspection still attempted
    assert summary["overall_verdict"] != "PASS"
    # The ORIGINAL exception is what the caller/summary sees, not the write failure.
    assert "canary" in summary["experiment_exception"]
    assert any("emergency_cleanup_results.json" in f for f in summary["secondary_cleanup_failures"])


def test_run_full_experiment_survives_broken_fixture_worktree_evidence_write(tmp_path, monkeypatch) -> None:
    fake_spike_dir = _run_full_experiment_with_canary_failure_and_broken_write(
        tmp_path, monkeypatch, "fixture_worktree_check.json"
    )
    run_dir = next((fake_spike_dir / "evidence").rglob("summary.json")).parent

    assert not (run_dir / "fixture_worktree_check.json").exists()
    assert (run_dir / "emergency_cleanup_results.json").exists()  # earlier phase, unaffected
    assert (run_dir / "baseline_final_comparison.json").exists()  # later phase still ran
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "run.log").exists()

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["scratch_root_removed_confirmed"] is True
    assert summary["baseline_final_listings_ok"] is True
    assert summary["overall_verdict"] != "PASS"
    assert "canary" in summary["experiment_exception"]
    assert any("fixture_worktree_check.json" in f for f in summary["secondary_cleanup_failures"])


def test_run_full_experiment_survives_broken_summary_evidence_write_and_still_writes_run_log(
    tmp_path, monkeypatch
) -> None:
    fake_spike_dir = _run_full_experiment_with_canary_failure_and_broken_write(tmp_path, monkeypatch, "summary.json")
    run_dirs = list((fake_spike_dir / "evidence").rglob("emergency_cleanup_results.json"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0].parent

    assert not (run_dir / "summary.json").exists()
    assert (run_dir / "emergency_cleanup_results.json").exists()
    assert (run_dir / "fixture_worktree_check.json").exists()
    assert (run_dir / "baseline_final_comparison.json").exists()
    # run.log is phase 9, entirely separate from phase 8's summary
    # write -- it must still be attempted and must still succeed even
    # though the immediately preceding phase failed.
    assert (run_dir / "run.log").exists()
    assert "EXPERIMENT ERROR" in (run_dir / "run.log").read_text()


def test_run_full_experiment_survives_elog_failure_during_finalization_and_preserves_original_exception(
    tmp_path, monkeypatch
) -> None:
    """Correction: the `except BaseException` block now performs
    EXACTLY `experiment_exception = exc` and nothing else -- the
    original-exception log message is constructed inside the
    already-guarded phase-9 run-log block instead. This test forces an
    original canary-creation RuntimeError AND forces `elog` (via the
    builtin `print` it calls) to raise during phase 9, and proves:
    every EARLIER cleanup/final-inspection phase (1-8) still ran and
    wrote its evidence; the caller still receives the ORIGINAL canary
    RuntimeError, never anything related to the logging failure; and
    the logging failure is represented as a secondary failure -- since
    it occurs in the very last phase (after summary.json has already
    been durably written), the only artifact that can still reflect it
    is `run.log`'s own absence, which is exactly what is asserted
    below, rather than a fabricated claim that some later-written file
    contains the failure text."""
    registry = _FakeDockerRegistry()
    canary_c_name_holder: dict = {}

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1:3] == ["run", "-d"]:
            name = argv[argv.index("--name") + 1]
            if name.startswith("codeagent-spike-s5-") and "fail" not in canary_c_name_holder:
                canary_c_name_holder["fail"] = name
                return _FakeCompleted(1, stderr="simulated canary C creation failure")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake harness content for this test only\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)

    real_print = print

    def faulty_print(*args, **kwargs):
        text = args[0] if args else ""
        if "EXPERIMENT ERROR" in str(text):
            raise OSError("simulated elog/print failure during finalization")
        return real_print(*args, **kwargs)

    # Shadows the builtin `print` specifically within spike_s5's own
    # module namespace -- `elog`'s bare `print(text)` call resolves to
    # this, exactly like every other monkeypatch in this file relies on
    # module-global name resolution at call time.
    monkeypatch.setattr(s, "print", faulty_print, raising=False)

    with pytest.raises(RuntimeError, match="canary"):
        s.run_full_experiment()

    run_dir = next((fake_spike_dir / "evidence").rglob("summary.json")).parent

    # Every earlier phase (1-8) still ran and wrote its evidence.
    assert (run_dir / "emergency_cleanup_results.json").exists()
    assert (run_dir / "fixture_worktree_check.json").exists()
    assert (run_dir / "baseline_final_comparison.json").exists()
    assert (run_dir / "summary.json").exists()

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["scratch_root_removed_confirmed"] is True
    assert summary["baseline_final_listings_ok"] is True
    # The caller-visible exception is the ORIGINAL canary failure --
    # never the elog/print failure.
    assert "canary" in summary["experiment_exception"]
    assert "elog" not in summary["experiment_exception"]
    assert "print" not in summary["experiment_exception"]

    # Phase 9 itself failed before reaching its own write_evidence call
    # -- run.log's absence IS the retained evidence that this secondary
    # failure occurred and was recorded (in `secondary_cleanup_failures`
    # in-memory) rather than silently succeeding or masking the
    # original exception.
    assert not (run_dir / "run.log").exists()


def test_run_full_experiment_exception_after_scenario_child_launch_cleans_that_exact_child(tmp_path, monkeypatch) -> None:
    """Correction 1 + 4: proves inventory completeness for a genuine
    SCENARIO child (not just a canary) -- after the first scenario-1-4
    child has been launched and inventoried, an unrelated exception
    (raised from `wait_for_ready`, simulating any unexpected failure
    right after launch) must still result in emergency cleanup
    attempting exactly that child's exact resources, proving the
    inventory-append-before-any-fallible-operation discipline actually
    covers scenario children, not only canaries."""
    registry = _FakeDockerRegistry()

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake harness content for this test only\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", _make_fake_docker_git_run(registry, ""))
    monkeypatch.setattr(s.subprocess, "Popen", lambda *a, **k: _FakePopen())

    wait_for_ready_calls: list[str] = []

    def fake_wait_for_ready(identity, timeout):
        wait_for_ready_calls.append(identity.run_token)
        if len(wait_for_ready_calls) == 1:
            return True  # canary D reaches READY normally
        raise RuntimeError("simulated failure right after launching a scenario child")

    monkeypatch.setattr(s, "wait_for_ready", fake_wait_for_ready)

    original_child_entry = s._emergency_cleanup_child_entry
    child_cleanup_calls: list[str] = []

    def spying_child_entry(entry, source_repo):
        child_cleanup_calls.append(entry["identity"].run_token)
        return original_child_entry(entry, source_repo)

    monkeypatch.setattr(s, "_emergency_cleanup_child_entry", spying_child_entry)

    with pytest.raises(RuntimeError, match="simulated failure"):
        s.run_full_experiment()

    # Exactly two children were ever launched: canary D (reached READY)
    # and the first scenario child (normal_completion), whose
    # wait_for_ready call is what raised. Both must be recorded in
    # inventory and both must have been cleaned up -- not just canary D.
    assert len(wait_for_ready_calls) == 2
    assert set(child_cleanup_calls) == set(wait_for_ready_calls)

    evidence_dirs = list((fake_spike_dir / "evidence").rglob("summary.json"))
    assert len(evidence_dirs) == 1
    summary = json.loads(evidence_dirs[0].read_text())
    assert summary["overall_verdict"] != "PASS"
    assert summary["experiment_exception"] is not None
    assert "simulated failure" in summary["experiment_exception"]

    emergency_evidence = json.loads((evidence_dirs[0].parent / "emergency_cleanup_results.json").read_text())
    # 3 canary containers (A, B, C) + 2 children (D, first scenario)
    assert len(emergency_evidence["results"]) == 5


def test_emergency_cleanup_continues_across_individual_failures_with_multiple_entries(monkeypatch) -> None:
    """Directly exercises `emergency_cleanup` (NOT `run_full_experiment`
    -- a hand-built inventory is used specifically to avoid needing to
    steer `run_full_experiment`'s randomly generated names) with three
    entries, one of which can never actually be removed. Proves cleanup
    still attempts -- and reports on -- every recorded entry, not just
    the ones before the first failure."""
    registry = _FakeDockerRegistry()

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1] == "rm":
            name = argv[-1]
            if name == "canary-a-that-will-not-actually-be-removed":
                return _FakeCompleted(1, stderr="simulated rm failure")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    monkeypatch.setattr(s, "run", fake_run)

    registry.create("canary-a-that-will-not-actually-be-removed", {})
    registry.create("canary-b-ok", {})
    registry.create("canary-c-ok", {})
    inventory = [
        {"kind": "container", "name": "canary-a-that-will-not-actually-be-removed", "id": "fakeid-canary-a-that-will-not-actually-be-removed", "expected_labels": {}},
        {"kind": "container", "name": "canary-b-ok", "id": "fakeid-canary-b-ok", "expected_labels": {}},
        {"kind": "container", "name": "canary-c-ok", "id": "fakeid-canary-c-ok", "expected_labels": {}},
    ]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)

    assert len(result["results"]) == 3  # every entry attempted and reported, none skipped after the failure
    assert result["all_clean"] is False
    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["canary-a-that-will-not-actually-be-removed"]["action"] == "removal_unconfirmed"
    assert by_name["canary-b-ok"]["action"] == "removed"
    assert by_name["canary-c-ok"]["action"] == "removed"


def test_emergency_cleanup_isolates_an_unexpected_exception_to_one_entry(monkeypatch) -> None:
    """Correction 3: an unexpected exception (not merely a nonzero
    Docker/Git result) cleaning up the FIRST entry must not prevent the
    remaining entries from being attempted, and must be recorded as a
    sanitized technical_failure for that entry only."""

    def observe_container_raises_for_first(name: str):
        if name == "will-raise":
            raise OSError("simulated unexpected exception during observation")
        return s.ContainerObservation(ok=True, present=True, id="cid", labels={})

    monkeypatch.setattr(s, "observe_container", observe_container_raises_for_first)
    monkeypatch.setattr(s, "remove_container_and_confirm", lambda name: True)

    inventory = [
        {"kind": "container", "name": "will-raise", "id": "cid", "expected_labels": {}},
        {"kind": "container", "name": "will-succeed", "id": "cid", "expected_labels": {}},
    ]
    result = s.emergency_cleanup(inventory, SOURCE_REPO)

    assert len(result["results"]) == 2  # the second entry was still attempted
    by_name = {r.get("name"): r for r in result["results"]}
    assert by_name["will-raise"]["action"] == "technical_failure"
    assert "OSError" in by_name["will-raise"]["detail"]
    assert by_name["will-succeed"]["action"] == "removed"
    assert result["all_clean"] is False


# --------------------------------------------------------------------
# Fixture-repository worktree cleanliness check (Correction 2): the
# pure counting/parsing logic, extracted for direct testing.
# --------------------------------------------------------------------


def _count_and_validate_only_main_worktree(porcelain_text: str, expected_main_path: str) -> tuple[int, bool]:
    """Mirrors run_full_experiment's own inline fixture-worktree check
    -- kept here as a small reusable helper so the parsing logic itself
    is directly testable without invoking the full experiment."""
    entries = [line for line in porcelain_text.splitlines() if line.startswith("worktree ")]
    only_main = len(entries) == 1 and entries[0][len("worktree ") :] == expected_main_path
    return len(entries), only_main


def test_fixture_worktree_check_passes_with_only_main_worktree() -> None:
    text = "worktree /fake/fixture-repo\nHEAD deadbeef\nbranch refs/heads/main\n\n"
    count, only_main = _count_and_validate_only_main_worktree(text, "/fake/fixture-repo")
    assert count == 1
    assert only_main is True


def test_fixture_worktree_check_fails_with_leftover_registered_worktree() -> None:
    text = (
        "worktree /fake/fixture-repo\nHEAD deadbeef\nbranch refs/heads/main\n\n"
        "worktree /fake/fixture-repo/.git-worktrees/leftover\nHEAD deadbeef\ndetached\n\n"
    )
    count, only_main = _count_and_validate_only_main_worktree(text, "/fake/fixture-repo")
    assert count == 2
    assert only_main is False


# --------------------------------------------------------------------
# Reuse discipline
# --------------------------------------------------------------------


def test_default_image_is_imported_not_retyped() -> None:
    from codeagent import executor

    assert s.DEFAULT_IMAGE is executor.DEFAULT_IMAGE


# --------------------------------------------------------------------
# Linux/x86-64 portability infrastructure: platform gating, Actions-
# context validation, baseline-commit ancestry, strict Docker/cgroup
# provenance, findmnt-based filesystem observation, and the
# ProvenanceIncomplete -> TECHNICAL_FAILURE path. All Docker-free.
# --------------------------------------------------------------------


def test_is_supported_platform_accepts_darwin_any_machine(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s.platform, "machine", lambda: "arm64")
    ok, reason = s.is_supported_platform()
    assert ok is True
    assert "Darwin" in reason


def test_is_supported_platform_accepts_linux_x86_64(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "system", lambda: "Linux")
    monkeypatch.setattr(s.platform, "machine", lambda: "x86_64")
    ok, reason = s.is_supported_platform()
    assert ok is True
    assert "Linux/x86_64" in reason


def test_is_supported_platform_rejects_linux_aarch64(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "system", lambda: "Linux")
    monkeypatch.setattr(s.platform, "machine", lambda: "aarch64")
    ok, reason = s.is_supported_platform()
    assert ok is False
    assert "aarch64" in reason


def test_is_supported_platform_rejects_other_systems(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "system", lambda: "Windows")
    monkeypatch.setattr(s.platform, "machine", lambda: "AMD64")
    ok, reason = s.is_supported_platform()
    assert ok is False
    assert "Windows" in reason


def test_platform_key_still_correct_for_linux_x86_64(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "system", lambda: "Linux")
    monkeypatch.setattr(s.platform, "machine", lambda: "x86_64")
    assert s.platform_key() == "linux-x86_64"


def test_platform_key_still_correct_for_darwin_arm64(monkeypatch) -> None:
    """Preservation of existing macOS behavior: the Linux
    generalization must not have altered platform_key()'s macOS
    output, which every retained macOS evidence directory's path
    depends on."""
    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s.platform, "machine", lambda: "arm64")
    assert s.platform_key() == "macos-docker-desktop-arm64"


# ---- Actions-context validation ----


def test_validate_actions_context_absent_when_no_vars_set() -> None:
    assert s.validate_actions_context({}) == {"present": False}


def test_validate_actions_context_valid_full_context() -> None:
    env = {
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
    }
    ctx = s.validate_actions_context(env)
    assert ctx["present"] is True
    assert ctx["run_id"] == "12345"
    assert ctx["run_attempt"] == "2"
    assert ctx["workflow_url"] == "https://github.com/org/repo/actions/runs/12345"


def test_validate_actions_context_rejects_partial_context() -> None:
    env = {"GITHUB_RUN_ID": "12345"}  # missing attempt/server_url/repository
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(env)


def test_validate_actions_context_rejects_non_numeric_run_id() -> None:
    env = {
        "GITHUB_RUN_ID": "not-a-number",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
    }
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(env)


def test_validate_actions_context_rejects_non_numeric_run_attempt() -> None:
    env = {
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "one",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
    }
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(env)


def test_validate_actions_context_rejects_missing_server_url() -> None:
    env = {"GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "org/repo"}
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(env)


def test_validate_actions_context_rejects_missing_repository() -> None:
    env = {"GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SERVER_URL": "https://github.com"}
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(env)


def _valid_env(**overrides) -> dict:
    env = {
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
    }
    env.update(overrides)
    return env


def test_validate_actions_context_normalizes_trailing_slash_on_server_url() -> None:
    ctx = s.validate_actions_context(_valid_env(GITHUB_SERVER_URL="https://github.com/"))
    assert ctx["workflow_url"] == "https://github.com/org/repo/actions/runs/12345"


def test_validate_actions_context_rejects_non_http_scheme() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_SERVER_URL="ftp://github.com"))


def test_validate_actions_context_rejects_url_with_empty_host() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_SERVER_URL="https:///no-host"))


def test_validate_actions_context_rejects_url_with_embedded_credentials() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_SERVER_URL="https://user:pass@github.com"))


def test_validate_actions_context_rejects_relative_server_url() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_SERVER_URL="github.com"))


def test_validate_actions_context_rejects_repository_with_too_many_components() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="org/repo/extra"))


def test_validate_actions_context_rejects_repository_with_empty_owner() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="/repo"))


def test_validate_actions_context_rejects_repository_with_empty_name() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="org/"))


def test_validate_actions_context_rejects_repository_with_whitespace() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="org /repo"))


def test_validate_actions_context_rejects_repository_with_traversal_component() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="../repo"))


def test_validate_actions_context_rejects_repository_with_query_string() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="org/repo?x=1"))


def test_validate_actions_context_rejects_repository_with_fragment() -> None:
    with pytest.raises(s.ProvenanceIncomplete):
        s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="org/repo#frag"))


def test_validate_actions_context_accepts_well_formed_repository_with_dots_and_hyphens() -> None:
    ctx = s.validate_actions_context(_valid_env(GITHUB_REPOSITORY="my-org/my.repo_name"))
    assert ctx["present"] is True


# ---- --validate-actions-context / --normalize-docker-inventory CLI modes ----


def test_main_validate_actions_context_cli_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "org/repo",
        },
    )
    with pytest.raises(SystemExit) as exc_info:
        s.main(["--validate-actions-context"])
    assert exc_info.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["present"] is True


def test_main_validate_actions_context_cli_failure_exits_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(os, "environ", {"GITHUB_RUN_ID": "1"})  # partial
    with pytest.raises(SystemExit) as exc_info:
        s.main(["--validate-actions-context"])
    assert exc_info.value.code == 1
    assert "TECHNICAL_FAILURE" in capsys.readouterr().err


def test_evidence_run_dir_name_uses_uuid_when_absent() -> None:
    name = s.evidence_run_dir_name({"present": False})
    assert name.startswith("run-")
    assert "attempt" not in name


def test_evidence_run_dir_name_uses_run_id_and_attempt_when_present() -> None:
    name = s.evidence_run_dir_name({"present": True, "run_id": "999", "run_attempt": "3"})
    assert name == "run-999-attempt-3"


# ---- Baseline-commit ancestry ----


def test_check_ancestor_confirmed(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0))
    status, detail = s.check_ancestor("deadbeef", "/fake/repo")
    assert status == "ancestor"


def test_check_ancestor_confirmed_not_ancestor(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1))
    status, detail = s.check_ancestor("deadbeef", "/fake/repo")
    assert status == "not_ancestor"
    assert "NOT an ancestor" in detail


def test_check_ancestor_technical_failure_on_other_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(128, stderr="unknown revision"))
    status, detail = s.check_ancestor("deadbeef", "/fake/repo")
    assert status == "technical_failure"
    assert "128" in detail


# ---- Strict Docker/cgroup provenance parsing ----


def test_docker_client_version_success(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout="27.3.1\n"))
    assert s.docker_client_version() == "27.3.1"


def test_docker_client_version_fails_closed_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1, stderr="daemon unreachable"))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_client_version()


def test_docker_client_version_fails_closed_on_empty_output(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=""))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_client_version()


def test_docker_server_version_success(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout="27.3.1\n"))
    assert s.docker_server_version() == "27.3.1"


def test_docker_server_version_fails_closed_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_server_version()


def test_docker_daemon_cgroup_version_success(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout='"2"\n'))
    assert s.docker_daemon_cgroup_version() == "2"


def test_docker_daemon_cgroup_version_fails_closed_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_daemon_cgroup_version()


def test_docker_daemon_cgroup_version_fails_closed_on_malformed_json(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout="not json at all"))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_daemon_cgroup_version()


def test_docker_daemon_cgroup_version_fails_closed_on_empty_string_value(monkeypatch) -> None:
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout='""\n'))
    with pytest.raises(s.ProvenanceIncomplete):
        s.docker_daemon_cgroup_version()


def test_collect_kernel_and_architecture_success(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "release", lambda: "6.8.0-generic")
    monkeypatch.setattr(s.platform, "machine", lambda: "x86_64")
    kernel, machine = s.collect_kernel_and_architecture()
    assert kernel == "6.8.0-generic"
    assert machine == "x86_64"


def test_collect_kernel_and_architecture_fails_closed_on_empty_release(monkeypatch) -> None:
    monkeypatch.setattr(s.platform, "release", lambda: "")
    monkeypatch.setattr(s.platform, "machine", lambda: "x86_64")
    with pytest.raises(s.ProvenanceIncomplete):
        s.collect_kernel_and_architecture()


# ---- findmnt --json-based Linux filesystem observation ----


def _findmnt_json(target: str, fstype: str) -> str:
    """A real-shaped `findmnt --json --output TARGET,FSTYPE --target
    <path>` fixture, matching util-linux's actual output shape."""
    return json.dumps({"filesystems": [{"target": target, "fstype": fstype}]})


def test_observe_filesystem_linux_success_real_shaped_json(tmp_path, monkeypatch) -> None:
    target = tmp_path / "worktrees" / "abc"
    target.mkdir(parents=True)

    def fake_run(argv, **kw):
        if argv[0] == "findmnt":
            return _FakeCompleted(0, stdout=_findmnt_json(str(tmp_path), "ext4"))
        return _FakeCompleted(1)

    monkeypatch.setattr(s, "run", fake_run)
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is True
    assert obs.fs_type == "ext4"
    assert obs.mountpoint == str(tmp_path)
    assert obs.resolved_path == str(target.resolve())


def test_observe_filesystem_linux_fails_closed_on_nonexistent_path(tmp_path) -> None:
    """Proves the precondition: filesystem observation cannot succeed
    before the target path exists -- exactly why the real call site in
    run_full_experiment is placed only after the child has created its
    worktree/lock file and reached READY, never before. The error is a
    fixed categorical string, never a raw host path."""
    missing = tmp_path / "does-not-exist-yet" / "lock"
    obs = s.observe_filesystem_linux(missing)
    assert obs.ok is False
    assert obs.error == "path_does_not_exist"
    assert obs.resolved_path is None


def test_observe_filesystem_linux_then_succeeds_once_path_exists(tmp_path, monkeypatch) -> None:
    """Direct proof of the ordering requirement: the exact same path
    fails closed before creation and succeeds after -- there is no
    silent 'describes the wrong enclosing directory' outcome in
    between."""
    target = tmp_path / "locks" / "abc.lock"

    def fake_run(argv, **kw):
        return _FakeCompleted(0, stdout=_findmnt_json(str(tmp_path), "apfs"))

    monkeypatch.setattr(s, "run", fake_run)

    before = s.observe_filesystem_linux(target)
    assert before.ok is False

    target.parent.mkdir(parents=True)
    target.write_text("x")
    after = s.observe_filesystem_linux(target)
    assert after.ok is True
    assert after.fs_type == "apfs"


def test_observe_filesystem_linux_fails_closed_on_findmnt_command_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_command_failed"


def test_observe_filesystem_linux_fails_closed_on_malformed_json(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout="not json at all"))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_output_not_valid_json"


def test_observe_filesystem_linux_fails_closed_on_empty_output(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=""))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_output_not_valid_json"


def test_observe_filesystem_linux_fails_closed_on_missing_filesystems_key(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=json.dumps({"other": []})))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_missing_filesystems_array"


def test_observe_filesystem_linux_fails_closed_on_zero_records(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=json.dumps({"filesystems": []})))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_did_not_contain_exactly_one_record"


def test_observe_filesystem_linux_fails_closed_on_multiple_records(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": [{"target": "/a", "fstype": "ext4"}, {"target": "/b", "fstype": "ext4"}]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_did_not_contain_exactly_one_record"


def test_observe_filesystem_linux_fails_closed_on_record_not_an_object(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": ["not-an-object"]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_record_not_an_object"


def test_observe_filesystem_linux_fails_closed_on_missing_target_field(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": [{"fstype": "ext4"}]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_target_missing_or_empty"


def test_observe_filesystem_linux_fails_closed_on_wrong_typed_target_field(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": [{"target": 12345, "fstype": "ext4"}]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_target_missing_or_empty"


def test_observe_filesystem_linux_fails_closed_on_missing_fstype_field(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": [{"target": "/mnt"}]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_fstype_missing_or_empty"


def test_observe_filesystem_linux_fails_closed_on_empty_string_fstype(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x"
    target.write_text("x")
    raw = json.dumps({"filesystems": [{"target": "/mnt", "fstype": ""}]})
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=raw))
    obs = s.observe_filesystem_linux(target)
    assert obs.ok is False
    assert obs.error == "findmnt_json_fstype_missing_or_empty"


def test_observe_filesystem_linux_error_never_contains_host_path() -> None:
    """Every FilesystemObservation.error value used across this file's
    fail-closed tests is a fixed categorical token -- never an
    interpolated path or raw command output -- so folding it into
    ProvenanceIncomplete's message (and eventually
    technical_failure_reason) never leaks host filesystem layout."""
    categorical_errors = {
        "path_does_not_exist",
        "findmnt_command_failed",
        "findmnt_output_not_valid_json",
        "findmnt_json_missing_filesystems_array",
        "findmnt_json_did_not_contain_exactly_one_record",
        "findmnt_json_record_not_an_object",
        "findmnt_json_target_missing_or_empty",
        "findmnt_json_fstype_missing_or_empty",
    }
    for err in categorical_errors:
        assert "/" not in err  # no path separator ever appears in a fixed categorical token


# ---- Workflow-diagnostic normalization helper: exact `docker inspect`
# based (implemented in Python). Replaces an earlier comma-separated
# `docker ps .Labels` string-parsing approach -- that mechanism's own
# "round-trip" ambiguity check was not a valid detector: a comma
# embedded in one label's value can still split into ANOTHER
# syntactically valid key=value pair from the remainder, which passes
# a naive round-trip comparison while silently misattributing label
# data. Structured `docker inspect` JSON has no such ambiguity. ----

ID_A = "a" * 64
ID_B = "b" * 64


def _inspect_record(container_id: str, name: str = "/mycontainer", image: str = "sha256:" + "d" * 64, labels=None) -> list:
    return [{"Id": container_id, "Name": name, "Image": image, "Config": {"Labels": labels if labels is not None else {}}}]


def _fake_docker_inspect(outcomes: dict, monkeypatch) -> None:
    """outcomes maps container_id -> one of: a list (the parsed
    `docker inspect` JSON array, dumped to JSON stdout), a raw string
    (malformed/unparseable stdout used as-is), or the sentinel
    "FAIL" (simulates `docker inspect` itself exiting nonzero). Any
    container_id not present in `outcomes` also simulates a missing
    container (nonzero exit)."""

    def fake_run(argv, **kw):
        assert argv[0] == "docker" and argv[1] == "inspect"
        cid = argv[2]
        outcome = outcomes.get(cid, "FAIL")
        if outcome == "FAIL":
            return _FakeCompleted(1, stderr="no such object")
        if isinstance(outcome, str):
            return _FakeCompleted(0, stdout=outcome)
        return _FakeCompleted(0, stdout=json.dumps(outcome))

    monkeypatch.setattr(s, "run", fake_run)


def test_normalize_container_listing_empty_list_is_valid_empty_inventory() -> None:
    assert s.normalize_container_listing([]) == []


def test_normalize_container_listing_success_strips_leading_slash_from_name(monkeypatch) -> None:
    _fake_docker_inspect({ID_A: _inspect_record(ID_A, name="/mycontainer")}, monkeypatch)
    entries = s.normalize_container_listing([ID_A])
    assert entries == [{"id": ID_A, "name": "mycontainer", "image": "sha256:" + "d" * 64, "codeagent_labels": {}}]


def test_normalize_container_listing_sorts_by_id(monkeypatch) -> None:
    _fake_docker_inspect({ID_A: _inspect_record(ID_A), ID_B: _inspect_record(ID_B)}, monkeypatch)
    entries = s.normalize_container_listing([ID_B, ID_A])
    assert [e["id"] for e in entries] == [ID_A, ID_B]


def test_normalize_container_listing_preserves_label_values_with_commas_and_equals_signs(monkeypatch) -> None:
    """The whole point of using structured docker inspect JSON instead
    of comma-joined `docker ps .Labels` text: a label value containing
    a literal comma or equals sign is preserved EXACTLY, with no
    ambiguity or misparsing possible."""
    labels = {"codeagent.spike": "s5,weird=value,with=commas", "codeagent.s5_session": "a=b,c=d"}
    _fake_docker_inspect({ID_A: _inspect_record(ID_A, labels=labels)}, monkeypatch)
    entries = s.normalize_container_listing([ID_A])
    assert entries[0]["codeagent_labels"] == labels


def test_normalize_container_listing_only_emits_codeagent_relevant_labels(monkeypatch) -> None:
    labels = {"codeagent.spike": "s5", "unrelated.label": "should-not-appear", "other": "x"}
    _fake_docker_inspect({ID_A: _inspect_record(ID_A, labels=labels)}, monkeypatch)
    entries = s.normalize_container_listing([ID_A])
    assert entries[0]["codeagent_labels"] == {"codeagent.spike": "s5"}


def test_normalize_container_listing_never_emits_full_config_or_env(monkeypatch) -> None:
    """Only the four documented fields are ever emitted -- never the
    full inspect record, Config.Env, Mounts, or anything else that
    could contain container environment values."""
    record = _inspect_record(ID_A, labels={"codeagent.spike": "s5"})
    record[0]["Config"]["Env"] = ["SECRET_TOKEN=abc123", "PATH=/usr/bin"]
    record[0]["Mounts"] = [{"Source": "/host/secret/path"}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    entries = s.normalize_container_listing([ID_A])
    assert set(entries[0].keys()) == {"id", "name", "image", "codeagent_labels"}
    dumped = json.dumps(entries)
    assert "SECRET_TOKEN" not in dumped
    assert "/host/secret/path" not in dumped


def test_normalize_container_listing_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        s.normalize_container_listing([ID_A, ID_A])


def test_normalize_container_listing_rejects_malformed_id() -> None:
    with pytest.raises(ValueError, match="malformed"):
        s.normalize_container_listing(["not-a-hex-id!!"])


def test_normalize_container_listing_rejects_empty_string_id() -> None:
    with pytest.raises(ValueError, match="malformed"):
        s.normalize_container_listing([""])


def test_normalize_container_listing_rejects_inspect_command_failure(monkeypatch) -> None:
    _fake_docker_inspect({}, monkeypatch)  # ID_A absent -> simulated inspect failure
    with pytest.raises(ValueError, match="failed"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_malformed_json(monkeypatch) -> None:
    _fake_docker_inspect({ID_A: "not json at all"}, monkeypatch)
    with pytest.raises(ValueError, match="malformed JSON"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_zero_results(monkeypatch) -> None:
    _fake_docker_inspect({ID_A: []}, monkeypatch)
    with pytest.raises(ValueError, match="exactly one result"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_multiple_results(monkeypatch) -> None:
    _fake_docker_inspect({ID_A: _inspect_record(ID_A) + _inspect_record(ID_A)}, monkeypatch)
    with pytest.raises(ValueError, match="exactly one result"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_missing_id_field(monkeypatch) -> None:
    record = [{"Name": "/x", "Image": "sha256:dead", "Config": {"Labels": {}}}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="Id"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_id_mismatch(monkeypatch) -> None:
    """The inspected record's own Id must match the requested ID
    exactly -- catches an index/argument mixup rather than silently
    trusting whatever docker inspect happened to return."""
    _fake_docker_inspect({ID_A: _inspect_record(ID_B)}, monkeypatch)
    with pytest.raises(ValueError, match="does not match"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_missing_name_field(monkeypatch) -> None:
    record = [{"Id": ID_A, "Image": "sha256:dead", "Config": {"Labels": {}}}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="Name"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_missing_image_field(monkeypatch) -> None:
    record = [{"Id": ID_A, "Name": "/x", "Config": {"Labels": {}}}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="Image"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_missing_config_object(monkeypatch) -> None:
    record = [{"Id": ID_A, "Name": "/x", "Image": "sha256:dead"}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="Config"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_non_object_labels(monkeypatch) -> None:
    record = [{"Id": ID_A, "Name": "/x", "Image": "sha256:dead", "Config": {"Labels": "not-an-object"}}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="Labels"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_rejects_non_string_label_value(monkeypatch) -> None:
    record = [{"Id": ID_A, "Name": "/x", "Image": "sha256:dead", "Config": {"Labels": {"codeagent.spike": 123}}}]
    _fake_docker_inspect({ID_A: record}, monkeypatch)
    with pytest.raises(ValueError, match="not a string"):
        s.normalize_container_listing([ID_A])


def test_normalize_container_listing_treats_missing_labels_key_as_empty(monkeypatch) -> None:
    """Config.Labels can legitimately be absent entirely (docker
    reports null/omits it for a container with no labels at all) --
    this is a valid empty-labels case, not a missing-field error."""
    record = [{"Id": ID_A, "Name": "/x", "Image": "sha256:dead", "Config": {}}]
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(0, stdout=json.dumps(record)))
    entries = s.normalize_container_listing([ID_A])
    assert entries[0]["codeagent_labels"] == {}


# ---- ProvenanceIncomplete -> TECHNICAL_FAILURE end-to-end ----


def test_run_full_experiment_ancestor_check_failure_raises_before_evidence_created(tmp_path, monkeypatch) -> None:
    """A PRE-EVIDENCE precondition (ancestor-check) failure happens
    before any evidence directory or resource is created -- there is
    nothing yet to finalize, no try/except/finally scope exists yet at
    this point in the function, and the exception propagates directly
    to the caller (the workflow's own shell-level provenance preflight,
    or a local invocation's process exit). This is NOT a harness
    overall_verdict=TECHNICAL_FAILURE case -- there is no summary.json
    at all, by construction, only a raised exception. Contrast with
    test_run_full_experiment_technical_failure_when_host_provenance_
    collection_fails below, where the SAME exception type raised LATER
    (after evidence/finalization scope exists) does produce a real
    summary.json with that overall_verdict."""
    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake\n")

    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", lambda argv, **kw: _FakeCompleted(1))  # ancestor check: not_ancestor
    monkeypatch.setattr(os, "environ", {})

    with pytest.raises(s.ProvenanceIncomplete):
        s.run_full_experiment()

    # No evidence directory at all -- never a summary.json, never any
    # overall_verdict value (TECHNICAL_FAILURE included).
    assert not (fake_spike_dir / "evidence").exists()


def test_run_full_experiment_malformed_actions_context_raises_before_evidence_created(tmp_path, monkeypatch) -> None:
    """A PRE-EVIDENCE precondition failure, exactly like the ancestor-
    check test above: a partial Actions context must never fall back
    to local UUID naming, raises before any evidence directory exists,
    and produces no harness summary.json/overall_verdict of any kind --
    only a raised exception, caught by the workflow's own shell-level
    `--validate-actions-context` preflight before the real experiment
    ever starts."""
    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake\n")

    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(os, "environ", {"GITHUB_RUN_ID": "123"})  # partial

    with pytest.raises(s.ProvenanceIncomplete):
        s.run_full_experiment()

    assert not (fake_spike_dir / "evidence").exists()


def test_run_full_experiment_technical_failure_when_host_provenance_collection_fails(tmp_path, monkeypatch) -> None:
    """A provenance failure that happens AFTER the scratch root/fixture
    repo already exist (docker cgroup-version collection, here) must
    still run full finalization -- unlike the two precondition tests
    above, evidence IS produced, overall_verdict is TECHNICAL_FAILURE
    (never FAIL, never PASS), the CLI exit is nonzero (an uncaught
    exception), and the reason is sanitized (no raw daemon stderr)."""
    registry = _FakeDockerRegistry()

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1] == "info":
            return _FakeCompleted(1, stderr="daemon exploded with secret token abc123")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)
    monkeypatch.setattr(os, "environ", {})

    with pytest.raises(s.ProvenanceIncomplete):
        s.run_full_experiment()

    evidence_dirs = list((fake_spike_dir / "evidence").rglob("summary.json"))
    assert len(evidence_dirs) == 1
    summary = json.loads(evidence_dirs[0].read_text())
    assert summary["overall_verdict"] == "TECHNICAL_FAILURE"
    assert summary["technical_failure_reason"] is not None
    assert "secret token" not in summary["technical_failure_reason"]
    assert "exit=1" in summary["technical_failure_reason"]

    # Finalization still ran fully: scratch root removed, emergency
    # cleanup attempted the resources already created (fixture repo
    # setup only got as far as `docker pull`, before host_info -- no
    # canaries/children existed yet, so an empty-but-successful cleanup
    # is the correct outcome here).
    assert summary["scratch_root_removed_confirmed"] is True


def test_run_full_experiment_run_info_includes_workflow_and_baseline_provenance(tmp_path, monkeypatch) -> None:
    registry = _FakeDockerRegistry()

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake\n")

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1:3] == ["run", "-d"]:
            name = argv[argv.index("--name") + 1]
            if name.startswith("codeagent-spike-s5-"):
                return _FakeCompleted(1, stderr="stop before full run -- only RUN_INFO matters for this test")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "GITHUB_RUN_ID": "555",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "org/repo",
        },
    )

    with pytest.raises(RuntimeError):
        s.run_full_experiment()

    run_info_files = list((fake_spike_dir / "evidence").rglob("RUN_INFO.json"))
    assert len(run_info_files) == 1
    run_info = json.loads(run_info_files[0].read_text())
    assert run_info["s5_baseline_commit"] == s.S5_BASELINE_COMMIT
    assert run_info["s5_baseline_commit_is_ancestor"] is True
    assert run_info["workflow_run_id"] == "555"
    assert run_info["workflow_run_attempt"] == "1"
    assert run_info["workflow_run_url"] == "https://github.com/org/repo/actions/runs/555"
    assert run_info["actions_context_present"] is True
    assert run_info_files[0].parent.name == "run-555-attempt-1"


def test_run_full_experiment_darwin_host_info_has_no_linux_filesystem_fields(tmp_path, monkeypatch) -> None:
    """Preservation of existing macOS behavior: point 1 of this
    correction pass explicitly forbids adding new macOS filesystem
    detection in this Linux-only preparation pass. Drives
    run_full_experiment far enough (through the new fail-closed
    preconditions and the new fail-closed host-provenance collection)
    to reach host.json, forced to Darwin, and confirms it contains the
    new required client/server/cgroup provenance fields but NONE of
    the Linux-only lock/worktree filesystem observation fields, which
    are only ever added on the Linux branch inside the scenario-5
    block."""
    registry = _FakeDockerRegistry()

    def fake_run(argv, **kw):
        if argv[0] == "docker" and argv[1:3] == ["run", "-d"]:
            name = argv[argv.index("--name") + 1]
            if name.startswith("codeagent-spike-s5-"):
                return _FakeCompleted(1, stderr="stop right after host_info -- only host.json matters here")
        return _make_fake_docker_git_run(registry, "")(argv, **kw)

    fake_spike_dir = tmp_path / "repo" / "spikes" / "s5"
    fake_spike_dir.mkdir(parents=True)
    fake_this_file = fake_spike_dir / "spike_s5.py"
    fake_this_file.write_bytes(b"# fake\n")

    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(s, "SPIKE_DIR", fake_spike_dir)
    monkeypatch.setattr(s, "THIS_FILE", fake_this_file)
    monkeypatch.setattr(s, "run", fake_run)
    monkeypatch.setattr(os, "environ", {})

    with pytest.raises(RuntimeError):
        s.run_full_experiment()

    host_files = list((fake_spike_dir / "evidence").rglob("host.json"))
    assert len(host_files) == 1
    host_info = json.loads(host_files[0].read_text())
    assert host_info["docker_client_version"] == "99.0.0"
    assert host_info["docker_server_version"] == "99.0.0"
    assert host_info["docker_daemon_cgroup_version"] == "2"
    assert "lock_filesystem_type" not in host_info
    assert "worktree_filesystem_type" not in host_info
