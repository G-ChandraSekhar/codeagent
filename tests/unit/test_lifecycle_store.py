"""Tests for `codeagent.lifecycle_store` (Milestone 3 Slice 3A-2, ADR
0004 Amendment 1 sections 5, 6, 12, 16)."""

from __future__ import annotations

import dataclasses
import multiprocessing
import os
import signal
import subprocess
from pathlib import Path

import pytest

from codeagent import _git_safety as gs
from codeagent import _lifecycle_fs as lf
from codeagent import checkpoint_ref as cr
from codeagent import checkpoint_session as cs
from codeagent import lifecycle_owner as lo
from codeagent import lifecycle_store as ls
from codeagent import repo_identity as ri
from codeagent import state_locks as sl
from codeagent import worktree_lifecycle as wl


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


# ---------------------------------------------------------------------------
# End-to-end composition
# ---------------------------------------------------------------------------


def test_prepare_lifecycle_basic(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)

    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    try:
        assert ls._is_hex32(lease.lifecycle_id)
        assert ls._is_hex32(lease.repo_key)
        assert lease.lifecycle_lock.is_held
        assert lease.repository_lock.is_held
    finally:
        lease.close()


def test_prepare_lifecycle_publishes_valid_initial_projection(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    try:
        path = (
            state_dir
            / "repos"
            / lease.repo_key
            / "runs"
            / lease.lifecycle_id
            / ls.LIFECYCLE_JSON_FILENAME
        )
        data = path.read_bytes()
        payload = lf.canonical_json_loads_strict(data, max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")
        assert payload["state"] == "PREPARING"
        assert payload["lifecycle_id"] == lease.lifecycle_id
        assert payload["repo_key"] == lease.repo_key
        assert payload["run_id"] == "run-1"
        assert payload["containers"]["baseline"] == {"intent": "absent", "id": None}
        assert payload["containers"]["verification"] == {"intent": "absent", "id": None}
        assert payload["worktree"] == {"intent": "absent", "expected_head": None}
        assert payload["checkpoint_ref"] == {
            "intent": "absent",
            "accepted_sha": None,
            "expected_old_sha": None,
            "proposed_new_sha": None,
        }
        assert payload["failure"] is None
        assert payload["reconciliation"] == {"attempts_total": 0, "recent_failures": []}
        # file mode is private
        st = os.stat(path)
        assert (st.st_mode & 0o777) == 0o600
    finally:
        lease.close()


def test_prepare_lifecycle_source_repo_path_is_working_tree_root_not_common_dir(tmp_path, monkeypatch):
    # Correction: source_repo_path previously recorded
    # validated_identity.canonical_common_dir (typically "<repo>/.git"),
    # not the repository's actual working-tree root.
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    identity, context = ri.discover_repository_identity_and_context(str(repo))
    assert context.working_tree_root is not None
    assert context.working_tree_root != identity.canonical_common_dir

    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    try:
        path = state_dir / "repos" / lease.repo_key / "runs" / lease.lifecycle_id / ls.LIFECYCLE_JSON_FILENAME
        payload = lf.canonical_json_loads_strict(path.read_bytes(), max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)
        assert payload["source_repo_path"] == context.working_tree_root
        assert payload["source_repo_path"] != identity.canonical_common_dir
        assert not payload["source_repo_path"].endswith(".git")
    finally:
        lease.close()


def test_prepare_lifecycle_two_runs_get_distinct_lifecycle_ids(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)

    lease1 = ls.prepare_lifecycle(str(repo), run_id="run-1")
    lease1.close()
    lease2 = ls.prepare_lifecycle(str(repo), run_id="run-2")
    try:
        assert lease1.lifecycle_id != lease2.lifecycle_id
    finally:
        lease2.close()


def test_prepare_lifecycle_sha256_or_skipped(tmp_path, monkeypatch):
    repo = tmp_path / "sha256repo"
    repo.mkdir()
    result = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(repo)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("installed git does not support --object-format=sha256")
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    _set_state_dir(monkeypatch, tmp_path)

    lease = ls.prepare_lifecycle(str(repo), run_id="run-sha256")
    try:
        assert lease.lifecycle_id
    finally:
        lease.close()


def test_prepare_lifecycle_sha1_explicit(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="run-sha1")
    try:
        identity, _ = ri.discover_repository_identity_and_context(str(repo))
        assert identity.object_format == "sha1"
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Preflight gating: zero repository operations on preflight failure
# ---------------------------------------------------------------------------


def test_preflight_failure_causes_zero_repository_operations(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    def _boom():
        raise gs.GitSafetyError(gs.GitSafetyFailure.GIT_VERSION_CHECK_FAILED, "forced")

    monkeypatch.setattr(ls, "check_git_preflight", _boom)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("repository discovery must not run when preflight fails")

    monkeypatch.setattr(ls, "discover_repository_identity_and_context", _should_not_be_called)

    with pytest.raises(gs.GitSafetyError):
        ls.prepare_lifecycle(str(repo), run_id="run-1")

    assert not state_dir.exists(), "no state-root directory may be created before preflight succeeds"


def test_preflight_runs_before_discovery(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    order = []

    real_preflight = ls.check_git_preflight
    real_discover = ls.discover_repository_identity_and_context

    def _preflight_recorder():
        order.append("preflight")
        return real_preflight()

    def _discover_recorder(*args, **kwargs):
        order.append("discover")
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(ls, "check_git_preflight", _preflight_recorder)
    monkeypatch.setattr(ls, "discover_repository_identity_and_context", _discover_recorder)

    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    try:
        assert order == ["preflight", "discover"]
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Exact composition ordering
# ---------------------------------------------------------------------------


def test_exact_lock_directory_projection_ordering(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    order = []

    real_acquire_repo_lock = ls.acquire_repository_lock
    real_create_dir = ls.create_exclusive_directory_at
    real_acquire_lifecycle_lock = ls.acquire_lifecycle_lock
    real_publish = ls.publish_private_file_atomically_at

    def _repo_lock_recorder(*args, **kwargs):
        order.append("repository_lock")
        return real_acquire_repo_lock(*args, **kwargs)

    def _create_dir_recorder(*args, **kwargs):
        order.append("run_directory")
        return real_create_dir(*args, **kwargs)

    def _lifecycle_lock_recorder(*args, **kwargs):
        order.append("lifecycle_lock")
        return real_acquire_lifecycle_lock(*args, **kwargs)

    def _publish_recorder(*args, **kwargs):
        order.append("projection")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(ls, "acquire_repository_lock", _repo_lock_recorder)
    monkeypatch.setattr(ls, "create_exclusive_directory_at", _create_dir_recorder)
    monkeypatch.setattr(ls, "acquire_lifecycle_lock", _lifecycle_lock_recorder)
    monkeypatch.setattr(ls, "publish_private_file_atomically_at", _publish_recorder)

    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    try:
        assert order == ["repository_lock", "run_directory", "lifecycle_lock", "projection"]
    finally:
        lease.close()


def test_release_ordering_lifecycle_lock_then_run_dir_then_repo_lock_then_state_root(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")

    order = []
    real_lifecycle_release = lease.lifecycle_lock.release
    real_close_confirmed = ls.close_confirmed
    real_repo_release = lease.repository_lock.release
    real_state_root_close = lease.state_root.close

    def _lifecycle_release():
        order.append("lifecycle_lock")
        return real_lifecycle_release()

    def _close_confirmed_recorder(fds):
        if fds == [lease.run_dir_fd]:
            order.append("run_directory")
        return real_close_confirmed(fds)

    def _repo_release():
        order.append("repository_lock")
        return real_repo_release()

    def _state_root_close():
        order.append("state_root")
        return real_state_root_close()

    lease.lifecycle_lock.release = _lifecycle_release
    monkeypatch.setattr(ls, "close_confirmed", _close_confirmed_recorder)
    lease.repository_lock.release = _repo_release
    lease.state_root.close = _state_root_close

    lease.close()
    assert order == ["lifecycle_lock", "run_directory", "repository_lock", "state_root"]


# ---------------------------------------------------------------------------
# Collision refusal: no adoption
# ---------------------------------------------------------------------------


def test_lifecycle_id_collision_is_refused_not_adopted(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)

    fixed_id = "a" * 32
    monkeypatch.setattr(ls, "new_lifecycle_id", lambda: fixed_id)

    lease1 = ls.prepare_lifecycle(str(repo), run_id="run-1")
    lease1.close()

    # The run directory for fixed_id now already exists (from lease1).
    # A second attempt reusing the same (monkeypatched) id must refuse,
    # never adopt or overwrite the existing run.
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-2")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.LIFECYCLE_ID_COLLISION

    # The original projection is untouched.
    location_path = (
        tmp_path / "state-root" / "repos" / lease1.repo_key / "runs" / fixed_id / ls.LIFECYCLE_JSON_FILENAME
    )
    payload = lf.canonical_json_loads_strict(location_path.read_bytes(), max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)
    assert payload["run_id"] == "run-1"


def test_lifecycle_id_collision_still_releases_repo_lock_and_state_root(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    fixed_id = "b" * 32

    with monkeypatch.context() as scoped:
        scoped.setattr(ls, "new_lifecycle_id", lambda: fixed_id)
        lease1 = ls.prepare_lifecycle(str(repo), run_id="run-1")
        lease1.close()

        with pytest.raises(ls.LifecycleStoreError):
            ls.prepare_lifecycle(str(repo), run_id="run-2")

    # The repository lock must be free again: a fresh attempt with a
    # non-colliding id (new_lifecycle_id no longer patched) succeeds.
    lease3 = ls.prepare_lifecycle(str(repo), run_id="run-3")
    try:
        assert lease3.lifecycle_id != fixed_id
    finally:
        lease3.close()


# ---------------------------------------------------------------------------
# Symlink / ownership / permission refusal at the run-directory step
# ---------------------------------------------------------------------------


def test_hostile_preexisting_symlink_at_lifecycle_id_is_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    fixed_id = "c" * 32
    monkeypatch.setattr(ls, "new_lifecycle_id", lambda: fixed_id)

    # Pre-create the runs/ parent for this repo_key by running one
    # throwaway successful lifecycle first (with a different id), then
    # plant a symlink named exactly the fixed id inside runs/.
    monkeypatch.setattr(ls, "new_lifecycle_id", lambda: "d" * 32)
    warm = ls.prepare_lifecycle(str(repo), run_id="warm")
    repo_key = warm.repo_key
    warm.close()

    runs_dir = state_dir / "repos" / repo_key / "runs"
    hostile_target = tmp_path / "hostile-target"
    hostile_target.mkdir()
    (runs_dir / fixed_id).symlink_to(hostile_target)

    monkeypatch.setattr(ls, "new_lifecycle_id", lambda: fixed_id)
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="hostile")
    # Slice 3B-1: automatic pre-run reconciliation now enumerates the
    # complete runs/ namespace before a lifecycle_id is ever minted, so
    # this hostile symlink is caught there (REFUSED, aborting the whole
    # pass) rather than later at `create_exclusive_directory_at`'s own
    # collision check.
    assert excinfo.value.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED
    # The symlink itself is left in place -- never deleted or adopted.
    assert (runs_dir / fixed_id).is_symlink()


# ---------------------------------------------------------------------------
# Size bounds
# ---------------------------------------------------------------------------


def test_run_id_over_bound_refused_before_any_side_effect(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    huge_run_id = "x" * (ls.RUN_ID_MAX_ENCODED_BYTES + 10)

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id=huge_run_id)
    assert excinfo.value.reason is ls.LifecycleStoreFailure.OVERSIZED
    assert not state_dir.exists()


def test_empty_run_id_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.OVERSIZED


def test_source_repo_path_over_bound_refused_after_locks_still_cleans_up(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)

    huge_path = "/" + ("a" * (lf.STORED_PATH_MAX_FS_BYTES + 10))

    real_discover = ls.discover_repository_identity_and_context

    def _tampered(source_repo_path):
        identity, context = real_discover(source_repo_path)
        return identity, ri.TrustedRepositoryContext(working_tree_root=huge_path, common_dir=context.common_dir)

    with monkeypatch.context() as scoped:
        scoped.setattr(ls, "discover_repository_identity_and_context", _tampered)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls.prepare_lifecycle(str(repo), run_id="run-1")
        assert excinfo.value.reason is ls.LifecycleStoreFailure.OVERSIZED

    # The repository lock and state root were both released, and the
    # tampering patch above no longer applies outside the `with` block
    # -- but the failed attempt already created a real run directory
    # and acquired its lifecycle lock before the oversized-path check
    # ran, so that directory durably exists with no `lifecycle.json`
    # ever published into it. Slice 3B-1's automatic pre-run
    # reconciliation (ADR 0004 Amendment 2 section 7) refuses exactly
    # this shape -- a run directory without a valid, identity-matched
    # projection is never adopted or repaired -- so a fresh attempt now
    # correctly blocks rather than silently proceeding past it. This is
    # the named residual risk in Amendment 2: recovery requires future
    # abandonment, not implemented in this slice.
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-2")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED


def test_whole_projection_over_bound_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "LIFECYCLE_JSON_MAX_BYTES", 10)
    projection = ls.build_initial_preparing_projection(
        lifecycle_id="a" * 32,
        state_root_id="b" * 32,
        repo_key="c" * 32,
        run_id="run-1",
        source_repo_path="/tmp/x",
    )
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls._encode_and_bound_projection(projection)
    assert excinfo.value.reason is ls.LifecycleStoreFailure.OVERSIZED


# ---------------------------------------------------------------------------
# Schema validation: valid shape, unknown fields, invalid combinations
# ---------------------------------------------------------------------------


_SHA1_A = "a" * 40
_SHA1_B = "b" * 40
_SHA1_F = "f" * 40
_SHA256_A = "a" * 64
_SHA256_B = "b" * 64


def _valid_payload(**overrides):
    payload = {
        "schema_version": 1,
        "lifecycle_id": "a" * 32,
        "state_root_id": "b" * 32,
        "repo_key": "c" * 32,
        "run_id": "run-1",
        "source_repo_path": "/tmp/repo",
        "state": "PREPARING",
        "containers": {
            "baseline": {"intent": "absent", "id": None},
            "verification": {"intent": "absent", "id": None},
        },
        "worktree": {"intent": "absent", "expected_head": None},
        "checkpoint_ref": {
            "intent": "absent",
            "accepted_sha": None,
            "expected_old_sha": None,
            "proposed_new_sha": None,
        },
        "failure": None,
        "reconciliation": {"attempts_total": 0, "recent_failures": []},
    }
    payload.update(overrides)
    return payload


def test_valid_payload_passes_schema_validation():
    ls.validate_lifecycle_json_schema(_valid_payload(), object_format="sha1")


def test_invalid_object_format_refused():
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(_valid_payload(), object_format="sha512")


def test_unknown_top_level_field_refused():
    payload = _valid_payload()
    payload["unexpected"] = "x"
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_missing_top_level_field_refused():
    payload = _valid_payload()
    del payload["state"]
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_unknown_field_in_containers_refused():
    payload = _valid_payload()
    payload["containers"]["baseline"]["extra"] = 1
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_unknown_field_in_checkpoint_ref_refused():
    payload = _valid_payload()
    payload["checkpoint_ref"]["extra"] = 1
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_unknown_field_in_worktree_refused():
    payload = _valid_payload()
    payload["worktree"]["extra"] = 1
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_unknown_field_in_reconciliation_refused():
    payload = _valid_payload()
    payload["reconciliation"]["extra"] = 1
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_invalid_schema_version_refused():
    payload = _valid_payload(schema_version=2)
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_boolean_schema_version_refused():
    # bool is a subclass of int in Python; True == 1 must never be
    # accepted as schema_version.
    payload = _valid_payload(schema_version=True)
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_invalid_lifecycle_id_shape_refused():
    payload = _valid_payload(lifecycle_id="not-hex")
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_invalid_state_refused():
    payload = _valid_payload(state="NOT_A_STATE")
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_relative_source_repo_path_refused():
    payload = _valid_payload(source_repo_path="relative/path")
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_oversized_source_repo_path_refused():
    payload = _valid_payload(source_repo_path="/" + "a" * (lf.STORED_PATH_MAX_FS_BYTES + 1))
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_oversized_run_id_refused():
    payload = _valid_payload(run_id="x" * (ls.RUN_ID_MAX_ENCODED_BYTES + 1))
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


@pytest.mark.parametrize(
    ("intent", "container_id", "valid"),
    [
        ("absent", None, True),
        ("creating", None, True),
        ("present", "abc123", True),
        ("removing", "abc123", True),
        ("absent", "abc123", False),
        ("present", None, False),
        ("bogus", None, False),
    ],
)
def test_container_intent_id_combinations(intent, container_id, valid):
    payload = _valid_payload()
    payload["containers"]["baseline"] = {"intent": intent, "id": container_id}
    if valid:
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")
    else:
        with pytest.raises(ls.LifecycleStoreError):
            ls.validate_lifecycle_json_schema(payload, object_format="sha1")


# ---------------------------------------------------------------------------
# checkpoint_ref: reuses checkpoint_session.CheckpointTransition's own
# combination table, plus this module's own object-format-aware OID
# validation (the gap the correction pass closed: "A"/"B" placeholders
# are no longer accepted as persisted SHAs).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "accepted", "expected_old", "proposed_new", "valid"),
    [
        ("absent", None, None, None, True),
        ("creating", None, None, _SHA1_B, True),
        ("present", _SHA1_A, None, None, True),
        ("advancing", _SHA1_A, _SHA1_A, _SHA1_B, True),
        ("removing", _SHA1_F, _SHA1_F, None, True),
        ("absent", _SHA1_A, None, None, False),
        ("creating", _SHA1_A, None, _SHA1_B, False),
        ("present", _SHA1_A, _SHA1_A, None, False),
        ("advancing", _SHA1_A, "x" * 40, _SHA1_B, False),
        ("advancing", _SHA1_A, _SHA1_A, _SHA1_A, False),
        ("removing", _SHA1_F, "x" * 40, None, False),
        ("bogus", None, None, None, False),
    ],
)
def test_checkpoint_ref_intent_combinations_sha1(intent, accepted, expected_old, proposed_new, valid):
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": intent,
        "accepted_sha": accepted,
        "expected_old_sha": expected_old,
        "proposed_new_sha": proposed_new,
    }
    if valid:
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")
    else:
        with pytest.raises(ls.LifecycleStoreError):
            ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_present_valid_sha256():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": _SHA256_A,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_checkpoint_ref_advancing_valid_sha256():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "advancing",
        "accepted_sha": _SHA256_A,
        "expected_old_sha": _SHA256_A,
        "proposed_new_sha": _SHA256_B,
    }
    ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_checkpoint_ref_sha1_length_refused_against_sha256_repo():
    # A structurally valid sha1-length hex OID must be refused when the
    # repository's actual object format is sha256 -- object-format
    # awareness, not merely "is this some hex string."
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": _SHA1_A,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_checkpoint_ref_sha256_length_refused_against_sha1_repo():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": _SHA256_A,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_uppercase_hex_refused():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": _SHA1_A.upper(),
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_non_hex_refused():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": "g" * 40,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_placeholder_sha_refused():
    # The exact gap the correction pass closed: single-character
    # placeholders like "A"/"B" must never be accepted as persisted
    # SHAs.
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": "A",
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_wrong_length_hex_refused():
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": "a" * 39,  # one short of sha1's 40
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_ref_zero_oid_refused_as_persisted_value_sha1():
    # ADR 0004 section 5 is explicit: null means "no ref"; the zero OID
    # appears only in Git argv (checkpoint_ref.ObjectFormat.zero_oid),
    # never in a persisted transition record. The zero OID is
    # otherwise a structurally valid-length, valid-hex string, so this
    # must be enforced as its own rule, not merely by the generic hex
    # shape check.
    zero_sha1 = "0" * 40
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": zero_sha1,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID


def test_checkpoint_ref_zero_oid_refused_as_persisted_value_sha256():
    zero_sha256 = "0" * 64
    payload = _valid_payload()
    payload["checkpoint_ref"] = {
        "intent": "present",
        "accepted_sha": zero_sha256,
        "expected_old_sha": None,
        "proposed_new_sha": None,
    }
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.validate_lifecycle_json_schema(payload, object_format="sha256")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID


@pytest.mark.parametrize("field_name", ["accepted_sha", "expected_old_sha", "proposed_new_sha"])
def test_checkpoint_ref_zero_oid_refused_in_every_sha_field(field_name):
    # The zero OID must be refused regardless of which of the three
    # SHA fields carries it, not only accepted_sha.
    payload = _valid_payload()
    checkpoint_ref = {
        "intent": "advancing",
        "accepted_sha": _SHA1_A,
        "expected_old_sha": _SHA1_A,
        "proposed_new_sha": _SHA1_B,
    }
    checkpoint_ref[field_name] = "0" * 40
    payload["checkpoint_ref"] = checkpoint_ref
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_checkpoint_transition_rejects_zero_oid_directly_sha1():
    # Load-bearing at the shared validation boundary itself
    # (checkpoint_session.CheckpointTransition), not only through the
    # lifecycle-store validator that reuses it.
    with pytest.raises(ValueError):
        cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha="0" * 40)


def test_checkpoint_transition_rejects_zero_oid_directly_sha256():
    with pytest.raises(ValueError):
        cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha="0" * 64)


def test_checkpoint_transition_accepts_ordinary_nonzero_sha1_and_sha256():
    # Confirms the zero-OID fix does not overreach: ordinary nonzero
    # OIDs of both supported lengths remain accepted.
    t1 = cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=_SHA1_A)
    assert t1.accepted_sha == _SHA1_A
    t2 = cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=_SHA256_A)
    assert t2.accepted_sha == _SHA256_A


def test_object_format_zero_oid_still_available_for_git_argv():
    # checkpoint_ref.ObjectFormat.zero_oid must remain unaffected --
    # it is a Git-argv-only value, never a persisted/in-memory
    # transition field, and the fix above must not interfere with it.
    from codeagent.checkpoint_ref import ObjectFormat

    assert ObjectFormat.SHA1.zero_oid == "0" * 40
    assert ObjectFormat.SHA256.zero_oid == "0" * 64


# ---------------------------------------------------------------------------
# worktree: ADR 0004 Amendment 10's exact persisted-combination table --
# updated from this module's prior "absent only" narrowing. See
# _validate_worktree_shape's own docstring.
# ---------------------------------------------------------------------------


def test_worktree_absent_with_expected_head_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "absent", "expected_head": _SHA1_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


@pytest.mark.parametrize("intent", ["creating", "present", "disposing"])
def test_worktree_non_absent_intent_with_valid_oid_accepted(intent):
    """ADR 0004 Amendment 10: a non-absent worktree shape with a valid,
    object-format-matching expected_head is now a genuinely accepted
    shape -- updated from this module's prior "categorically refused"
    assertion, which predated the amendment."""
    payload = _valid_payload()
    payload["worktree"] = {"intent": intent, "expected_head": _SHA1_A}
    validated = ls.validate_lifecycle_json_schema(payload, object_format="sha1")
    assert validated["worktree"] == {"intent": intent, "expected_head": _SHA1_A}


@pytest.mark.parametrize("intent", ["creating", "present", "disposing"])
def test_worktree_non_absent_intent_without_expected_head_refused(intent):
    """A non-absent intent with no expected_head remains refused -- the
    combination rule (non-absent requires non-null expected_head),
    unchanged by Amendment 10."""
    payload = _valid_payload()
    payload["worktree"] = {"intent": intent, "expected_head": None}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_present_valid_sha256():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": _SHA256_A}
    ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_worktree_sha1_length_refused_against_sha256_repo():
    # A structurally valid sha1-length hex OID must be refused when the
    # repository's actual object format is sha256 -- object-format
    # awareness, not merely "is this some hex string."
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": _SHA1_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_worktree_sha256_length_refused_against_sha1_repo():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": _SHA256_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_uppercase_hex_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": _SHA1_A.upper()}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_non_hex_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": "g" * 40}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_placeholder_sha_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": "A"}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_wrong_length_hex_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": _SHA1_A + "a"}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_zero_oid_refused_as_persisted_value_sha1():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": "0" * 40}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_zero_oid_refused_as_persisted_value_sha256():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "present", "expected_head": "0" * 64}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha256")


def test_worktree_unknown_intent_value_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "removing", "expected_head": _SHA1_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_unknown_key_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "absent", "expected_head": None, "extra": "x"}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_worktree_wrong_type_refused():
    payload = _valid_payload()
    payload["worktree"] = "not-a-dict"
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


# ---------------------------------------------------------------------------
# failure: narrowed to null only -- see _validate_failure_shape's own
# docstring for why.
# ---------------------------------------------------------------------------


def test_failure_populated_shape_categorically_refused():
    payload = _valid_payload(failure={"phase": "PREPARING", "detail": "forced"})
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_failure_malformed_shape_refused():
    payload_bad = _valid_payload(failure={"phase": "PREPARING"})
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload_bad, object_format="sha1")


def test_failure_null_accepted():
    payload = _valid_payload(failure=None)
    ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_reconciliation_recent_failures_bound():
    payload = _valid_payload(
        reconciliation={"attempts_total": 3, "recent_failures": ["x"] * 10}
    )
    ls.validate_lifecycle_json_schema(payload, object_format="sha1")

    payload_over = _valid_payload(
        reconciliation={"attempts_total": 3, "recent_failures": ["x"] * 11}
    )
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload_over, object_format="sha1")


def test_negative_attempts_total_refused():
    payload = _valid_payload(reconciliation={"attempts_total": -1, "recent_failures": []})
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


def test_duplicate_key_in_encoded_document_refused_on_readback(tmp_path):
    # canonical_json_loads_strict (reused unmodified from _lifecycle_fs)
    # already rejects duplicate keys; confirm the composition round-trip
    # actually goes through it.
    raw = b'{"schema_version":1,"schema_version":2}'
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)
    assert excinfo.value.reason is lf.LifecycleFsFailure.DUPLICATE_KEY


# ---------------------------------------------------------------------------
# Publication failure injection (before/after atomic replacement)
# ---------------------------------------------------------------------------


def test_write_failure_before_replace_leaves_no_lifecycle_json_and_cleans_temp(tmp_path, monkeypatch):
    # Outcome: "publication failed before installation."
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    def _boom(fd, data):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(lf, "write_all_eintr_safe", _boom)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls.prepare_lifecycle(str(repo), run_id="run-1")
        assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
        assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
        assert excinfo.value.__cause__.reason is lf.LifecycleFsFailure.IO_FAILED

    # Exactly one run directory was created (the failed attempt); it
    # must contain no lifecycle.json and no leftover temp file.
    runs_dir = None
    for repo_key_dir in (state_dir / "repos").iterdir():
        candidate = repo_key_dir / "runs"
        if candidate.is_dir():
            runs_dir = candidate
    assert runs_dir is not None
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    entries = os.listdir(run_dirs[0])
    assert ls.LIFECYCLE_JSON_FILENAME not in entries
    assert not any(name.startswith(f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-") for name in entries)

    # The locks were released, but the run directory above durably
    # exists with no `lifecycle.json` ever published into it. Slice
    # 3B-1's automatic pre-run reconciliation (ADR 0004 Amendment 2
    # section 7) refuses exactly this shape rather than adopting or
    # repairing it, so a fresh attempt now correctly blocks -- the
    # named residual risk in Amendment 2 (recovery requires future
    # abandonment, not implemented in this slice).
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-2")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED


def test_replace_failure_cleans_temp_and_leaves_no_final_file(tmp_path, monkeypatch):
    # Outcome: "publication failed before installation" (os.replace
    # itself never confirmed).
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("forced replace failure")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-1")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
    assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
    assert excinfo.value.__cause__.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE

    # No lifecycle.json and no leftover temp file anywhere.
    for repo_key_dir in (state_dir / "repos").iterdir():
        for run_dir in (repo_key_dir / "runs").iterdir():
            entries = os.listdir(run_dir)
            assert ls.LIFECYCLE_JSON_FILENAME not in entries
            assert not any(name.startswith(f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-") for name in entries)


def test_directory_fsync_failure_after_replace_is_distinct_outcome_and_leaves_file_present(tmp_path, monkeypatch):
    # Outcome: "installed but durability unconfirmed" -- the exact
    # ambiguity the correction pass fixes. This must be reported under
    # a reason distinct from a pre-installation fsync failure (see
    # test_write_failure_before_replace_leaves_no_lifecycle_json_and_cleans_temp
    # above, which also fails inside fsync_fd but is a completely
    # different outcome).
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)

    real_fsync_fd = lf.fsync_fd
    call_count = {"n": 0}

    def _fail_last_fsync(fd):
        call_count["n"] += 1
        # Let the temp-file fsync succeed; fail only the final
        # directory fsync (the last fsync_fd call this composition
        # makes).
        if call_count["n"] >= 2:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
        return real_fsync_fd(fd)

    monkeypatch.setattr(lf, "fsync_fd", _fail_last_fsync)

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-1")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
    assert excinfo.value.__cause__.reason is lf.LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED

    # The file itself was already installed by os.replace before the
    # directory-fsync failure -- it is left exactly as written, never
    # deleted or reverted, and no temp file remains.
    found = False
    for repo_key_dir in (state_dir / "repos").iterdir():
        for run_dir in (repo_key_dir / "runs").iterdir():
            entries = os.listdir(run_dir)
            assert not any(name.startswith(f".{ls.LIFECYCLE_JSON_FILENAME}.tmp-") for name in entries)
            candidate = run_dir / ls.LIFECYCLE_JSON_FILENAME
            if candidate.exists():
                found = True
                payload = lf.canonical_json_loads_strict(candidate.read_bytes(), max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)
                ls.validate_lifecycle_json_schema(payload, object_format="sha1")
    assert found, "lifecycle.json must remain after a directory-fsync-only failure"


def test_temp_file_cleanup_failure_chains_from_original_failure(tmp_path, monkeypatch):
    # Outcome: "exact-temp cleanup unconfirmed", chained from the
    # original pre-installation failure that triggered the cleanup
    # attempt.
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)

    def _boom_write(fd, data):
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

    def _boom_unlink(path, *, dir_fd=None):
        raise OSError("forced unlink failure")

    monkeypatch.setattr(lf, "write_all_eintr_safe", _boom_write)
    monkeypatch.setattr(os, "unlink", _boom_unlink)

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        ls.prepare_lifecycle(str(repo), run_id="run-1")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
    assert excinfo.value.__cause__.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__.__cause__, lf.LifecycleFsError)
    assert excinfo.value.__cause__.__cause__.reason is lf.LifecycleFsFailure.IO_FAILED


# ---------------------------------------------------------------------------
# Complete descriptor cleanup and release ordering under multi-stage failure
# ---------------------------------------------------------------------------


def test_close_attempts_every_stage_even_when_earlier_stages_fail(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")

    calls = {"lifecycle": 0, "run_dir": 0, "repo": 0, "state_root": 0}

    def _fail_lifecycle_release():
        calls["lifecycle"] += 1
        raise sl.LockError(sl.LockFailure.RELEASE_UNCONFIRMED, "forced")

    def _fail_close_confirmed(fds):
        if fds == [lease.run_dir_fd]:
            calls["run_dir"] += 1
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")
        return lf.close_confirmed(fds)

    def _fail_repo_release():
        calls["repo"] += 1
        raise sl.LockError(sl.LockFailure.RELEASE_UNCONFIRMED, "forced")

    def _fail_state_root_close():
        calls["state_root"] += 1
        raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

    lease.lifecycle_lock.release = _fail_lifecycle_release
    monkeypatch.setattr(ls, "close_confirmed", _fail_close_confirmed)
    lease.repository_lock.release = _fail_repo_release
    lease.state_root.close = _fail_state_root_close

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        lease.close()
    assert excinfo.value.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
    assert calls == {"lifecycle": 1, "run_dir": 1, "repo": 1, "state_root": 1}
    assert "lifecycle lock" in excinfo.value.message
    assert "run-directory descriptor" in excinfo.value.message
    assert "repository lock" in excinfo.value.message
    assert "state-root descriptor" in excinfo.value.message


def test_close_is_idempotent_after_success(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="run-1")
    lease.close()
    lease.close()  # must not raise or double-release


# ---------------------------------------------------------------------------
# Proof: no container, worktree, or checkpoint-ref mutation is reachable
# ---------------------------------------------------------------------------


def test_no_container_worktree_or_checkpoint_ref_mutation_reachable():
    # AST-based, not substring-based: this module's own docstrings
    # legitimately discuss Docker/worktrees/checkpoint refs in prose
    # (describing what must NOT exist yet); only actual code references
    # (names, attribute access) count as a reachability finding.
    import ast

    source = __import__("pathlib").Path(ls.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_identifiers = {"docker", "Docker", "DockerVerifier", "GitWorktree", "subprocess"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_identifiers:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_identifiers:
            found.add(node.attr)
    assert not found, f"forbidden identifiers referenced as code: {found}"

    # CheckpointRef itself is legitimately imported only for
    # new_lifecycle_id(); no mutation method may be referenced.
    forbidden_checkpoint_ref_calls = {"create", "advance", "delete"}
    called_attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not (called_attrs & forbidden_checkpoint_ref_calls)


def test_lifecycle_store_imports_no_docker_or_workspace_modules():
    import ast

    tree = ast.parse(__import__("pathlib").Path(ls.__file__).read_text(encoding="utf-8"))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    forbidden_modules = {"codeagent.executor", "codeagent.workspace", "codeagent.patch", "docker"}
    assert not (imported_modules & forbidden_modules)


# ---------------------------------------------------------------------------
# Cross-process exclusion and crash/kill-point evidence
# ---------------------------------------------------------------------------


def _child_prepare_and_wait(repo_path: str, state_dir: str, run_id: str, ready_evt, release_evt) -> None:
    import os as _os

    _os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as _ls

    lease = _ls.prepare_lifecycle(repo_path, run_id=run_id)
    ready_evt.set()
    release_evt.wait(timeout=30)
    lease.close()


def test_real_two_process_repository_lock_exclusion(tmp_path):
    repo = _make_repo(tmp_path)
    state_dir = str(tmp_path / "state-root")
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_child_prepare_and_wait, args=(str(repo), state_dir, "child-run", ready_evt, release_evt)
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        os.environ["CODEAGENT_STATE_DIR"] = state_dir
        with pytest.raises(sl.LockError) as excinfo:
            ls.prepare_lifecycle(str(repo), run_id="parent-run")
        assert excinfo.value.reason is sl.LockFailure.BUSY
    finally:
        del os.environ["CODEAGENT_STATE_DIR"]
        release_evt.set()
        proc.join(timeout=10)


def _child_prepare_hold_lifecycle_lock_then_die(
    repo_path: str, state_dir: str, run_id: str, ready_evt
) -> None:
    import os as _os

    _os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.lifecycle_store as _ls

    _ls.prepare_lifecycle(repo_path, run_id=run_id)
    ready_evt.set()
    import time as _time

    _time.sleep(30)  # SIGKILLed by the parent long before this returns


def test_real_lock_acquirable_after_holder_sigkilled_before_close(tmp_path):
    repo = _make_repo(tmp_path)
    state_dir = str(tmp_path / "state-root")
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    proc = ctx.Process(
        target=_child_prepare_hold_lifecycle_lock_then_die,
        args=(str(repo), state_dir, "victim-run", ready_evt),
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=10)
        assert proc.exitcode is not None and proc.exitcode != 0

        os.environ["CODEAGENT_STATE_DIR"] = state_dir
        # The repository lock is released by the kernel when the
        # SIGKILLed process's descriptor table is torn down; a fresh
        # attempt must succeed with a brand-new lifecycle_id, never
        # reusing or adopting the dead run's directory (no
        # reconciliation exists yet in this slice).
        lease = ls.prepare_lifecycle(str(repo), run_id="fresh-run")
        try:
            assert lease.lifecycle_id
            # The dead run's own projection is left exactly as it was
            # -- never touched, adopted, or deleted.
            state_dir_path = __import__("pathlib").Path(state_dir)
            run_dirs = list((state_dir_path / "repos" / lease.repo_key / "runs").iterdir())
            assert len(run_dirs) == 2
        finally:
            lease.close()
    finally:
        os.environ.pop("CODEAGENT_STATE_DIR", None)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


def test_real_lifecycle_lock_cross_process_exclusion(tmp_path):
    """Two processes both reach the point of holding the repository
    lock sequentially (only one run at a time is possible, per T-E1's
    repository-lock exclusion) -- this test instead proves the
    lifecycle-lock primitive itself is cross-process exclusive by
    acquiring it directly a second time against the same run directory
    while the first (real, separate-process) holder is still alive."""
    repo = _make_repo(tmp_path)
    state_dir = str(tmp_path / "state-root")
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_child_prepare_and_wait, args=(str(repo), state_dir, "holder-run", ready_evt, release_evt)
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=10)
        os.environ["CODEAGENT_STATE_DIR"] = state_dir
        # The repository lock is held by the child, so a second
        # composition attempt is refused at that (earlier) point --
        # confirming the repository lock, not merely the lifecycle
        # lock, is what a concurrent run collides with first.
        with pytest.raises(sl.LockError) as excinfo:
            ls.prepare_lifecycle(str(repo), run_id="contender-run")
        assert excinfo.value.reason is sl.LockFailure.BUSY
    finally:
        del os.environ["CODEAGENT_STATE_DIR"]
        release_evt.set()
        proc.join(timeout=10)


# ---------------------------------------------------------------------------
# Slice 3B-2: locked, authoritative lifecycle-projection writer
# ---------------------------------------------------------------------------


def _prepared_lease(tmp_path, monkeypatch, *, name="repo"):
    repo = _make_repo(tmp_path, name=name)
    _set_state_dir(monkeypatch, tmp_path)
    return ls.prepare_lifecycle(str(repo), run_id="writer-run")


# --- Factory validation ---


def test_open_projection_writer_succeeds_and_returns_initial_projection(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        assert isinstance(writer, ls._LifecycleProjectionWriter)
        assert current.state == ls.LifecycleState.PREPARING
        assert current.lifecycle_id == lease.lifecycle_id
    finally:
        lease.close()


def test_open_projection_writer_refuses_after_close(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    lease.close()
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        lease.open_projection_writer()
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


def test_open_projection_writer_refuses_repository_kind_lock_substituted(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        # Substitute a repository-kind lock in place of the lifecycle
        # lock -- never a legal scope for a projection write.
        fake_lock = object.__new__(sl.LockHandle)
        fake_lock._fd = -1
        fake_lock.scope = sl.LockScope(kind=sl.LockKind.REPOSITORY, repo_key=lease.repo_key)
        fake_lock._held = True
        fake_lock.diagnostic_path = "<fake>"
        real_lock = lease.lifecycle_lock
        lease.lifecycle_lock = fake_lock
        try:
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                lease.open_projection_writer()
            assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
        finally:
            lease.lifecycle_lock = real_lock
    finally:
        lease.close()


def test_open_projection_writer_refuses_mismatched_lifecycle_id_scope(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        fake_lock = object.__new__(sl.LockHandle)
        fake_lock._fd = -1
        fake_lock.scope = sl.LockScope(kind=sl.LockKind.LIFECYCLE, repo_key=lease.repo_key, lifecycle_id="f" * 32)
        fake_lock._held = True
        fake_lock.diagnostic_path = "<fake>"
        real_lock = lease.lifecycle_lock
        lease.lifecycle_lock = fake_lock
        try:
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                lease.open_projection_writer()
            assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
        finally:
            lease.lifecycle_lock = real_lock
    finally:
        lease.close()


def test_open_projection_writer_refuses_incomplete_lease_no_raw_error():
    bare = ls.LifecycleLease()
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        bare.open_projection_writer()
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


# --- Authoritative read / stale-expectation prevention ---


def test_write_refuses_stale_expected_projection(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        # `initial` is now stale -- the durable state has moved on.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
    finally:
        lease.close()


def test_write_refuses_corrupt_projection_without_overwriting(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()

        corrupt_bytes = b"not json at all"
        fd = os.open(ls.LIFECYCLE_JSON_FILENAME, os.O_WRONLY | os.O_TRUNC, dir_fd=lease.run_dir_fd)
        try:
            os.write(fd, corrupt_bytes)
        finally:
            os.close(fd)

        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID

        # Never overwritten: the corrupt bytes are exactly unchanged.
        fd = os.open(ls.LIFECYCLE_JSON_FILENAME, os.O_RDONLY, dir_fd=lease.run_dir_fd)
        try:
            assert os.read(fd, 65536) == corrupt_bytes
        finally:
            os.close(fd)
    finally:
        lease.close()


# --- Publication outcomes ---


def test_publication_pre_install_failure_leaves_old_expected_valid(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()

        def _boom(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
            assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED

        # Nothing installed: `initial` is still authoritative, and a
        # retry with the same `expected` is accepted (not stale).
        result = writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        assert result.state == ls.LifecycleState.ACTIVE
    finally:
        lease.close()


def test_publication_durability_unconfirmed_requires_refresh(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
            assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # The old `expected` (PREPARING) is now stale: the new content
        # (ACTIVE) is currently installed, but its directory-entry
        # durability was not confirmed by this process.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION

        # The mandated recovery path: refresh() reads the authoritative
        # installed value, which the caller then reconsiders.
        current = writer.refresh()
        assert current.state == ls.LifecycleState.ACTIVE
        result = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        assert result.state == ls.LifecycleState.CLEANING
    finally:
        lease.close()


def test_publication_cleanup_unconfirmed_is_distinct_and_fails_closed(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()

        def _fail_write(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

        def _fail_unlink(path, *, dir_fd):
            raise OSError("forced temp-cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _fail_write)
            scoped.setattr(lf.os, "unlink", _fail_unlink)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
            assert excinfo.value.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
    finally:
        lease.close()


def test_publication_success_returns_newly_confirmed_projection(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        result = writer.advance_lifecycle_state(expected=initial, state=ls.LifecycleState.ACTIVE)
        assert result.state == ls.LifecycleState.ACTIVE
        on_disk = _read_lifecycle_json(lease)
        assert on_disk["state"] == "ACTIVE"
    finally:
        lease.close()


def _read_lifecycle_json(lease):
    fd = os.open(ls.LIFECYCLE_JSON_FILENAME, os.O_RDONLY, dir_fd=lease.run_dir_fd)
    try:
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    return lf.canonical_json_loads_strict(data, max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)


# --- Owner lifecycle-state edges ---


def test_owner_state_happy_path_to_complete(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)
        assert current.state == ls.LifecycleState.COMPLETE
    finally:
        lease.close()


def test_owner_state_same_state_no_op_zero_publication_io(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for a no-op")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            result = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.PREPARING)
        assert result == current
    finally:
        lease.close()


def test_owner_state_skips_are_refused(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_owner_state_refuses_reconciler_state_even_if_identical(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        reconciling = dataclasses.replace(
            initial,
            state=ls.LifecycleState.RECONCILING,
            reconciliation=dataclasses.replace(initial.reconciliation, attempts_total=1),
        )
        data = lf.canonical_json_dumps(ls.projection_to_dict(reconciling))
        ls.publish_private_file_atomically_at(lease.run_dir_fd, ls.LIFECYCLE_JSON_FILENAME, data, mode=0o600)

        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=reconciling, state=ls.LifecycleState.RECONCILING)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_cleaning_to_complete_refused_with_genuinely_dirty_container(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        # Genuinely dirty the durable projection via the writer's own
        # container API -- not a fabricated in-memory belief -- so the
        # guard is exercised against the real authoritative state.
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id="a" * 64
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def _publish_raw(lease, projection: ls.LifecycleProjection) -> None:
    data = lf.canonical_json_dumps(ls.projection_to_dict(projection))
    ls.publish_private_file_atomically_at(lease.run_dir_fd, ls.LIFECYCLE_JSON_FILENAME, data, mode=0o600)


def test_cleaning_to_complete_worktree_dirty_shape_is_refused_by_clean_final_guard(tmp_path, monkeypatch):
    """Updated by the worktree-attribution substrate slice (ADR 0004
    Amendment 10): a non-absent worktree shape with a valid OID is now
    genuinely loadable (the prior "SCHEMA_INVALID, intercepted before
    the clean-final guard ever runs" assertion predates Amendment 10),
    so the CLEANING->COMPLETE clean-final guard's own worktree check is
    now actually reachable in practice, for the first time, and
    correctly refuses with ILLEGAL_TRANSITION rather than SCHEMA_INVALID."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        dirty = dataclasses.replace(
            current, worktree=ls.WorktreeTransition(intent=ls.WorktreeIntent.PRESENT, expected_head=_SHA1_A)
        )
        _publish_raw(lease, dirty)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=dirty, state=ls.LifecycleState.COMPLETE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_cleaning_to_complete_populated_failure_shape_is_unloadable(tmp_path, monkeypatch):
    """Populated `failure` writing is deferred, same narrowing as
    above: the schema validator refuses to load it at all, so this is
    also SCHEMA_INVALID, not ILLEGAL_TRANSITION -- see the worktree
    test's docstring for the full explanation."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        dirty = dataclasses.replace(current, failure=ls.FailureDetail(phase="p", detail="d"))
        _publish_raw(lease, dirty)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=dirty, state=ls.LifecycleState.COMPLETE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID
    finally:
        lease.close()


def test_cleaning_to_complete_succeeds_when_genuinely_clean(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        result = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)
        assert result.state == ls.LifecycleState.COMPLETE
    finally:
        lease.close()


# --- Container transition edges ---


def test_container_full_happy_path_round_trip(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        assert current.baseline.intent is ls.ContainerIntent.CREATING
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id="c" * 64
        )
        assert current.baseline.id == "c" * 64
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.REMOVING, id="c" * 64
        )
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None
        )
        assert current.baseline.intent is ls.ContainerIntent.ABSENT
    finally:
        lease.close()


def test_container_failed_create_recovery_edge(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_container_transition(
            expected=current, role="verification", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_container_transition(
            expected=current, role="verification", intent=ls.ContainerIntent.ABSENT, id=None
        )
        assert current.verification.intent is ls.ContainerIntent.ABSENT
    finally:
        lease.close()


def test_container_exact_tuple_no_op_zero_publication_io(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for a no-op")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            result = writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None
            )
        assert result == current
    finally:
        lease.close()


@pytest.mark.parametrize(
    "from_intent,from_id,to_intent,to_id",
    [
        (ls.ContainerIntent.ABSENT, None, ls.ContainerIntent.PRESENT, "a" * 64),
        (ls.ContainerIntent.ABSENT, None, ls.ContainerIntent.REMOVING, "a" * 64),
    ],
)
def test_container_illegal_edges_from_absent(tmp_path, monkeypatch, from_intent, from_id, to_intent, to_id):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(expected=current, role="baseline", intent=to_intent, id=to_id)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_present_to_present_different_id_is_illegal(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id="a" * 64
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id="b" * 64
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_removing_with_different_id_is_illegal(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id="a" * 64
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.REMOVING, id="b" * 64
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_shape_guard_absent_with_id_refused(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id="a" * 64
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_shape_guard_present_without_id_refused(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.PRESENT, id=None
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_role_isolated(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        assert current.verification.intent is ls.ContainerIntent.ABSENT
    finally:
        lease.close()


# --- Checkpoint-ref transition edges (SHA continuity) ---


def _sha(seed: str) -> str:
    return (seed * 40)[:40]


def test_checkpoint_ref_full_happy_path_round_trip(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        initial_sha = _sha("1")
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=initial_sha),
        )
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=initial_sha),
        )
        assert current.checkpoint_ref.accepted_sha == initial_sha

        new_sha = _sha("2")
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(
                intent=cs.CheckpointIntent.ADVANCING,
                accepted_sha=initial_sha,
                expected_old_sha=initial_sha,
                proposed_new_sha=new_sha,
            ),
        )
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=new_sha),
        )
        assert current.checkpoint_ref.accepted_sha == new_sha

        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(
                intent=cs.CheckpointIntent.REMOVING, accepted_sha=new_sha, expected_old_sha=new_sha
            ),
        )
        current = writer.record_checkpoint_ref_transition(
            expected=current, transition=cs.ABSENT_TRANSITION
        )
        assert current.checkpoint_ref == cs.ABSENT_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_create_failure_recovery(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")),
        )
        current = writer.record_checkpoint_ref_transition(expected=current, transition=cs.ABSENT_TRANSITION)
        assert current.checkpoint_ref == cs.ABSENT_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_advance_collapse_to_old_sha_on_failure(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        initial_sha = _sha("1")
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=initial_sha),
        )
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=initial_sha),
        )
        new_sha = _sha("2")
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(
                intent=cs.CheckpointIntent.ADVANCING,
                accepted_sha=initial_sha,
                expected_old_sha=initial_sha,
                proposed_new_sha=new_sha,
            ),
        )
        # Failed advance, confirmed still at the old SHA.
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=initial_sha),
        )
        assert current.checkpoint_ref.accepted_sha == initial_sha
    finally:
        lease.close()


def test_checkpoint_ref_discontinuous_sha_is_illegal(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        initial_sha = _sha("1")
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=initial_sha),
        )
        # creating -> present must confirm the *same* proposed SHA.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(
                expected=current,
                transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=_sha("9")),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION

        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=initial_sha),
        )
        # advancing -> present with a totally unrelated third SHA.
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(
                intent=cs.CheckpointIntent.ADVANCING,
                accepted_sha=initial_sha,
                expected_old_sha=initial_sha,
                proposed_new_sha=_sha("2"),
            ),
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(
                expected=current,
                transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=_sha("9")),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_wrong_object_format_length_is_illegal(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        assert lease.object_format == "sha1"
        sha256_looking = ("a" * 64)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(
                expected=current,
                transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=sha256_looking),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_exact_no_op_zero_publication_io(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for a no-op")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            result = writer.record_checkpoint_ref_transition(expected=current, transition=cs.ABSENT_TRANSITION)
        assert result == current
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Correction pass: resource transitions refused outside owner-controlled
# nonterminal states (COMPLETE / reconciler-owned)
# ---------------------------------------------------------------------------


def _drive_to_complete(writer, current):
    current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
    current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
    return writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)


def test_container_absent_to_creating_refused_from_complete(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = _drive_to_complete(writer, current)
        assert current.state == ls.LifecycleState.COMPLETE
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_absent_to_creating_refused_from_complete(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = _drive_to_complete(writer, current)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(
                expected=current,
                transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_exact_no_op_refused_from_complete(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = _drive_to_complete(writer, current)

        def _boom(*a, **k):
            raise AssertionError("must not even be reached for a COMPLETE projection")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.record_container_transition(
                    expected=current, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None
                )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_exact_no_op_refused_from_complete(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = _drive_to_complete(writer, current)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(expected=current, transition=cs.ABSENT_TRANSITION)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


@pytest.mark.parametrize(
    "reconciler_state",
    [ls.LifecycleState.RECONCILING, ls.LifecycleState.RECONCILED, ls.LifecycleState.RECONCILIATION_FAILED],
)
def test_container_transition_refused_from_reconciler_owned_states(tmp_path, monkeypatch, reconciler_state):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        reconciler_owned = dataclasses.replace(initial, state=reconciler_state)
        _publish_raw(lease, reconciler_owned)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=reconciler_owned, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        # Even an exact no-op is refused.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_container_transition(
                expected=reconciler_owned, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


@pytest.mark.parametrize(
    "reconciler_state",
    [ls.LifecycleState.RECONCILING, ls.LifecycleState.RECONCILED, ls.LifecycleState.RECONCILIATION_FAILED],
)
def test_checkpoint_ref_transition_refused_from_reconciler_owned_states(tmp_path, monkeypatch, reconciler_state):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        reconciler_owned = dataclasses.replace(initial, state=reconciler_state)
        _publish_raw(lease, reconciler_owned)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(
                expected=reconciler_owned,
                transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        # Even an exact no-op is refused.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_checkpoint_ref_transition(expected=reconciler_owned, transition=cs.ABSENT_TRANSITION)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_container_and_checkpoint_ref_transitions_still_legal_in_active_and_cleaning(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_checkpoint_ref_transition(
            expected=current,
            transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")),
        )
        current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None
        )
        current = writer.record_checkpoint_ref_transition(expected=current, transition=cs.ABSENT_TRANSITION)

        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        current = writer.record_container_transition(
            expected=current, role="verification", intent=ls.ContainerIntent.CREATING, id=None
        )
        current = writer.record_container_transition(
            expected=current, role="verification", intent=ls.ContainerIntent.ABSENT, id=None
        )
        assert current.state == ls.LifecycleState.CLEANING
        assert current.verification.intent is ls.ContainerIntent.ABSENT
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Correction pass: uniform writer-call ordering (lock check first)
# ---------------------------------------------------------------------------


def test_container_transition_invalid_role_on_released_lock_gives_wrong_lock_scope(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        writer.record_container_transition(
            expected=current, role="not-a-real-role", intent=ls.ContainerIntent.CREATING, id=None
        )
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


def test_checkpoint_ref_transition_invalid_type_on_released_lock_gives_wrong_lock_scope(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        writer.record_checkpoint_ref_transition(expected=current, transition="not-a-transition")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


# ---------------------------------------------------------------------------
# Correction pass: sanitized invalid checkpoint-transition inputs
# ---------------------------------------------------------------------------


def test_checkpoint_ref_transition_none_is_illegal_not_raw_error(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for an invalid transition")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.record_checkpoint_ref_transition(expected=current, transition=None)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_checkpoint_ref_transition_wrong_type_is_illegal_not_raw_error(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for an invalid transition")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.record_checkpoint_ref_transition(expected=current, transition={"intent": "absent"})
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Slice 3B-3: LifecycleCheckpointRefPublisher adapter
# ---------------------------------------------------------------------------


def test_publisher_adapter_success_threads_returned_projection_forward(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))

        assert publisher.current.checkpoint_ref.intent is cs.CheckpointIntent.CREATING
        assert publisher.current != current  # the stored projection advanced
    finally:
        lease.close()


def test_publisher_adapter_repeated_legal_transitions_use_updated_expected(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        initial_sha = _sha("1")
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=initial_sha))
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=initial_sha))

        assert publisher.current.checkpoint_ref.accepted_sha == initial_sha

        new_sha = _sha("2")
        publisher.publish(
            cs.CheckpointTransition(
                intent=cs.CheckpointIntent.ADVANCING,
                accepted_sha=initial_sha,
                expected_old_sha=initial_sha,
                proposed_new_sha=new_sha,
            )
        )
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=new_sha))
        assert publisher.current.checkpoint_ref.accepted_sha == new_sha
    finally:
        lease.close()


def test_publisher_adapter_does_not_hide_stale_expectation(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)
        # A second, independent writer publishes behind the adapter's back.
        real_current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        assert real_current != current

        with pytest.raises(cs.CheckpointPublicationError) as excinfo:
            publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))
        assert excinfo.value.reason is cs.CheckpointPublicationFailure.STALE_EXPECTATION
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
        # The adapter's own belief is untouched by the failed call.
        assert publisher.current == current
    finally:
        lease.close()


def test_publisher_adapter_pre_installation_failure_leaves_expected_unchanged(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        def _boom(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _boom)
            with pytest.raises(cs.CheckpointPublicationError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is cs.CheckpointPublicationFailure.NOT_INSTALLED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
        assert publisher.current == current
    finally:
        lease.close()


def test_publisher_adapter_durability_unconfirmed_does_not_silently_update_and_refresh_recovers(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(cs.CheckpointPublicationError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is cs.CheckpointPublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # Never silently treated as success: the adapter's own belief
        # is unchanged.
        assert publisher.current == current

        # Explicit refresh() recovers: currently installed content is
        # returned and becomes the new expectation.
        refreshed = publisher.refresh()
        assert refreshed.checkpoint_ref.intent is cs.CheckpointIntent.CREATING
        assert publisher.current is refreshed

        # The now-correct expectation lets a further legal transition
        # succeed.
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=_sha("1")))
        assert publisher.current.checkpoint_ref.accepted_sha == _sha("1")
    finally:
        lease.close()


def test_publisher_adapter_cleanup_unconfirmed_remains_distinct_and_propagates(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        def _fail_write(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

        def _fail_unlink(path, *, dir_fd):
            raise OSError("forced temp-cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _fail_write)
            scoped.setattr(lf.os, "unlink", _fail_unlink)
            with pytest.raises(cs.CheckpointPublicationError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is cs.CheckpointPublicationFailure.CLEANUP_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
        assert publisher.current == current
    finally:
        lease.close()


def test_publisher_adapter_wrong_lock_scope_propagates_unchanged(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

    with pytest.raises(cs.CheckpointPublicationError) as excinfo:
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))
    assert excinfo.value.reason is cs.CheckpointPublicationFailure.WRONG_LOCK_SCOPE
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
    assert publisher.current == current


# ---------------------------------------------------------------------------
# Slice 3B-3: real production-shaped integration -- no mocking of the
# durable writer, real repo, real CheckpointRef, real CheckpointSession
# ---------------------------------------------------------------------------


def test_real_checkpoint_session_durably_publishes_through_the_real_writer(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="publisher-integration")
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

        checkpoint_ref = cr.CheckpointRef(str(repo), lease.lifecycle_id)
        session = cs.CheckpointSession(checkpoint_ref, transition_publisher=publisher)

        first_sha = _head(repo)

        session.establish(first_sha)
        on_disk = _read_lifecycle_json(lease)
        assert on_disk["checkpoint_ref"]["intent"] == "present"
        assert on_disk["checkpoint_ref"]["accepted_sha"] == first_sha
        assert checkpoint_ref.observe() == cr.RefObservation(present=True, oid=first_sha)

        (repo / "f.txt").write_text("y")
        _run("git", "-C", str(repo), "add", ".")
        _run("git", "-C", str(repo), "commit", "-qm", "second")
        second_sha = _head(repo)

        session.advance(second_sha)
        on_disk = _read_lifecycle_json(lease)
        assert on_disk["checkpoint_ref"]["intent"] == "present"
        assert on_disk["checkpoint_ref"]["accepted_sha"] == second_sha
        assert checkpoint_ref.observe() == cr.RefObservation(present=True, oid=second_sha)

        session.delete()
        on_disk = _read_lifecycle_json(lease)
        assert on_disk["checkpoint_ref"]["intent"] == "absent"
        assert checkpoint_ref.observe() == cr.RefObservation(present=False, oid=None)

        # No leaked worktrees or extra refs after delete().
        assert _run("git", "-C", str(repo), "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
        assert "refs/codeagent/" not in _run("git", "-C", str(repo), "for-each-ref").stdout
    finally:
        lease.close()


def _head(repo) -> str:
    return _run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Slice 3B-5 correction pass, finding 5: direct tests for the reconciler-
# owned writer `_publish_reconciler_container_transition` (ADR 0004
# Amendment 5). These are deliberately unit-level and fd-only -- no
# lease, no lock, no Docker/Git call -- exercising every validation
# rule the function itself owns, independent of the full
# `reconcile_repository()` pipeline already covered in
# `test_reconciliation.py`.
# ---------------------------------------------------------------------------


def _open_run_dir_fd(tmp_path, name="run"):
    run_dir = tmp_path / name
    run_dir.mkdir()
    return os.open(str(run_dir), os.O_RDONLY | os.O_DIRECTORY), run_dir


def _base_projection(*, lifecycle_id="a" * 32, state=ls.LifecycleState.PREPARING, attempts_total=0):
    projection = ls.build_initial_preparing_projection(
        lifecycle_id=lifecycle_id,
        state_root_id="s" * 16,
        repo_key="r" * 32,
        run_id="run-1",
        source_repo_path="/tmp/x",
    )
    return dataclasses.replace(
        projection,
        state=state,
        reconciliation=ls.ReconciliationSummary(attempts_total=attempts_total, recent_failures=()),
    )


def _with_role(projection, *, role, intent, id):
    attr = ls.ContainerAttribution(intent=intent, id=id)
    if role == "baseline":
        return dataclasses.replace(projection, baseline=attr)
    return dataclasses.replace(projection, verification=attr)


def _role_attr(projection, role):
    return projection.baseline if role == "baseline" else projection.verification


def _read_back(run_dir):
    data = (run_dir / ls.LIFECYCLE_JSON_FILENAME).read_bytes()
    return lf.canonical_json_loads_strict(data, max_bytes=ls.LIFECYCLE_JSON_MAX_BYTES)


_LEGAL_RECONCILER_EDGES = [
    (ls.ContainerIntent.CREATING, None, ls.ContainerIntent.ABSENT, None),
    (ls.ContainerIntent.CREATING, None, ls.ContainerIntent.REMOVING, "1" * 64),
    (ls.ContainerIntent.PRESENT, "2" * 64, ls.ContainerIntent.REMOVING, "2" * 64),
    (ls.ContainerIntent.REMOVING, "3" * 64, ls.ContainerIntent.ABSENT, None),
]


@pytest.mark.parametrize("from_intent,from_id,to_intent,to_id", _LEGAL_RECONCILER_EDGES)
def test_reconciler_writer_exact_legal_edges(tmp_path, from_intent, from_id, to_intent, to_id):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        projection = _base_projection()
        projection = _with_role(projection, role="baseline", intent=from_intent, id=from_id)
        ls.publish_private_file_atomically_at(
            fd, ls.LIFECYCLE_JSON_FILENAME, lf.canonical_json_dumps(ls.projection_to_dict(projection)), mode=0o600
        )

        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=to_intent, id=to_id, attempts_total=1
        )
        assert _role_attr(updated, "baseline") == ls.ContainerAttribution(intent=to_intent, id=to_id)
        assert updated.state is ls.LifecycleState.RECONCILING
        assert updated.reconciliation.attempts_total == 1

        on_disk = _read_back(run_dir)
        assert on_disk["state"] == "RECONCILING"
        assert on_disk["containers"]["baseline"] == {"intent": to_intent.value, "id": to_id}
    finally:
        os.close(fd)


_ILLEGAL_RECONCILER_EDGES = [
    (ls.ContainerIntent.ABSENT, None, ls.ContainerIntent.CREATING, None),  # live-owner-only edge
    (ls.ContainerIntent.CREATING, None, ls.ContainerIntent.PRESENT, "4" * 64),
    (ls.ContainerIntent.ABSENT, None, ls.ContainerIntent.REMOVING, "5" * 64),
    (ls.ContainerIntent.PRESENT, "6" * 64, ls.ContainerIntent.ABSENT, None),  # no direct present->absent edge
    (ls.ContainerIntent.PRESENT, "7" * 64, ls.ContainerIntent.CREATING, None),
]


@pytest.mark.parametrize("from_intent,from_id,to_intent,to_id", _ILLEGAL_RECONCILER_EDGES)
def test_reconciler_writer_illegal_edges(tmp_path, from_intent, from_id, to_intent, to_id):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(_base_projection(), role="baseline", intent=from_intent, id=from_id)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=to_intent, id=to_id, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "state",
    [ls.LifecycleState.COMPLETE, ls.LifecycleState.RECONCILED, ls.LifecycleState.RECONCILIATION_FAILED],
)
def test_reconciler_writer_rejects_ineligible_state(tmp_path, state):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _base_projection(state=state)
        projection = _with_role(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "state,attempts_total",
    [
        (ls.LifecycleState.PREPARING, 0),
        (ls.LifecycleState.ACTIVE, 3),
        (ls.LifecycleState.CLEANING, 5),
        (ls.LifecycleState.RECONCILING, 2),
    ],
)
def test_reconciler_writer_accepts_every_eligible_state(tmp_path, state, attempts_total):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _base_projection(state=state, attempts_total=attempts_total)
        projection = _with_role(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        expected_attempts = attempts_total if state is ls.LifecycleState.RECONCILING else attempts_total + 1
        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=expected_attempts
        )
        assert updated.state is ls.LifecycleState.RECONCILING
        assert updated.reconciliation.attempts_total == expected_attempts
    finally:
        os.close(fd)


def test_reconciler_writer_rejects_bad_role(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(_base_projection(), role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="not-a-role", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "intent,id",
    [
        (ls.ContainerIntent.ABSENT, "8" * 64),  # absent must carry no id
        (ls.ContainerIntent.CREATING, "9" * 64),  # creating must carry no id
        (ls.ContainerIntent.PRESENT, None),  # present requires an id
        (ls.ContainerIntent.REMOVING, ""),  # removing requires a nonempty id
        (ls.ContainerIntent.REMOVING, "not-hex"),  # must be 64 lowercase hex
        (ls.ContainerIntent.REMOVING, "A" * 64),  # uppercase rejected
        (ls.ContainerIntent.REMOVING, "a" * 63),  # too short
    ],
)
def test_reconciler_writer_rejects_bad_id_grammar(tmp_path, intent, id):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(_base_projection(), role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=intent, id=id, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


def test_reconciler_writer_fresh_cycle_requires_exact_increment(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.PREPARING, attempts_total=0),
            role="baseline",
            intent=ls.ContainerIntent.CREATING,
            id=None,
        )
        for bad_attempts in (0, 2):
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                ls._publish_reconciler_container_transition(
                    fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=bad_attempts
                )
            assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


def test_reconciler_writer_resumed_cycle_rejects_double_increment(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=4),
            role="baseline",
            intent=ls.ContainerIntent.CREATING,
            id=None,
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=5
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


def test_reconciler_writer_true_noop_publishes_nothing(tmp_path, monkeypatch):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1),
            role="baseline",
            intent=ls.ContainerIntent.REMOVING,
            id="a" * 64,
        )

        def _boom(*a, **k):
            raise AssertionError("must not publish for a true no-op")

        monkeypatch.setattr(ls, "publish_private_file_atomically_at", _boom)
        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=ls.ContainerIntent.REMOVING, id="a" * 64, attempts_total=1
        )
        assert updated is projection
    finally:
        os.close(fd)


def test_reconciler_writer_matching_attribution_but_wrong_attempts_is_not_a_noop(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        # A dead owner already durably wrote removing(id) while still in
        # PREPARING (not yet RECONCILING) -- attribution alone matches
        # what this call would compute, but state/attempts still require
        # a real write to reach RECONCILING.
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.PREPARING, attempts_total=0),
            role="baseline",
            intent=ls.ContainerIntent.REMOVING,
            id="b" * 64,
        )
        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=ls.ContainerIntent.REMOVING, id="b" * 64, attempts_total=1
        )
        assert updated is not projection
        assert updated.state is ls.LifecycleState.RECONCILING
        assert updated.reconciliation.attempts_total == 1
    finally:
        os.close(fd)


def test_reconciler_writer_removing_to_removing_same_id_is_allowed(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1),
            role="baseline",
            intent=ls.ContainerIntent.REMOVING,
            id="c" * 64,
        )
        # Same id, but a different (still-eligible) call shape than the
        # true no-op above -- attempts_total unchanged and id unchanged,
        # so this specific call *is* the no-op case; exercise the edge
        # logic directly by starting from CLEANING instead, which forces
        # a real state/attempts transition even though the id is retained.
        projection = dataclasses.replace(projection, state=ls.LifecycleState.CLEANING, reconciliation=ls.ReconciliationSummary(attempts_total=1, recent_failures=()))
        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=ls.ContainerIntent.REMOVING, id="c" * 64, attempts_total=2
        )
        assert _role_attr(updated, "baseline") == ls.ContainerAttribution(intent=ls.ContainerIntent.REMOVING, id="c" * 64)
    finally:
        os.close(fd)


def test_reconciler_writer_removing_to_removing_different_id_is_rejected(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(
            _base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1),
            role="baseline",
            intent=ls.ContainerIntent.REMOVING,
            id="d" * 64,
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=ls.ContainerIntent.REMOVING, id="e" * 64, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


def test_reconciler_writer_preserves_every_unrelated_field(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _base_projection(state=ls.LifecycleState.ACTIVE, attempts_total=0)
        projection = _with_role(projection, role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
        projection = _with_role(projection, role="verification", intent=ls.ContainerIntent.PRESENT, id="f" * 64)

        updated = ls._publish_reconciler_container_transition(
            fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=1
        )
        # Only baseline and state/attempts changed; everything else, byte for byte.
        assert updated.verification == projection.verification
        assert updated.worktree == projection.worktree
        assert updated.checkpoint_ref == projection.checkpoint_ref
        assert updated.failure == projection.failure
        assert updated.lifecycle_id == projection.lifecycle_id
        assert updated.state_root_id == projection.state_root_id
        assert updated.repo_key == projection.repo_key
        assert updated.run_id == projection.run_id
        assert updated.source_repo_path == projection.source_repo_path
    finally:
        os.close(fd)


def test_reconciler_writer_publication_failure_is_sanitized_and_classified(tmp_path, monkeypatch):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(_base_projection(), role="baseline", intent=ls.ContainerIntent.CREATING, id=None)

        def _boom(*a, **k):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

        monkeypatch.setattr(ls, "publish_private_file_atomically_at", _boom)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
    finally:
        os.close(fd)


def test_reconciler_writer_durability_unconfirmed_is_classified_distinctly(tmp_path, monkeypatch):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_role(_base_projection(), role="baseline", intent=ls.ContainerIntent.CREATING, id=None)

        def _boom(*a, **k):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED, "forced")

        monkeypatch.setattr(ls, "publish_private_file_atomically_at", _boom)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_container_transition(
                fd, projection, role="baseline", intent=ls.ContainerIntent.ABSENT, id=None, attempts_total=1
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Slice 3B-6: LifecycleContainerPublisher adapter (ADR 0004 Amendment 6)
# ---------------------------------------------------------------------------


def test_container_publication_failure_map_is_exhaustive():
    """Every current `LifecycleStoreFailure` member must have an
    explicit mapping entry — no `.get(..., default)` fallback anywhere.
    Run against the real enum so a future addition forces an explicit
    decision (a `KeyError` at `publish()` call time) rather than a
    silently missing or wrong classification."""
    assert set(ls._CONTAINER_PUBLICATION_FAILURE_MAP.keys()) == set(ls.LifecycleStoreFailure)


def test_container_publisher_adapter_success_threads_returned_projection_forward(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)

        publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)

        assert publisher.current.baseline.intent is ls.ContainerIntent.CREATING
        assert publisher.current != current
    finally:
        lease.close()


def test_publisher_adapter_serves_both_roles_against_one_shared_projection(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)

        publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
        publisher.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.CREATING, id=None)

        assert publisher.current.baseline.intent is ls.ContainerIntent.CREATING
        assert publisher.current.verification.intent is ls.ContainerIntent.CREATING
    finally:
        lease.close()


def test_container_publisher_adapter_does_not_hide_stale_expectation(tmp_path, monkeypatch):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)
        # A second, independent writer publishes behind the adapter's back.
        real_current = writer.record_checkpoint_ref_transition(
            expected=current, transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
        )
        assert real_current != current

        with pytest.raises(cl.ContainerPublicationError) as excinfo:
            publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
        assert excinfo.value.reason is cl.ContainerPublicationFailure.STALE_EXPECTATION
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
        assert publisher.current == current
    finally:
        lease.close()


def test_container_publisher_adapter_pre_installation_failure_leaves_expected_unchanged(tmp_path, monkeypatch):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)

        def _boom(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _boom)
            with pytest.raises(cl.ContainerPublicationError) as excinfo:
                publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
            assert excinfo.value.reason is cl.ContainerPublicationFailure.NOT_INSTALLED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
        assert publisher.current == current
    finally:
        lease.close()


def test_container_publisher_adapter_durability_unconfirmed_does_not_silently_update_and_refresh_recovers(
    tmp_path, monkeypatch
):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(cl.ContainerPublicationError) as excinfo:
                publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
            assert excinfo.value.reason is cl.ContainerPublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # Never silently treated as success: the adapter's own belief
        # is unchanged, and it is never automatically refreshed either.
        assert publisher.current == current

        refreshed = publisher.refresh()
        assert refreshed.baseline.intent is ls.ContainerIntent.CREATING
        assert publisher.current is refreshed
    finally:
        lease.close()


def test_container_publisher_adapter_wrong_lock_scope_is_translated(tmp_path, monkeypatch):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    publisher = ls.LifecycleContainerPublisher(writer, current)

    with pytest.raises(cl.ContainerPublicationError) as excinfo:
        publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
    assert excinfo.value.reason is cl.ContainerPublicationFailure.WRONG_LOCK_SCOPE
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
    assert publisher.current == current


def test_container_publisher_exposes_identity_from_current_projection(tmp_path, monkeypatch):
    """`LifecycleContainerPublisher.lifecycle_id`/`state_root_id`
    (Slice 3B-6 correction pass) are the single source of identity
    `executor.DockerVerifierLifecycleContext` derives its Docker-side
    naming/labeling identity from."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)
        assert publisher.lifecycle_id == current.lifecycle_id == lease.lifecycle_id
        assert publisher.state_root_id == current.state_root_id == lease.state_root.state_root_id
    finally:
        lease.close()


def test_container_publisher_mismatched_initial_projection_fails_before_any_docker_operation(
    tmp_path, monkeypatch
):
    """A publisher constructed with an `initial_projection` that does
    not actually match what is durably installed for the writer's own
    lease (a caller bug -- e.g. a stale or wrongly paired projection)
    is never silently trusted: the very first `publish()` call fails,
    strictly before a `DockerVerifier` using this publisher could ever
    have issued a real Docker mutation, since `DockerVerifier` always
    publishes `CREATING` before any Docker call."""
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        # A real, differently-shaped projection for the *same* lifecycle
        # -- the identity fields still validate, but it no longer
        # matches what is actually installed on disk, exactly the
        # "caller supplied the wrong snapshot" scenario this guards
        # against.
        stale_initial = dataclasses.replace(current, run_id="a-different-run-id-than-what-is-installed")
        publisher = ls.LifecycleContainerPublisher(writer, stale_initial)

        with pytest.raises(cl.ContainerPublicationError) as excinfo:
            publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
        assert excinfo.value.reason is cl.ContainerPublicationFailure.STALE_EXPECTATION
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
        # Nothing was published; the adapter's own belief is untouched.
        assert publisher.current == stale_initial
    finally:
        lease.close()


def test_publisher_adapter_illegal_transition_is_translated_to_container_publication_error(tmp_path, monkeypatch):
    """`lifecycle_store.LifecycleStoreError` (here: `ILLEGAL_TRANSITION` —
    `PRESENT` is not a legal edge from the initial `ABSENT`, only
    `CREATING` is) is translated by the adapter into a
    `container_lifecycle.ContainerPublicationError`, never propagated
    unchanged as a raw `LifecycleStoreError`, and the original is
    preserved as `__cause__` (ADR 0004 Amendment 6, executor-side
    dependency inversion)."""
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)

        with pytest.raises(cl.ContainerPublicationError) as excinfo:
            publisher.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.PRESENT, id="a" * 64)

        assert excinfo.value.reason is cl.ContainerPublicationFailure.ILLEGAL_TRANSITION
        assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        # The persistence-layer message never leaks into the public error.
        assert "ILLEGAL_TRANSITION" not in str(excinfo.value)
        assert publisher.current == current
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Slice 3B-7 (ADR 0004 Amendment 7): shared lifecycle-projection cursor,
# the coordinated-publisher bundle/factory, and checkpoint-publication
# error translation.
# ---------------------------------------------------------------------------


def test_checkpoint_publication_failure_map_is_exhaustive():
    """Every current `LifecycleStoreFailure` member must have an
    explicit mapping entry — mirrors the container-side exhaustiveness
    test exactly."""
    assert set(ls._CHECKPOINT_PUBLICATION_FAILURE_MAP.keys()) == set(ls.LifecycleStoreFailure)


def test_checkpoint_publisher_translates_lifecycle_store_error(tmp_path, monkeypatch):
    """Amendment 7's own behavioral change: `LifecycleCheckpointRefPublisher.
    publish()` no longer lets a raw `LifecycleStoreError` escape — it
    translates to `checkpoint_session.CheckpointPublicationError`,
    chained, mirroring the container-side inversion pattern exactly."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    publisher = ls.LifecycleCheckpointRefPublisher(writer, current)
    lease.close()  # forces WRONG_LOCK_SCOPE on the next publish

    with pytest.raises(cs.CheckpointPublicationError) as excinfo:
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))
    assert excinfo.value.reason is cs.CheckpointPublicationFailure.WRONG_LOCK_SCOPE
    assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
    assert "WRONG_LOCK_SCOPE" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Slice 3C-1 (ADR 0004 Amendment 8): LifecycleOwnerStatePublisher --
# activate()/begin_cleanup()/complete() and their exhaustive failure
# translation, mirroring the container/checkpoint precedents exactly.
# ---------------------------------------------------------------------------


def test_owner_state_publication_failure_map_is_exhaustive():
    """Every current `LifecycleStoreFailure` member must have an
    explicit mapping entry — mirrors the container/checkpoint-side
    exhaustiveness tests exactly."""
    assert set(ls._OWNER_STATE_PUBLICATION_FAILURE_MAP.keys()) == set(ls.LifecycleStoreFailure)


def test_owner_state_publication_failure_map_only_classifies_prepare_lifecycle_only_reasons_as_unclassified():
    """`UNCLASSIFIED` is reserved exclusively for
    `LIFECYCLE_ID_COLLISION`/`RECONCILIATION_BLOCKED` (both
    `prepare_lifecycle()`-only, confirmed unreachable from
    `advance_lifecycle_state()`) — every other member gets its own
    precise, non-catch-all reason."""
    unclassified_keys = {
        reason
        for reason, mapped in ls._OWNER_STATE_PUBLICATION_FAILURE_MAP.items()
        if mapped is lo.OwnerStatePublicationFailure.UNCLASSIFIED
    }
    assert unclassified_keys == {
        ls.LifecycleStoreFailure.LIFECYCLE_ID_COLLISION,
        ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED,
    }


def test_owner_state_publisher_activate_begin_cleanup_complete_happy_path(tmp_path, monkeypatch):
    """All three semantic transitions succeed in the accepted owner
    state graph and thread the returned projection forward through the
    shared cursor — `RunController` never needs to see `LifecycleState`
    itself."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)

        publisher.activate()
        assert publisher.current.state is ls.LifecycleState.ACTIVE

        publisher.begin_cleanup()
        assert publisher.current.state is ls.LifecycleState.CLEANING

        publisher.complete()
        assert publisher.current.state is ls.LifecycleState.COMPLETE
    finally:
        lease.close()


def test_owner_state_publisher_does_not_hide_stale_expectation(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)
        # A second, independent writer publishes behind the adapter's back.
        real_current = writer.record_checkpoint_ref_transition(
            expected=current, transition=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
        )
        assert real_current != current

        with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
            publisher.activate()
        assert excinfo.value.reason is lo.OwnerStatePublicationFailure.STALE_EXPECTATION
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
        assert publisher.current == current
    finally:
        lease.close()


def test_owner_state_publisher_pre_installation_failure_leaves_expected_unchanged(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)

        def _boom(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _boom)
            with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
                publisher.activate()
            assert excinfo.value.reason is lo.OwnerStatePublicationFailure.NOT_INSTALLED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
        # No write attempt landed: the cursor's belief and the installed
        # projection both remain exactly the pre-call PREPARING value.
        assert publisher.current == current
    finally:
        lease.close()


def test_owner_state_publisher_activate_durability_unconfirmed_installed_vs_cursor_split(tmp_path, monkeypatch):
    """`PROJECTION_DURABILITY_UNCONFIRMED` means `os.replace` already
    installed the new (ACTIVE) projection on disk; only the trailing
    directory-fsync confirmation failed. The adapter's own `current`
    belief must NOT advance (it stays PREPARING, per the cursor's
    "update only after a confirmed successful write" rule), but an
    explicit `refresh()` proves the installed value really is ACTIVE --
    the installed-on-disk state and the cursor's belief genuinely
    diverge until refresh() is called. `RunController` must never call
    refresh() automatically; this test proves only that the adapter
    itself supports the divergence and its explicit recovery. This is
    the `activate()` phase only -- see the sibling `begin_cleanup()`/
    `complete()` tests immediately below for the other two phases; no
    single test here proves all three."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
                publisher.activate()
            assert excinfo.value.reason is lo.OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # No automatic refresh or retry: the cursor never silently
        # treats this as success.
        assert publisher.current == current
        assert publisher.current.state is ls.LifecycleState.PREPARING

        # But the installed-on-disk projection genuinely is ACTIVE --
        # only an explicit, never-automatic refresh() reveals this.
        refreshed = publisher.refresh()
        assert refreshed.state is ls.LifecycleState.ACTIVE
        assert publisher.current is refreshed
    finally:
        lease.close()


def test_owner_state_publisher_begin_cleanup_durability_unconfirmed_installed_vs_cursor_split(
    tmp_path, monkeypatch
):
    """Same divergence as the `activate()` test above, for the
    `begin_cleanup()` phase: the cursor stays at ACTIVE (the pre-call
    state) while the installed-on-disk projection genuinely advances to
    CLEANING. `activate()` is called for real, unfaulted, first, so the
    starting point is genuinely ACTIVE rather than assumed."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)
        publisher.activate()
        assert publisher.current.state is ls.LifecycleState.ACTIVE
        active_projection = publisher.current

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
                publisher.begin_cleanup()
            assert excinfo.value.reason is lo.OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # No automatic refresh or retry.
        assert publisher.current == active_projection
        assert publisher.current.state is ls.LifecycleState.ACTIVE

        refreshed = publisher.refresh()
        assert refreshed.state is ls.LifecycleState.CLEANING
        assert publisher.current is refreshed
    finally:
        lease.close()


def test_owner_state_publisher_complete_durability_unconfirmed_installed_vs_cursor_split(tmp_path, monkeypatch):
    """Same divergence as the two tests above, for the `complete()`
    phase: the cursor stays at CLEANING (the pre-call state) while the
    installed-on-disk projection genuinely reaches COMPLETE.
    `activate()`/`begin_cleanup()` are called for real, unfaulted,
    first. Additionally proves the installed COMPLETE projection is a
    genuinely valid clean-final shape
    (`is_projection_fully_absent_shape()`) -- the same predicate the
    writer's own `CLEANING -> COMPLETE` gate and reconciliation's
    terminal recognition both depend on. The COMPLETE-specific
    reconciliation `SKIPPED_TERMINAL` behavior itself is proven
    separately, without duplicating this real-writer fixture, by
    `tests/unit/test_reconciliation.py::
    test_complete_absent_shape_is_skipped_with_zero_calls`."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)
        publisher.activate()
        publisher.begin_cleanup()
        assert publisher.current.state is ls.LifecycleState.CLEANING
        cleaning_projection = publisher.current

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
                publisher.complete()
            assert excinfo.value.reason is lo.OwnerStatePublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # No automatic refresh or retry.
        assert publisher.current == cleaning_projection
        assert publisher.current.state is ls.LifecycleState.CLEANING

        refreshed = publisher.refresh()
        assert refreshed.state is ls.LifecycleState.COMPLETE
        assert publisher.current is refreshed
        # Genuinely valid clean-final: the installed COMPLETE projection
        # really does satisfy the same absent-shape predicate
        # CLEANING -> COMPLETE's own writer-side gate required to accept
        # it in the first place.
        assert ls.is_projection_fully_absent_shape(refreshed)
    finally:
        lease.close()


def test_owner_state_publisher_wrong_lock_scope_is_translated(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    publisher = ls.LifecycleOwnerStatePublisher(writer, current)

    with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
        publisher.activate()
    assert excinfo.value.reason is lo.OwnerStatePublicationFailure.WRONG_LOCK_SCOPE
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
    assert publisher.current == current


def test_owner_state_publisher_illegal_transition_is_translated(tmp_path, monkeypatch):
    """`COMPLETE` is not a legal edge directly from `ACTIVE` (only
    `CLEANING` is) -- `ILLEGAL_TRANSITION` is translated, never
    propagated unchanged as a raw `LifecycleStoreError`, and the
    original is preserved as `__cause__`."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleOwnerStatePublisher(writer, current)
        publisher.activate()

        with pytest.raises(lo.OwnerStatePublicationError) as excinfo:
            publisher.complete()

        assert excinfo.value.reason is lo.OwnerStatePublicationFailure.ILLEGAL_TRANSITION
        assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        assert "ILLEGAL_TRANSITION" not in str(excinfo.value)
        assert publisher.current.state is ls.LifecycleState.ACTIVE
    finally:
        lease.close()


# --- The staleness bug (documentation regression, not a target to preserve) ---


def test_standalone_adapters_must_never_be_combined_against_one_writer(tmp_path, monkeypatch):
    """Documents exactly why standalone adapters exist for isolated use
    only: two independently constructed adapters (never via
    `create_shared_lifecycle_publishers()`) against the same writer each
    hold their own private, quickly-stale belief about `current`. A
    write through one invalidates the other's next write —
    `LifecycleProjection` equality is whole-object, so *any* field
    changing anywhere breaks it, not just the field that changed. This
    is not a target behavior to preserve; it is why the coordinator
    exists."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        checkpoint_pub = ls.LifecycleCheckpointRefPublisher(writer, current)
        container_pub = ls.LifecycleContainerPublisher(writer, current)
        owner_pub = ls.LifecycleOwnerStatePublisher(writer, current)

        # A write through the container publisher moves the real
        # projection; the checkpoint publisher's own `_current` still
        # believes the original `current`.
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)

        with pytest.raises(cs.CheckpointPublicationError) as excinfo:
            checkpoint_pub.publish(
                cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
            )
        assert excinfo.value.reason is cs.CheckpointPublicationFailure.STALE_EXPECTATION

        # The owner-state publisher's own private `_current` is stale
        # too, for the identical reason.
        with pytest.raises(lo.OwnerStatePublicationError) as owner_excinfo:
            owner_pub.activate()
        assert owner_excinfo.value.reason is lo.OwnerStatePublicationFailure.STALE_EXPECTATION
    finally:
        lease.close()


# --- LifecycleProjectionCursor / SharedLifecyclePublishers structure ---


def test_shared_publishers_have_no_shadow_current(tmp_path, monkeypatch):
    """Structural proof: a facade built via `create_shared_lifecycle_publishers()`
    stores nothing but a reference to the shared cursor -- no per-facade
    `_current`/`_writer` shadow state that could silently drift from the
    shared one."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        bundle = ls.create_shared_lifecycle_publishers(writer, current)

        assert set(vars(bundle.checkpoint_ref_publisher).keys()) == {"_cursor"}
        assert set(vars(bundle.container_publisher).keys()) == {"_cursor"}
        assert set(vars(bundle.owner_publisher).keys()) == {"_cursor"}
        assert set(vars(bundle.worktree_publisher).keys()) == {"_cursor"}
        assert (
            bundle.checkpoint_ref_publisher._cursor
            is bundle.container_publisher._cursor
            is bundle.owner_publisher._cursor
            is bundle.worktree_publisher._cursor
            is bundle.cursor
        )
    finally:
        lease.close()


def test_standalone_constructor_still_works_and_is_source_compatible(tmp_path, monkeypatch):
    """Standalone construction (not via the factory) remains exactly as
    it was before this slice -- each instance privately owns its own
    cursor, never shared."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        checkpoint_pub = ls.LifecycleCheckpointRefPublisher(writer, current)
        container_pub = ls.LifecycleContainerPublisher(writer, current)
        owner_pub = ls.LifecycleOwnerStatePublisher(writer, current)
        worktree_pub = ls.LifecycleWorktreePublisher(writer, current)
        assert set(vars(checkpoint_pub).keys()) == {"_cursor"}
        assert set(vars(container_pub).keys()) == {"_cursor"}
        assert set(vars(owner_pub).keys()) == {"_cursor"}
        assert set(vars(worktree_pub).keys()) == {"_cursor"}
        assert checkpoint_pub._cursor is not container_pub._cursor is not owner_pub._cursor
        assert checkpoint_pub._cursor is not owner_pub._cursor
        assert worktree_pub._cursor is not checkpoint_pub._cursor
        assert worktree_pub._cursor is not container_pub._cursor
        assert worktree_pub._cursor is not owner_pub._cursor
        assert checkpoint_pub.current == current
        assert container_pub.current == current
        assert owner_pub.current == current
        assert worktree_pub.current == current
    finally:
        lease.close()


# --- The real, principal interleaving test (real lease/writer; no Docker, no real Git-ref mutation) ---


def test_shared_bundle_realistic_interleaving_no_spurious_staleness(tmp_path, monkeypatch):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        bundle = ls.create_shared_lifecycle_publishers(writer, current)
        cursor = bundle.cursor
        checkpoint_pub = bundle.checkpoint_ref_publisher
        container_pub = bundle.container_publisher

        # PREPARING -> ACTIVE (via the shared cursor directly).
        cursor.advance_state(ls.LifecycleState.ACTIVE)
        assert cursor.current.state is ls.LifecycleState.ACTIVE

        # Baseline container transitions through confirmed ABSENT.
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.PRESENT, id="a" * 64)
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.REMOVING, id="a" * 64)
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.ABSENT, id=None)
        assert cursor.current.baseline.intent is ls.ContainerIntent.ABSENT

        # Checkpoint ABSENT -> CREATING -> PRESENT.
        sha1 = _sha("1")
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=sha1))
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=sha1))
        assert cursor.current.checkpoint_ref.accepted_sha == sha1

        # Verification container transitions through confirmed ABSENT.
        container_pub.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.CREATING, id=None)
        container_pub.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.PRESENT, id="b" * 64)
        container_pub.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.REMOVING, id="b" * 64)
        container_pub.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.ABSENT, id=None)
        assert cursor.current.verification.intent is ls.ContainerIntent.ABSENT

        # Checkpoint advance, then removal.
        sha2 = _sha("2")
        checkpoint_pub.publish(
            cs.CheckpointTransition(
                intent=cs.CheckpointIntent.ADVANCING, accepted_sha=sha1, expected_old_sha=sha1, proposed_new_sha=sha2
            )
        )
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=sha2))
        checkpoint_pub.publish(
            cs.CheckpointTransition(
                intent=cs.CheckpointIntent.REMOVING, accepted_sha=sha2, expected_old_sha=sha2
            )
        )
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.ABSENT))
        assert cursor.current.checkpoint_ref == cs.ABSENT_TRANSITION

        # Fault-inject a durability-unconfirmed failure on the next
        # container write, then prove explicit refresh() re-syncs BOTH
        # facades sharing this cursor at once.
        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(cl.ContainerPublicationError) as excinfo:
                container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
            assert excinfo.value.reason is cl.ContainerPublicationFailure.DURABILITY_UNCONFIRMED

        # Not silently treated as success — cursor's own belief unchanged.
        assert cursor.current.baseline.intent is ls.ContainerIntent.ABSENT

        cursor.refresh()
        assert cursor.current.baseline.intent is ls.ContainerIntent.CREATING

        # The next write through the SAME facade now succeeds — no
        # spurious staleness, proving the shared refresh benefit.
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.ABSENT, id=None)
        assert cursor.current.baseline.intent is ls.ContainerIntent.ABSENT

        # Direct proof of the shared benefit in the OTHER direction too:
        # fault-inject a durability-unconfirmed failure on a
        # checkpoint-ref write, refresh, then prove the checkpoint
        # facade's own next write succeeds — the acceptance criterion is
        # "either facade," not merely the one already exercised above.
        sha3 = _sha("3")
        call_count["n"] = 0
        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(cs.CheckpointPublicationError) as excinfo:
                checkpoint_pub.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=sha3)
                )
            assert excinfo.value.reason is cs.CheckpointPublicationFailure.DURABILITY_UNCONFIRMED

        assert cursor.current.checkpoint_ref == cs.ABSENT_TRANSITION  # unchanged, not silently advanced

        cursor.refresh()
        assert cursor.current.checkpoint_ref.intent is cs.CheckpointIntent.CREATING

        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=sha3))
        assert cursor.current.checkpoint_ref.accepted_sha == sha3

        # Genuine external staleness is still refused: a second,
        # independent writer publishes behind the bundle's back.
        stale_expected = cursor.current
        writer.advance_lifecycle_state(expected=stale_expected, state=ls.LifecycleState.CLEANING)
        with pytest.raises(cl.ContainerPublicationError) as excinfo:
            container_pub.publish(role=ls.ContainerRole.VERIFICATION, intent=ls.ContainerIntent.CREATING, id=None)
        assert excinfo.value.reason is cl.ContainerPublicationFailure.STALE_EXPECTATION
    finally:
        lease.close()


def test_shared_bundle_owner_publisher_reaches_preparing_active_cleaning_complete(tmp_path, monkeypatch):
    """Slice 3C-1's own real-writer, real-lease, no-Docker proof: the
    owner-state publisher genuinely drives
    `PREPARING -> ACTIVE -> CLEANING -> COMPLETE` through the shared
    bundle once every other owned resource (both containers, the
    checkpoint ref) has independently reached its own absent shape --
    exactly what `is_projection_fully_absent_shape()`'s `CLEANING ->
    COMPLETE` clean-final gate requires. No composition API is
    introduced here -- this exercises the existing
    `create_shared_lifecycle_publishers()` factory directly, the same
    one `RunController`'s own (unbuilt) future composition layer would
    use."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        bundle = ls.create_shared_lifecycle_publishers(writer, current)
        cursor = bundle.cursor
        owner_pub = bundle.owner_publisher

        assert cursor.current.state is ls.LifecycleState.PREPARING

        owner_pub.activate()
        assert cursor.current.state is ls.LifecycleState.ACTIVE

        owner_pub.begin_cleanup()
        assert cursor.current.state is ls.LifecycleState.CLEANING

        # The initial projection's containers/worktree/checkpoint-ref
        # are already at their absent shape (nothing else in this test
        # ever touched them), so the clean-final gate is satisfied.
        owner_pub.complete()
        assert cursor.current.state is ls.LifecycleState.COMPLETE
    finally:
        lease.close()


# ---------------------------------------------------------------------------
# Worktree-attribution substrate slice (ADR 0004 Amendment 10):
# record_worktree_transition(), LifecycleWorktreePublisher, and shared-
# cursor integration. Mirrors the checkpoint-ref writer/publisher tests
# above exactly, since both use the identical single-value-object
# publish(transition) shape.
# ---------------------------------------------------------------------------


def _wt(intent: wl.WorktreeIntent, expected_head: str | None = None) -> wl.WorktreeTransition:
    return wl.WorktreeTransition(intent=intent, expected_head=expected_head)


# --- Correction pass: WorktreeAttribution compatibility alias ---


def test_worktree_attribution_is_exact_identity_alias_for_worktree_transition():
    """`lifecycle_store.WorktreeAttribution` was a public,
    non-underscored class before this slice; restored as an exact
    identity alias (never a second dataclass or a wrapper) for source
    compatibility with any external caller a repository-wide grep
    cannot rule out."""
    assert ls.WorktreeAttribution is wl.WorktreeTransition


def test_worktree_attribution_construction_is_now_deliberately_stricter():
    """The retired dataclass had no `__post_init__`, so
    `WorktreeAttribution(intent=PRESENT, expected_head=None)` was
    previously constructible. The alias now enforces the real
    combination rule -- the same call raises `ValueError`."""
    with pytest.raises(ValueError):
        ls.WorktreeAttribution(intent=wl.WorktreeIntent.PRESENT, expected_head=None)
    # The absent shape, and a well-formed non-absent shape, still work.
    ls.WorktreeAttribution(intent=wl.WorktreeIntent.ABSENT)
    ls.WorktreeAttribution(intent=wl.WorktreeIntent.PRESENT, expected_head=_sha("1"))


def test_worktree_full_happy_path_round_trip(tmp_path, monkeypatch):
    """Every legal owner edge in sequence: absent -> creating ->
    present -> disposing -> absent, with the materialization OID
    retained unchanged through creating/present/disposing and cleared
    only at disposing->absent."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        origin = _sha("1")
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, origin)
        )
        assert current.worktree == _wt(wl.WorktreeIntent.CREATING, origin)

        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.PRESENT, origin)
        )
        assert current.worktree == _wt(wl.WorktreeIntent.PRESENT, origin)

        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.DISPOSING, origin)
        )
        assert current.worktree == _wt(wl.WorktreeIntent.DISPOSING, origin)

        current = writer.record_worktree_transition(
            expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION
        )
        assert current.worktree == wl.ABSENT_WORKTREE_TRANSITION
    finally:
        lease.close()


def test_worktree_creating_to_absent_recovery_edge(tmp_path, monkeypatch):
    """The caller-confirmed recovery edge: creating -> absent, legal
    purely as a graph edge (this writer performs no Git/filesystem
    observation of its own -- the caller's own confirmation discipline
    is outside this writer's scope, per the writer method's docstring)."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha("1"))
        )
        current = writer.record_worktree_transition(
            expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION
        )
        assert current.worktree == wl.ABSENT_WORKTREE_TRANSITION
    finally:
        lease.close()


@pytest.mark.parametrize(
    "from_transition,to_transition",
    [
        # From absent: only creating is legal.
        (wl.ABSENT_WORKTREE_TRANSITION, _wt(wl.WorktreeIntent.PRESENT, _sha("1"))),
        (wl.ABSENT_WORKTREE_TRANSITION, _wt(wl.WorktreeIntent.DISPOSING, _sha("1"))),
        # From creating: present or absent only -- never disposing directly.
        (_wt(wl.WorktreeIntent.CREATING, _sha("1")), _wt(wl.WorktreeIntent.DISPOSING, _sha("1"))),
        # From present: disposing only -- never back to creating, never
        # straight to absent.
        (_wt(wl.WorktreeIntent.PRESENT, _sha("1")), _wt(wl.WorktreeIntent.CREATING, _sha("1"))),
        (_wt(wl.WorktreeIntent.PRESENT, _sha("1")), wl.ABSENT_WORKTREE_TRANSITION),
        (_wt(wl.WorktreeIntent.PRESENT, _sha("1")), _wt(wl.WorktreeIntent.PRESENT, _sha("2"))),
        # From disposing: absent only -- never back to present/creating.
        (_wt(wl.WorktreeIntent.DISPOSING, _sha("1")), _wt(wl.WorktreeIntent.PRESENT, _sha("1"))),
        (_wt(wl.WorktreeIntent.DISPOSING, _sha("1")), _wt(wl.WorktreeIntent.CREATING, _sha("1"))),
    ],
)
def test_worktree_illegal_edges(tmp_path, monkeypatch, from_transition, to_transition):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        if from_transition.intent is not wl.WorktreeIntent.ABSENT:
            # Reach the `from_transition` state via whatever legal path
            # gets there, so the edge under test is isolated from setup.
            if from_transition.intent is wl.WorktreeIntent.CREATING:
                current = writer.record_worktree_transition(expected=current, transition=from_transition)
            elif from_transition.intent is wl.WorktreeIntent.PRESENT:
                current = writer.record_worktree_transition(
                    expected=current,
                    transition=_wt(wl.WorktreeIntent.CREATING, from_transition.expected_head),
                )
                current = writer.record_worktree_transition(expected=current, transition=from_transition)
            elif from_transition.intent is wl.WorktreeIntent.DISPOSING:
                current = writer.record_worktree_transition(
                    expected=current,
                    transition=_wt(wl.WorktreeIntent.CREATING, from_transition.expected_head),
                )
                current = writer.record_worktree_transition(
                    expected=current,
                    transition=_wt(wl.WorktreeIntent.PRESENT, from_transition.expected_head),
                )
                current = writer.record_worktree_transition(expected=current, transition=from_transition)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(expected=current, transition=to_transition)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_worktree_creating_to_present_oid_change_refused(tmp_path, monkeypatch):
    """Immutable-OID continuity: creating->present must retain the
    exact same materialization commit -- a different OID is refused
    even though (creating, present) is otherwise a legal edge pair."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha("1"))
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(
                expected=current, transition=_wt(wl.WorktreeIntent.PRESENT, _sha("2"))
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_worktree_present_to_disposing_oid_change_refused(tmp_path, monkeypatch):
    """Immutable-OID continuity: present->disposing must also retain
    the exact same materialization commit."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        origin = _sha("1")
        current = writer.record_worktree_transition(expected=current, transition=_wt(wl.WorktreeIntent.CREATING, origin))
        current = writer.record_worktree_transition(expected=current, transition=_wt(wl.WorktreeIntent.PRESENT, origin))
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(
                expected=current, transition=_wt(wl.WorktreeIntent.DISPOSING, _sha("2"))
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_worktree_exact_tuple_no_op_zero_publication_io(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for a no-op")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            result = writer.record_worktree_transition(expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION)
        assert result == current
    finally:
        lease.close()


def test_worktree_wrong_object_format_length_is_illegal(tmp_path, monkeypatch):
    """The writer re-validates `expected_head` against the repository's
    *actual* object format, even though `WorktreeTransition.__post_init__`
    already accepted the generic 40-or-64 shape -- a sha256-length OID
    offered to a sha1 repository must be refused here too."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(
                expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha256("1"))
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def _sha256(seed: str) -> str:
    return (seed * 64)[:64]


@pytest.mark.parametrize(
    "reconciler_state",
    [ls.LifecycleState.RECONCILING, ls.LifecycleState.RECONCILED, ls.LifecycleState.RECONCILIATION_FAILED],
)
def test_worktree_transition_refused_from_reconciler_owned_states(tmp_path, monkeypatch, reconciler_state):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, initial = lease.open_projection_writer()
        reconciler_owned = dataclasses.replace(initial, state=reconciler_state)
        _publish_raw(lease, reconciler_owned)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(
                expected=reconciler_owned, transition=_wt(wl.WorktreeIntent.CREATING, _sha("1"))
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        # Even an exact no-op is refused.
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.record_worktree_transition(expected=reconciler_owned, transition=wl.ABSENT_WORKTREE_TRANSITION)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_worktree_and_checkpoint_ref_transitions_still_legal_in_active_and_cleaning(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha("1"))
        )
        current = writer.record_worktree_transition(expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION)

        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha("2"))
        )
        current = writer.record_worktree_transition(expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION)
        assert current.state == ls.LifecycleState.CLEANING
        assert current.worktree == wl.ABSENT_WORKTREE_TRANSITION
    finally:
        lease.close()


def test_worktree_transition_invalid_type_on_released_lock_gives_wrong_lock_scope(tmp_path, monkeypatch):
    """Uniform writer-call ordering: a wrong lock scope is detected
    before a wrong-type `transition` is ever inspected."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        writer.record_worktree_transition(expected=current, transition="not-a-transition")
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


def test_worktree_transition_none_is_illegal_not_raw_error(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for an invalid transition")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.record_worktree_transition(expected=current, transition=None)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


def test_worktree_transition_wrong_type_is_illegal_not_raw_error(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()

        def _boom(*a, **k):
            raise AssertionError("must not publish for an invalid transition")

        with monkeypatch.context() as scoped:
            scoped.setattr(ls, "publish_private_file_atomically_at", _boom)
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                writer.record_worktree_transition(expected=current, transition={"intent": "absent"})
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


# --- LifecycleWorktreePublisher adapter ---


def test_worktree_publisher_adapter_success_threads_returned_projection_forward(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)

        publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))

        assert publisher.current.worktree.intent is wl.WorktreeIntent.CREATING
        assert publisher.current != current
    finally:
        lease.close()


def test_worktree_publisher_adapter_does_not_hide_stale_expectation(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)
        real_current = writer.record_container_transition(
            expected=current, role="baseline", intent=ls.ContainerIntent.CREATING, id=None
        )
        assert real_current != current

        with pytest.raises(wl.WorktreePublicationError) as excinfo:
            publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
        assert excinfo.value.reason is wl.WorktreePublicationFailure.STALE_EXPECTATION
        assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
        assert publisher.current == current
    finally:
        lease.close()


def test_worktree_publisher_adapter_durability_unconfirmed_installed_vs_cursor_split_and_refresh(
    tmp_path, monkeypatch
):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)

        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(wl.WorktreePublicationError) as excinfo:
                publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
            assert excinfo.value.reason is wl.WorktreePublicationFailure.DURABILITY_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

        # Never silently treated as success: the adapter's own belief
        # (the "cursor" side of the split) is unchanged even though the
        # write was actually installed (the "installed" side).
        assert publisher.current == current

        # Explicit, never-automatic refresh() recovers.
        refreshed = publisher.refresh()
        assert refreshed.worktree.intent is wl.WorktreeIntent.CREATING
        assert publisher.current is refreshed

        # No automatic retry happened above -- only this explicit call
        # to publish() again, now with the correct expectation, succeeds.
        publisher.publish(_wt(wl.WorktreeIntent.PRESENT, _sha("1")))
        assert publisher.current.worktree.intent is wl.WorktreeIntent.PRESENT
    finally:
        lease.close()


def test_worktree_publisher_adapter_cleanup_unconfirmed_remains_distinct_and_propagates(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)

        def _fail_write(fd, data):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced write failure")

        def _fail_unlink(path, *, dir_fd):
            raise OSError("forced temp-cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "write_all_eintr_safe", _fail_write)
            scoped.setattr(lf.os, "unlink", _fail_unlink)
            with pytest.raises(wl.WorktreePublicationError) as excinfo:
                publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
            assert excinfo.value.reason is wl.WorktreePublicationFailure.CLEANUP_UNCONFIRMED
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
        assert publisher.current == current
    finally:
        lease.close()


def test_worktree_publisher_adapter_wrong_lock_scope_propagates_unchanged(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    publisher = ls.LifecycleWorktreePublisher(writer, current)

    with pytest.raises(wl.WorktreePublicationError) as excinfo:
        publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
    assert excinfo.value.reason is wl.WorktreePublicationFailure.WRONG_LOCK_SCOPE


def test_worktree_publication_failure_map_is_exhaustive():
    """Every current `LifecycleStoreFailure` member must have an
    explicit mapping entry — mirrors the container/checkpoint/owner-
    side exhaustiveness tests exactly."""
    assert set(ls._WORKTREE_PUBLICATION_FAILURE_MAP.keys()) == set(ls.LifecycleStoreFailure)


def test_worktree_publication_failure_map_only_classifies_prepare_lifecycle_only_reasons_as_unclassified():
    unclassified_keys = {
        reason
        for reason, mapped in ls._WORKTREE_PUBLICATION_FAILURE_MAP.items()
        if mapped is wl.WorktreePublicationFailure.UNCLASSIFIED
    }
    assert unclassified_keys == {
        ls.LifecycleStoreFailure.LIFECYCLE_ID_COLLISION,
        ls.LifecycleStoreFailure.RECONCILIATION_BLOCKED,
    }


def test_worktree_publisher_translates_lifecycle_store_error(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    publisher = ls.LifecycleWorktreePublisher(writer, current)
    lease.close()  # forces WRONG_LOCK_SCOPE on the next publish

    with pytest.raises(wl.WorktreePublicationError) as excinfo:
        publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
    assert excinfo.value.reason is wl.WorktreePublicationFailure.WRONG_LOCK_SCOPE
    assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
    assert "WRONG_LOCK_SCOPE" not in str(excinfo.value)


def test_worktree_publisher_identity_properties_match_cursor_projection(tmp_path, monkeypatch):
    """`lifecycle_id`/`state_root_id` are read directly from the
    publisher's own cursor, matching `LifecycleContainerPublisher`'s
    identical identity-property contract -- not an independently
    supplied or stale value."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)
        assert publisher.lifecycle_id == current.lifecycle_id == lease.lifecycle_id
        assert publisher.state_root_id == current.state_root_id
        # ADR 0004 Amendment 11: repo_key, from the same cursor projection.
        assert publisher.repo_key == current.repo_key == lease.repo_key

        publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
        # Identity is unaffected by an ordinary resource transition --
        # it is still derived from the (now-advanced) cursor projection.
        assert publisher.lifecycle_id == publisher.current.lifecycle_id == lease.lifecycle_id
        assert publisher.state_root_id == publisher.current.state_root_id
        assert publisher.repo_key == publisher.current.repo_key == lease.repo_key
    finally:
        lease.close()


def test_worktree_publication_error_message_never_leaks_cause_detail_or_enum_spelling(tmp_path, monkeypatch):
    """The fixed, sanitized production message must never contain the
    injected `LifecycleStoreError`'s own detail text, nor the
    categorical enum member's own spelling (upper- or lower-case) --
    only `__cause__`, inspected explicitly, carries that information."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    publisher = ls.LifecycleWorktreePublisher(writer, current)
    lease.close()  # forces WRONG_LOCK_SCOPE, whose own LifecycleStoreError
    # message text is "a projection write requires an already-held
    # lifecycle lock on a fully identified lease" -- none of that, and
    # no "wrong_lock_scope"/"WRONG_LOCK_SCOPE" spelling, may appear in
    # the translated public message.
    with pytest.raises(wl.WorktreePublicationError) as excinfo:
        publisher.publish(_wt(wl.WorktreeIntent.CREATING, _sha("1")))
    message = str(excinfo.value)
    assert message == "worktree transition could not be published"
    assert "lifecycle lock" not in message
    assert "wrong_lock_scope" not in message.lower()
    assert "WRONG_LOCK_SCOPE" not in message
    assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
    assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE


def test_worktree_publisher_wrong_type_transition_translated_without_raw_leakage(tmp_path, monkeypatch):
    """A wrong-type `transition` passed through the publisher is
    translated to `WorktreePublicationError`/`ILLEGAL_TRANSITION` --
    never a raw `AttributeError`/`TypeError` escaping from an unguarded
    attribute access on a non-`WorktreeTransition` value."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleWorktreePublisher(writer, current)

        for bad_transition in (None, "not-a-transition", {"intent": "absent"}, 123, object()):
            with pytest.raises(wl.WorktreePublicationError) as excinfo:
                publisher.publish(bad_transition)  # type: ignore[arg-type]
            assert excinfo.value.reason is wl.WorktreePublicationFailure.ILLEGAL_TRANSITION
            assert isinstance(excinfo.value.__cause__, ls.LifecycleStoreError)
            assert excinfo.value.__cause__.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        lease.close()


# --- Unchanged clean-final / reconciliation-eligibility behavior for absent worktrees ---


def test_is_projection_fully_absent_shape_still_true_for_absent_worktree():
    projection = ls.build_initial_preparing_projection(
        lifecycle_id="a" * 32,
        state_root_id="b" * 32,
        repo_key="c" * 32,
        run_id="r",
        source_repo_path="/tmp/x",
    )
    assert ls.is_projection_fully_absent_shape(projection)
    assert ls.is_projection_reconciliation_eligible_shape(projection)


def test_is_projection_fully_absent_shape_false_for_non_absent_worktree():
    """New behavior this slice introduces: a non-absent worktree shape
    now genuinely exists and is correctly recognized as NOT fully
    absent / NOT reconciliation-eligible -- both predicates' own
    worktree check, previously unreachable (no non-absent shape could
    ever be loaded), is now exercised for real."""
    projection = ls.build_initial_preparing_projection(
        lifecycle_id="a" * 32,
        state_root_id="b" * 32,
        repo_key="c" * 32,
        run_id="r",
        source_repo_path="/tmp/x",
    )
    dirty = dataclasses.replace(
        projection, worktree=_wt(wl.WorktreeIntent.PRESENT, _sha("1"))
    )
    assert not ls.is_projection_fully_absent_shape(dirty)
    assert not ls.is_projection_reconciliation_eligible_shape(dirty)


def test_cleaning_to_complete_requires_absent_worktree(tmp_path, monkeypatch):
    """The CLEANING->COMPLETE clean-final guard genuinely refuses a
    durably-installed non-absent worktree shape via the normal writer
    path (not only via `_publish_raw` bypass, as the schema-interception
    test above exercises) -- reached here by establishing a real
    `creating` worktree transition through the writer itself."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.record_worktree_transition(
            expected=current, transition=_wt(wl.WorktreeIntent.CREATING, _sha("1"))
        )
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION

        # Disposing it back to absent lets COMPLETE succeed.
        current = writer.record_worktree_transition(expected=current, transition=wl.ABSENT_WORKTREE_TRANSITION)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.COMPLETE)
        assert current.state is ls.LifecycleState.COMPLETE
    finally:
        lease.close()


# --- Shared-cursor interleaving with checkpoint, container, and owner publishers ---


def test_shared_bundle_worktree_interleaving_no_spurious_staleness(tmp_path, monkeypatch):
    from codeagent import container_lifecycle as cl

    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        bundle = ls.create_shared_lifecycle_publishers(writer, current)
        cursor = bundle.cursor
        checkpoint_pub = bundle.checkpoint_ref_publisher
        container_pub = bundle.container_publisher
        owner_pub = bundle.owner_publisher
        worktree_pub = bundle.worktree_publisher

        owner_pub.activate()
        assert cursor.current.state is ls.LifecycleState.ACTIVE

        # Worktree creating -> present, interleaved with a container
        # transition and a checkpoint-ref transition through the SAME
        # shared cursor -- proving a write through any one facade keeps
        # every other facade's own next write correctly synchronized,
        # the identical property Slice 3B-7 established for the first
        # three facades.
        origin = _sha("1")
        worktree_pub.publish(_wt(wl.WorktreeIntent.CREATING, origin))
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.CREATING, id=None)
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=origin))
        worktree_pub.publish(_wt(wl.WorktreeIntent.PRESENT, origin))
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.PRESENT, id="a" * 64)
        checkpoint_pub.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.PRESENT, accepted_sha=origin))

        assert cursor.current.worktree == _wt(wl.WorktreeIntent.PRESENT, origin)
        assert cursor.current.baseline.intent is ls.ContainerIntent.PRESENT
        assert cursor.current.checkpoint_ref.accepted_sha == origin

        # Fault-inject a durability-unconfirmed failure on the worktree
        # write, then prove explicit refresh() re-syncs every facade
        # sharing this cursor, including the three NOT directly involved
        # in the failed call.
        real_fsync_fd = lf.fsync_fd
        call_count = {"n": 0}

        def _fail_last_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced directory fsync failure")
            return real_fsync_fd(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "fsync_fd", _fail_last_fsync)
            with pytest.raises(wl.WorktreePublicationError) as excinfo:
                worktree_pub.publish(_wt(wl.WorktreeIntent.DISPOSING, origin))
            assert excinfo.value.reason is wl.WorktreePublicationFailure.DURABILITY_UNCONFIRMED

        assert cursor.current.worktree == _wt(wl.WorktreeIntent.PRESENT, origin)
        cursor.refresh()
        assert cursor.current.worktree == _wt(wl.WorktreeIntent.DISPOSING, origin)

        # The next write through a DIFFERENT facade (container) now
        # succeeds immediately -- no spurious staleness introduced by
        # the worktree facade's own prior fault.
        container_pub.publish(role=ls.ContainerRole.BASELINE, intent=ls.ContainerIntent.REMOVING, id="a" * 64)
        assert cursor.current.baseline.intent is ls.ContainerIntent.REMOVING

        # And the worktree facade's own next write succeeds too.
        worktree_pub.publish(wl.ABSENT_WORKTREE_TRANSITION)
        assert cursor.current.worktree == wl.ABSENT_WORKTREE_TRANSITION
    finally:
        lease.close()


# --- Static proof: no production workspace/controller/reconciliation integration ---


def test_no_controller_or_reconciliation_integration_and_workspace_uses_only_the_leaf_module():
    """Static source-level scope proof. Updated by ADR 0004 Amendment 11:
    `workspace.py` now deliberately imports the dependency-light
    `worktree_lifecycle` module (the optional, unwired producer seam), so
    the Amendment 10 version of this test -- which forbade that import --
    is narrowed rather than kept. What still holds, and is asserted:
    `controller.py`/`reconciliation.py` have no worktree-publication
    integration at all, and `workspace.py` never imports `lifecycle_store`
    nor references the concrete adapter or writer method."""
    for module_name in ("controller", "reconciliation"):
        source = Path(f"src/codeagent/{module_name}.py").read_text()
        assert "worktree_lifecycle" not in source, f"{module_name}.py must not import worktree_lifecycle"
        assert "LifecycleWorktreePublisher" not in source
        assert "record_worktree_transition" not in source
    workspace_source = Path("src/codeagent/workspace.py").read_text()
    assert "lifecycle_store" not in workspace_source
    assert "LifecycleWorktreePublisher" not in workspace_source
    assert "record_worktree_transition" not in workspace_source


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 12: the narrow `creating`-worktree reconciliation shape
# and the reconciler-only `creating -> absent` worktree writer.
# ---------------------------------------------------------------------------

_ORIGIN = "1" * 40


def _creating(projection, head=_ORIGIN):
    return dataclasses.replace(
        projection, worktree=wl.WorktreeTransition(intent=wl.WorktreeIntent.CREATING, expected_head=head)
    )


def test_creating_worktree_shape_truth_table():
    base = _base_projection()
    assert ls.is_projection_creating_worktree_reconciliation_shape(_creating(base))
    # The existing absent-worktree predicate is unchanged and disjoint.
    assert not ls.is_projection_reconciliation_eligible_shape(_creating(base))
    assert not ls.is_projection_creating_worktree_reconciliation_shape(base)

    for intent in (wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.DISPOSING):
        other = dataclasses.replace(base, worktree=wl.WorktreeTransition(intent=intent, expected_head=_ORIGIN))
        assert not ls.is_projection_creating_worktree_reconciliation_shape(other)
        assert not ls.is_projection_reconciliation_eligible_shape(other)

    with_container = _with_role(_creating(base), role="baseline", intent=ls.ContainerIntent.CREATING, id=None)
    assert not ls.is_projection_creating_worktree_reconciliation_shape(with_container)
    with_verification = _with_role(
        _creating(base), role="verification", intent=ls.ContainerIntent.PRESENT, id="9" * 64
    )
    assert not ls.is_projection_creating_worktree_reconciliation_shape(with_verification)
    with_ref = dataclasses.replace(
        _creating(base),
        checkpoint_ref=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_ORIGIN),
    )
    assert not ls.is_projection_creating_worktree_reconciliation_shape(with_ref)
    with_failure = dataclasses.replace(_creating(base), failure=ls.FailureDetail(phase="p", detail="d"))
    assert not ls.is_projection_creating_worktree_reconciliation_shape(with_failure)


def test_reconciler_worktree_writer_collapses_and_carries_every_other_field(tmp_path):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        projection = _creating(_base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=3))
        updated = ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=3)
        assert updated.worktree == wl.ABSENT_WORKTREE_TRANSITION
        assert dataclasses.replace(updated, worktree=projection.worktree) == projection
        on_disk = _read_back(run_dir)
        assert on_disk["worktree"] == {"intent": "absent", "expected_head": None}
        assert on_disk["state"] == "RECONCILING"
        assert on_disk["reconciliation"]["attempts_total"] == 3
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "state", [ls.LifecycleState.PREPARING, ls.LifecycleState.ACTIVE, ls.LifecycleState.CLEANING]
)
def test_reconciler_worktree_writer_requires_reconciling(tmp_path, state):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(fd, _creating(_base_projection(state=state)), attempts_total=0)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
        assert not (run_dir / ls.LIFECYCLE_JSON_FILENAME).exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize("attempts_total", [0, 2, 4])
def test_reconciler_worktree_writer_never_changes_attempts(tmp_path, attempts_total):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _creating(_base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=3))
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=attempts_total)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "worktree",
    [
        wl.ABSENT_WORKTREE_TRANSITION,
        wl.WorktreeTransition(intent=wl.WorktreeIntent.PRESENT, expected_head=_ORIGIN),
    ],
)
def test_reconciler_worktree_writer_only_creating_to_absent(tmp_path, worktree):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = dataclasses.replace(
            _base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1), worktree=worktree
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=1)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "fs_reason, store_reason",
    [
        (lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED),
        (
            lf.LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED,
            ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED,
        ),
    ],
)
def test_reconciler_worktree_writer_classifies_publication_failures(tmp_path, monkeypatch, fs_reason, store_reason):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        monkeypatch.setattr(
            ls,
            "publish_private_file_atomically_at",
            lambda *a, **k: (_ for _ in ()).throw(lf.LifecycleFsError(fs_reason, "x")),
        )
        projection = _creating(_base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1))
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=1)
        assert excinfo.value.reason is store_reason
    finally:
        os.close(fd)


def test_live_owner_worktree_edges_unchanged_and_reconciler_table_is_exact():
    assert ls._RECONCILER_WORKTREE_TRANSITION_EDGES == frozenset(
        {
            (wl.WorktreeIntent.CREATING, wl.WorktreeIntent.ABSENT),
            (wl.WorktreeIntent.CREATING, wl.WorktreeIntent.DISPOSING),
            (wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.DISPOSING),
            (wl.WorktreeIntent.DISPOSING, wl.WorktreeIntent.ABSENT),
        }
    )
    t = wl.WorktreeTransition
    absent = wl.ABSENT_WORKTREE_TRANSITION
    creating = t(intent=wl.WorktreeIntent.CREATING, expected_head=_ORIGIN)
    present = t(intent=wl.WorktreeIntent.PRESENT, expected_head=_ORIGIN)
    disposing = t(intent=wl.WorktreeIntent.DISPOSING, expected_head=_ORIGIN)
    legal = {(absent, creating), (creating, present), (creating, absent), (present, disposing), (disposing, absent)}
    states = [absent, creating, present, disposing]
    for current in states:
        for target in states:
            if (current, target) in legal:
                ls._validate_worktree_edge(current, target)
            else:
                with pytest.raises(ls.LifecycleStoreError):
                    ls._validate_worktree_edge(current, target)


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 13: materialized-worktree eligibility and the
# reconciler-only `-> disposing` / `disposing -> absent` edges.
# ---------------------------------------------------------------------------


def _with_worktree(projection, intent, head=_ORIGIN):
    return dataclasses.replace(projection, worktree=wl.WorktreeTransition(intent=intent, expected_head=head))


def test_materialized_worktree_shape_truth_table():
    base = _base_projection()
    for intent in (wl.WorktreeIntent.CREATING, wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.DISPOSING):
        projection = _with_worktree(base, intent)
        assert ls.is_projection_materialized_worktree_reconciliation_shape(projection)
        assert not ls.is_projection_materialized_worktree_reconciliation_shape(
            _with_role(projection, role="baseline", intent=ls.ContainerIntent.PRESENT, id="9" * 64)
        )
        assert not ls.is_projection_materialized_worktree_reconciliation_shape(
            dataclasses.replace(
                projection,
                checkpoint_ref=cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_ORIGIN),
            )
        )
        assert not ls.is_projection_materialized_worktree_reconciliation_shape(
            dataclasses.replace(projection, failure=ls.FailureDetail(phase="p", detail="d"))
        )
    assert not ls.is_projection_materialized_worktree_reconciliation_shape(base)


@pytest.mark.parametrize("source", [wl.WorktreeIntent.CREATING, wl.WorktreeIntent.PRESENT])
def test_reconciler_disposing_write_ahead_keeps_head_and_carries_fields(tmp_path, source):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_worktree(_base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=2), source)
        target = wl.WorktreeTransition(intent=wl.WorktreeIntent.DISPOSING, expected_head=_ORIGIN)
        updated = ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=2, target=target)
        assert updated.worktree == target
        assert dataclasses.replace(updated, worktree=projection.worktree) == projection
        on_disk = _read_back(run_dir)
        assert on_disk["worktree"] == {"intent": "disposing", "expected_head": _ORIGIN}
        assert on_disk["reconciliation"]["attempts_total"] == 2
    finally:
        os.close(fd)


def test_reconciler_disposing_requires_same_head(tmp_path):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_worktree(_base_projection(state=ls.LifecycleState.RECONCILING), wl.WorktreeIntent.PRESENT)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(
                fd,
                projection,
                attempts_total=0,
                target=wl.WorktreeTransition(intent=wl.WorktreeIntent.DISPOSING, expected_head="2" * 40),
            )
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)


def test_reconciler_disposing_resume_is_a_no_op_publishing_nothing(tmp_path, monkeypatch):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        monkeypatch.setattr(
            ls, "publish_private_file_atomically_at", lambda *a, **k: pytest.fail("a no-op must not publish")
        )
        projection = _with_worktree(_base_projection(state=ls.LifecycleState.RECONCILING), wl.WorktreeIntent.DISPOSING)
        same = ls._publish_reconciler_worktree_transition(
            fd,
            projection,
            attempts_total=0,
            target=wl.WorktreeTransition(intent=wl.WorktreeIntent.DISPOSING, expected_head=_ORIGIN),
        )
        assert same is projection
    finally:
        os.close(fd)


def test_reconciler_disposing_to_absent_collapse(tmp_path):
    fd, run_dir = _open_run_dir_fd(tmp_path)
    try:
        projection = _with_worktree(_base_projection(state=ls.LifecycleState.RECONCILING, attempts_total=1), wl.WorktreeIntent.DISPOSING)
        updated = ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=1)
        assert updated.worktree == wl.ABSENT_WORKTREE_TRANSITION
        assert _read_back(run_dir)["worktree"] == {"intent": "absent", "expected_head": None}
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "source, target_intent",
    [
        (wl.WorktreeIntent.PRESENT, wl.WorktreeIntent.ABSENT),  # never skips disposing
        (wl.WorktreeIntent.DISPOSING, wl.WorktreeIntent.PRESENT),
        (wl.WorktreeIntent.ABSENT, wl.WorktreeIntent.DISPOSING),
    ],
)
def test_reconciler_worktree_illegal_amendment_13_edges(tmp_path, source, target_intent):
    fd, _ = _open_run_dir_fd(tmp_path)
    try:
        base = _base_projection(state=ls.LifecycleState.RECONCILING)
        projection = base if source is wl.WorktreeIntent.ABSENT else _with_worktree(base, source)
        target = (
            wl.ABSENT_WORKTREE_TRANSITION
            if target_intent is wl.WorktreeIntent.ABSENT
            else wl.WorktreeTransition(intent=target_intent, expected_head=projection.worktree.expected_head or _ORIGIN)
        )
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            ls._publish_reconciler_worktree_transition(fd, projection, attempts_total=0, target=target)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.ILLEGAL_TRANSITION
    finally:
        os.close(fd)
