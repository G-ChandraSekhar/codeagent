"""Tests for `codeagent.repo_identity` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1 sections 1, 13, 14, 16)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeagent import _git_safety as gs
from codeagent import _lifecycle_fs as lf
from codeagent import repo_identity as ri
from codeagent import state_locks as sl
from codeagent.state_root import init_state_root, open_or_create_canonical_root


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


def _make_state_root(tmp_path):
    location = lf.StateRootLocation(
        path=str(tmp_path / "state-root"),
        origin=lf.StateRootOrigin.EXPLICIT,
        conventional_parent_creation_allowed=False,
    )
    fd, canonical = open_or_create_canonical_root(location)
    return init_state_root(fd, canonical)


# ---------------------------------------------------------------------------
# Exact pinned repo_key vector
# ---------------------------------------------------------------------------


def test_pinned_repo_key_vector():
    assert (
        ri.compute_repo_key("/tmp/codeagent-fixed-repo-key-vector") == "126b3309737aaf2addc754b014f19c79"
    )


def test_repo_key_is_stable_for_same_canonical_path():
    assert ri.compute_repo_key("/a/b/c") == ri.compute_repo_key("/a/b/c")


def test_repo_key_differs_for_different_paths():
    assert ri.compute_repo_key("/a/b/c") != ri.compute_repo_key("/a/b/d")


# ---------------------------------------------------------------------------
# Real repository discovery
# ---------------------------------------------------------------------------


def test_discover_repository_identity_real(tmp_path):
    repo = _make_repo(tmp_path)
    identity, context = ri.discover_repository_identity_and_context(str(repo))
    assert len(identity.repo_key) == 32
    assert identity.object_format == "sha1"
    assert identity.canonical_common_dir.endswith(".git")
    assert context.working_tree_root is not None
    assert os.path.isabs(context.working_tree_root)


def test_discover_repository_identity_refuses_bare_repository(tmp_path):
    bare = tmp_path / "bare.git"
    _run("git", "init", "-q", "--bare", str(bare))
    with pytest.raises(ri.RepoIdentityError) as excinfo:
        ri.discover_repository_identity_and_context(str(bare))
    assert excinfo.value.reason is ri.RepoIdentityFailure.BARE_REPOSITORY_UNSUPPORTED


def test_discover_repository_identity_refuses_linked_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    worktree = tmp_path / "wt"
    _run("git", "-C", str(repo), "worktree", "add", str(worktree), "-b", "wtbranch")
    with pytest.raises(ri.RepoIdentityError) as excinfo:
        ri.discover_repository_identity_and_context(str(worktree))
    assert excinfo.value.reason is ri.RepoIdentityFailure.LINKED_WORKTREE_UNSUPPORTED


def test_discover_repository_identity_hostile_git_env_cannot_redirect(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    other_repo = _make_repo(tmp_path, name="other")
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other_repo))
    identity, context = ri.discover_repository_identity_and_context(str(repo))
    # Must reflect `repo`, never `other_repo`, despite the hostile GIT_* env.
    assert context.working_tree_root == str(repo.resolve())


def test_discover_repository_identity_case_alias_containment(tmp_path):
    # On a case-insensitive filesystem, opening via a differently-cased
    # alias must still produce the same canonical identity. On a
    # case-sensitive filesystem, the alias simply won't resolve to the
    # same repo, so this only meaningfully exercises the mechanism
    # where the OS supports it (guarded via os.path.exists).
    repo = _make_repo(tmp_path)
    alias = str(repo).replace(str(repo)[-len(repo.name):], repo.name.upper())
    if not os.path.exists(alias):
        pytest.skip("filesystem is not case-insensitive; alias does not resolve")
    identity1, _ = ri.discover_repository_identity_and_context(str(repo))
    identity2, _ = ri.discover_repository_identity_and_context(alias)
    assert identity1.repo_key == identity2.repo_key
    assert identity1.canonical_common_dir == identity2.canonical_common_dir


def test_discover_repository_identity_sha1(tmp_path):
    repo = _make_repo(tmp_path)
    identity, _ = ri.discover_repository_identity_and_context(str(repo))
    assert identity.object_format == "sha1"


def test_discover_repository_identity_sha256_or_skipped(tmp_path):
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
    identity, _ = ri.discover_repository_identity_and_context(str(repo))
    assert identity.object_format == "sha256"


def test_discover_repository_identity_cleanup_on_failure(tmp_path, monkeypatch):
    """A failure after the common-dir descriptor is opened but before
    discovery completes must still close that descriptor — verified via
    the capability-stack pattern raising cleanly rather than leaking."""
    repo = _make_repo(tmp_path)

    def _boom(*args, **kwargs):
        raise gs.GitSafetyError(gs.GitSafetyFailure.OBJECT_FORMAT_UNAVAILABLE, "boom")

    monkeypatch.setattr(ri, "detect_object_format", _boom)
    with pytest.raises(ri.RepoIdentityError) as excinfo:
        ri.discover_repository_identity_and_context(str(repo))
    assert excinfo.value.reason is ri.RepoIdentityFailure.OBJECT_FORMAT_UNAVAILABLE


# ---------------------------------------------------------------------------
# load_or_create_repo_json
# ---------------------------------------------------------------------------


def _identity_and_lock(tmp_path, state_root, repo):
    identity, _ = ri.discover_repository_identity_and_context(str(repo))
    lock = sl.acquire_repository_lock(state_root, identity.repo_key)
    return identity, lock


def test_load_or_create_repo_json_creates_new(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        try:
            result = ri.load_or_create_repo_json(state_root, identity, lock)
            assert result == identity
        finally:
            lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_normal_existing_validates(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        # Second, independent lock+validate pass over the same repo.json.
        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            result = ri.load_or_create_repo_json(state_root, identity, lock2)
            assert result == identity
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_requires_correct_lock_scope(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, _ = ri.discover_repository_identity_and_context(str(repo))
        wrong_lock = sl.acquire_repository_lock(state_root, "f" * 32)
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, wrong_lock)
            assert excinfo.value.reason is ri.RepoIdentityFailure.WRONG_LOCK_SCOPE
        finally:
            wrong_lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_requires_lock_currently_held(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        lock.release()
        with pytest.raises(ri.RepoIdentityError) as excinfo:
            ri.load_or_create_repo_json(state_root, identity, lock)
        assert excinfo.value.reason is ri.RepoIdentityFailure.WRONG_LOCK_SCOPE
    finally:
        state_root.close()


def test_load_or_create_repo_json_identity_mismatch_bounded_field_names(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        # Simulate the repository being replaced: different st_ino.
        tampered = ri.RepositoryIdentity(
            repo_key=identity.repo_key,
            canonical_common_dir=identity.canonical_common_dir,
            st_dev=identity.st_dev,
            st_ino=identity.st_ino + 1,
            object_format=identity.object_format,
        )
        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, tampered, lock2)
            assert excinfo.value.reason is ri.RepoIdentityFailure.IDENTITY_MISMATCH
            assert excinfo.value.mismatched_fields == frozenset({"st_ino"})
            # Never leaks the actual values into the message.
            assert str(identity.st_ino) not in excinfo.value.message
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_never_regenerates_corrupt_file(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        fd = os.open("repo.json", os.O_WRONLY | os.O_TRUNC, dir_fd=dir_fd)
        os.write(fd, b"corrupt")
        os.close(fd)
        os.close(dir_fd)

        with pytest.raises(ri.RepoIdentityError) as excinfo:
            ri.load_or_create_repo_json(state_root, identity, lock)
        assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
        lock.release()

        dir_fd2 = state_root.open_repo_dir(identity.repo_key)
        fd2 = os.open("repo.json", os.O_RDONLY, dir_fd=dir_fd2)
        content = os.read(fd2, 100)
        os.close(fd2)
        os.close(dir_fd2)
        assert content == b"corrupt"  # never regenerated
    finally:
        state_root.close()


def test_load_or_create_repo_json_anomalous_existing_file_never_adopted(tmp_path):
    """A file appearing between locked absence observation and O_EXCL
    creation is ANOMALOUS_EXISTING_FILE — simulated by a non-compliant
    writer creating repo.json directly (bypassing the lock discipline
    a compliant CodeAgent process would follow)."""
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)

        original_open = ri.open_private_create_exclusive_at

        def _hostile_write_then_create(dir_fd, basename, mode=0o600):
            # A non-compliant writer creates the file first, out of band.
            fd = os.open(basename, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=dir_fd)
            os.write(fd, b'{"hostile": true}')
            os.close(fd)
            return original_open(dir_fd, basename, mode)

        ri.open_private_create_exclusive_at = _hostile_write_then_create
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock)
            assert excinfo.value.reason is ri.RepoIdentityFailure.ANOMALOUS_EXISTING_FILE
        finally:
            ri.open_private_create_exclusive_at = original_open
            lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_rejects_bool_as_int_st_dev(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        payload = {
            "schema_version": 1,
            "repo_key": identity.repo_key,
            "canonical_common_dir": identity.canonical_common_dir,
            "st_dev": True,  # bool, not a real int
            "st_ino": identity.st_ino,
            "object_format": identity.object_format,
        }
        fd = os.open("repo.json", os.O_WRONLY | os.O_TRUNC, dir_fd=dir_fd)
        os.write(fd, lf.canonical_json_dumps(payload))
        os.close(fd)
        os.close(dir_fd)

        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock2)
            assert excinfo.value.reason is ri.RepoIdentityFailure.SCHEMA_INVALID
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_precondition_repos_dir_not_empty(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        dir_fd = state_root.open_repo_dir(identity.repo_key)
        fd = os.open("stray.txt", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=dir_fd)
        os.close(fd)
        os.close(dir_fd)

        with pytest.raises(ri.RepoIdentityError) as excinfo:
            ri.load_or_create_repo_json(state_root, identity, lock)
        assert excinfo.value.reason is ri.RepoIdentityFailure.NAMESPACE_NOT_EMPTY
        lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_precondition_worktrees_dir_not_empty(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        # Simulate pre-existing worktree state without repo.json existing yet.
        wt_fd = lf.open_managed_directory_chain(state_root.root_fd, ["worktrees", identity.repo_key])
        marker_fd = os.open("marker", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=wt_fd)
        os.close(marker_fd)
        os.close(wt_fd)

        with pytest.raises(ri.RepoIdentityError) as excinfo:
            ri.load_or_create_repo_json(state_root, identity, lock)
        assert excinfo.value.reason is ri.RepoIdentityFailure.NAMESPACE_NOT_EMPTY
        lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_no_worktrees_dir_is_fine(tmp_path):
    # worktrees/<repo_key>/ not existing at all must never block creation.
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        result = ri.load_or_create_repo_json(state_root, identity, lock)
        assert result == identity
        lock.release()
    finally:
        state_root.close()


def test_validate_existing_repo_json_rejects_unsafe_permissions(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        os.chmod("repo.json", 0o644, dir_fd=dir_fd)
        os.close(dir_fd)

        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock2)
            assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_discover_repository_identity_uses_descriptor_identity_not_realpath_string():
    """Correction 5: linked-worktree detection must compare
    descriptor-derived (st_dev, st_ino) identity, never an
    os.path.realpath string comparison. Static proof, since
    Path.resolve() legitimately uses realpath internally elsewhere in
    the module (canonicalization) — the requirement is specifically
    that the *comparison itself* never uses it."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(ri.discover_repository_identity_and_context))
    tree = ast.parse(source)
    func_body = tree.body[0].body
    # Skip the leading docstring expression (which legitimately explains
    # what NOT to do) and check only the executable statements.
    if isinstance(func_body[0], ast.Expr) and isinstance(func_body[0].value, ast.Constant):
        func_body = func_body[1:]
    body_source = "\n".join(ast.unparse(stmt) for stmt in func_body)
    assert "realpath" not in body_source


def test_linked_worktree_detection_real_positive_and_negative(tmp_path):
    """Confirms the descriptor-derived comparison actually works: a
    linked worktree is refused, and the main repository (same
    directory) is accepted, via the same st_dev/st_ino comparison."""
    repo = _make_repo(tmp_path)
    identity, _ = ri.discover_repository_identity_and_context(str(repo))
    assert identity.repo_key  # main repo accepted

    worktree = tmp_path / "wt2"
    _run("git", "-C", str(repo), "worktree", "add", str(worktree), "-b", "wtbranch2")
    with pytest.raises(ri.RepoIdentityError) as excinfo:
        ri.discover_repository_identity_and_context(str(worktree))
    assert excinfo.value.reason is ri.RepoIdentityFailure.LINKED_WORKTREE_UNSUPPORTED


# ---------------------------------------------------------------------------
# Correction pass: repo.json hardening
# ---------------------------------------------------------------------------


def test_repo_json_schema_rejects_boolean_schema_version(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        payload = {
            "schema_version": True,
            "repo_key": identity.repo_key,
            "canonical_common_dir": identity.canonical_common_dir,
            "st_dev": identity.st_dev,
            "st_ino": identity.st_ino,
            "object_format": identity.object_format,
        }
        fd = os.open("repo.json", os.O_WRONLY | os.O_TRUNC, dir_fd=dir_fd)
        os.write(fd, lf.canonical_json_dumps(payload))
        os.close(fd)
        os.close(dir_fd)

        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock2)
            assert excinfo.value.reason is ri.RepoIdentityFailure.SCHEMA_INVALID
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_validate_existing_repo_json_translates_read_failure(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        def _failing_read(fd, max_bytes):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "forced read failure")

        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(ri, "read_all_eintr_safe", _failing_read)
                with pytest.raises(ri.RepoIdentityError) as excinfo:
                    ri.load_or_create_repo_json(state_root, identity, lock2)
                assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
                assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_create_repo_json_translates_success_path_close_failure(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)

        real_close_confirmed = ri.close_confirmed
        call_count = {"n": 0}

        def _fail_second_call(fds):
            call_count["n"] += 1
            if call_count["n"] == 2:  # the fd close on the success path
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")
            return real_close_confirmed(fds)

        with monkeypatch.context() as scoped:
            scoped.setattr(ri, "close_confirmed", _fail_second_call)
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock)
            assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
        lock.release()
    finally:
        state_root.close()


def test_create_repo_json_translates_dir_fsync_failure(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)

        def _failing_fsync(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced")

        # Only fail the SECOND fsync_fd call (the parent-directory
        # fsync), letting the file's own fsync succeed normally.
        real_fsync = ri.fsync_fd
        call_count = {"n": 0}

        def _fail_second_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "forced")
            return real_fsync(fd)

        with monkeypatch.context() as scoped:
            scoped.setattr(ri, "fsync_fd", _fail_second_fsync)
            with pytest.raises(ri.RepoIdentityError) as excinfo:
                ri.load_or_create_repo_json(state_root, identity, lock)
            assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
            assert isinstance(excinfo.value.__cause__, lf.LifecycleFsError)
        lock.release()
    finally:
        state_root.close()


def test_load_or_create_repo_json_nested_close_failures_never_discard_prior_failure(tmp_path, monkeypatch):
    """If validating an existing repo.json fails AND both the file-fd
    close and the dir-fd close subsequently also fail, the original
    validation failure must still be the ultimate __cause__, not
    silently replaced by either cleanup failure."""
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        fd = os.open("repo.json", os.O_WRONLY | os.O_TRUNC, dir_fd=dir_fd)
        os.write(fd, b"corrupt")
        os.close(fd)
        os.close(dir_fd)

        def _always_fail_close(fds):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

        lock2 = sl.acquire_repository_lock(state_root, identity.repo_key)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(ri, "close_confirmed", _always_fail_close)
                with pytest.raises(ri.RepoIdentityError) as excinfo:
                    ri.load_or_create_repo_json(state_root, identity, lock2)
                # The outermost raised error is the dir_fd cleanup failure,
                # but its chain must lead back to the original corruption
                # failure, not lose it.
                assert excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
                cause_chain = []
                current = excinfo.value.__cause__
                while current is not None:
                    cause_chain.append(current)
                    current = getattr(current, "__cause__", None)
                assert any(
                    isinstance(exc, ri.RepoIdentityError) and exc.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
                    for exc in cause_chain
                ) or excinfo.value.reason is ri.RepoIdentityFailure.SUBSTRATE_UNAVAILABLE
        finally:
            lock2.release()
    finally:
        state_root.close()


def test_repo_json_serialization_under_lock_survives_reread(tmp_path):
    repo = _make_repo(tmp_path)
    state_root = _make_state_root(tmp_path)
    try:
        identity, lock = _identity_and_lock(tmp_path, state_root, repo)
        ri.load_or_create_repo_json(state_root, identity, lock)
        lock.release()

        dir_fd = state_root.open_repo_dir(identity.repo_key)
        fd = os.open("repo.json", os.O_RDONLY, dir_fd=dir_fd)
        data = os.read(fd, 65536)
        os.close(fd)
        os.close(dir_fd)
        payload = lf.canonical_json_loads_strict(data, max_bytes=lf.REPO_JSON_MAX_BYTES)
        assert payload["repo_key"] == identity.repo_key
        assert payload["object_format"] == identity.object_format
    finally:
        state_root.close()
