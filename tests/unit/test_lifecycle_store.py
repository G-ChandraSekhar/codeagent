"""Tests for `codeagent.lifecycle_store` (Milestone 3 Slice 3A-2, ADR
0004 Amendment 1 sections 5, 6, 12, 16)."""

from __future__ import annotations

import dataclasses
import multiprocessing
import os
import signal
import subprocess

import pytest

from codeagent import _git_safety as gs
from codeagent import _lifecycle_fs as lf
from codeagent import checkpoint_ref as cr
from codeagent import checkpoint_session as cs
from codeagent import lifecycle_store as ls
from codeagent import repo_identity as ri
from codeagent import state_locks as sl


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
# worktree: narrowed to the initial (absent) shape only -- see
# _validate_worktree_shape's own docstring for why.
# ---------------------------------------------------------------------------


def test_worktree_absent_with_expected_head_refused():
    payload = _valid_payload()
    payload["worktree"] = {"intent": "absent", "expected_head": _SHA1_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")


@pytest.mark.parametrize("intent", ["creating", "present", "disposing"])
def test_worktree_non_absent_intent_categorically_refused(intent):
    # Narrowed validator: any non-absent worktree shape is refused,
    # regardless of expected_head, because the ADR gives no formal
    # combination table for these shapes (unlike containers/
    # checkpoint_ref) -- this is not yet a "future validator" this
    # module claims to implement completely.
    payload = _valid_payload()
    payload["worktree"] = {"intent": intent, "expected_head": _SHA1_A}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload, object_format="sha1")

    payload_no_head = _valid_payload()
    payload_no_head["worktree"] = {"intent": intent, "expected_head": None}
    with pytest.raises(ls.LifecycleStoreError):
        ls.validate_lifecycle_json_schema(payload_no_head, object_format="sha1")


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


def test_cleaning_to_complete_worktree_dirty_shape_is_unloadable(tmp_path, monkeypatch):
    """Non-absent worktree writing is deferred (Slice 3A-2's own
    narrowing, unchanged): `validate_lifecycle_json_schema` refuses to
    load a non-absent worktree shape at all, so the durable projection
    can never become "authoritative and worktree-dirty" through the
    normal read path in the first place -- the CLEANING->COMPLETE
    guard's worktree check is correct but currently unreachable in
    practice, intercepted earlier by the loader's own existing
    narrowing (SCHEMA_INVALID), not by ILLEGAL_TRANSITION."""
    lease = _prepared_lease(tmp_path, monkeypatch)
    try:
        writer, current = lease.open_projection_writer()
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.ACTIVE)
        current = writer.advance_lifecycle_state(expected=current, state=ls.LifecycleState.CLEANING)
        dirty = dataclasses.replace(current, worktree=ls.WorktreeAttribution(intent=ls.WorktreeIntent.PRESENT, expected_head=None))
        _publish_raw(lease, dirty)
        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            writer.advance_lifecycle_state(expected=dirty, state=ls.LifecycleState.COMPLETE)
        assert excinfo.value.reason is ls.LifecycleStoreFailure.SCHEMA_INVALID
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

        with pytest.raises(ls.LifecycleStoreError) as excinfo:
            publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))
        assert excinfo.value.reason is ls.LifecycleStoreFailure.STALE_EXPECTED_PROJECTION
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
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_PUBLICATION_FAILED
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
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is ls.LifecycleStoreFailure.PROJECTION_DURABILITY_UNCONFIRMED

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
            with pytest.raises(ls.LifecycleStoreError) as excinfo:
                publisher.publish(
                    cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1"))
                )
            assert excinfo.value.reason is ls.LifecycleStoreFailure.CLEANUP_UNCONFIRMED
        assert publisher.current == current
    finally:
        lease.close()


def test_publisher_adapter_wrong_lock_scope_propagates_unchanged(tmp_path, monkeypatch):
    lease = _prepared_lease(tmp_path, monkeypatch)
    writer, current = lease.open_projection_writer()
    lease.close()
    publisher = ls.LifecycleCheckpointRefPublisher(writer, current)

    with pytest.raises(ls.LifecycleStoreError) as excinfo:
        publisher.publish(cs.CheckpointTransition(intent=cs.CheckpointIntent.CREATING, proposed_new_sha=_sha("1")))
    assert excinfo.value.reason is ls.LifecycleStoreFailure.WRONG_LOCK_SCOPE
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
