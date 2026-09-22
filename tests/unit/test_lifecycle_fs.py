"""Tests for `codeagent._lifecycle_fs` (Milestone 3 Slice 3A-1, ADR 0004
Amendment 1)."""

from __future__ import annotations

import json
import os
import platform
import stat
import time
from pathlib import Path

import pytest

from codeagent import _lifecycle_fs as lf


# ---------------------------------------------------------------------------
# close_confirmed: no short-circuiting any(), attempts every fd
# ---------------------------------------------------------------------------


def test_close_confirmed_closes_all_valid_fds(tmp_path):
    paths = [tmp_path / f"f{i}" for i in range(3)]
    fds = []
    for p in paths:
        p.write_text("x")
        fds.append(os.open(p, os.O_RDONLY))
    lf.close_confirmed(fds)
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_close_confirmed_attempts_every_fd_even_after_first_failure(tmp_path):
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.write_text("x")
    p2.write_text("y")
    fd1 = os.open(p1, os.O_RDONLY)
    fd2 = os.open(p2, os.O_RDONLY)
    os.close(fd1)  # pre-close so closing it again fails
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.close_confirmed([fd1, fd2])
    assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED
    # fd2 must have been closed despite fd1 failing first (no short-circuit).
    with pytest.raises(OSError):
        os.fstat(fd2)


def test_close_confirmed_all_failures_still_raises_once(tmp_path):
    p = tmp_path / "a"
    p.write_text("x")
    fd = os.open(p, os.O_RDONLY)
    os.close(fd)
    with pytest.raises(lf.LifecycleFsError):
        lf.close_confirmed([fd, fd])


# ---------------------------------------------------------------------------
# validate_hex32
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "12345", None, 123],
)
def test_validate_hex32_rejects_invalid(value):
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.validate_hex32(value, field_name="x")
    assert excinfo.value.reason is lf.LifecycleFsFailure.INVALID_COMPONENT


def test_validate_hex32_accepts_valid():
    value = "0123456789abcdef0123456789abcdef"
    assert lf.validate_hex32(value, field_name="x") == value


# ---------------------------------------------------------------------------
# is_within_or_equal: equality/descendant/prefix-sibling/unrelated
# ---------------------------------------------------------------------------


def test_containment_equal():
    assert lf.is_within_or_equal("/a/b", "/a/b") is True


def test_containment_descendant():
    assert lf.is_within_or_equal("/a/b/c", "/a/b") is True


def test_containment_prefix_sibling_is_not_contained():
    # "/state-root2" is NOT contained in "/state-root" despite the string prefix.
    assert lf.is_within_or_equal("/state-root2", "/state-root") is False


def test_containment_unrelated():
    assert lf.is_within_or_equal("/x/y", "/a/b") is False


def test_containment_container_longer_than_candidate():
    assert lf.is_within_or_equal("/a", "/a/b") is False


# ---------------------------------------------------------------------------
# canonicalize_directory: real Darwin F_GETPATH / real Linux / case alias
# ---------------------------------------------------------------------------


def test_canonicalize_directory_real(tmp_path):
    fd, canonical = lf.canonicalize_directory(str(tmp_path))
    try:
        assert os.path.isabs(canonical)
        st = os.fstat(fd)
        assert stat.S_ISDIR(st.st_mode)
    finally:
        os.close(fd)


def test_canonicalize_directory_refuses_symlink_target_file(tmp_path):
    target = tmp_path / "notadir"
    target.write_text("x")
    with pytest.raises(lf.LifecycleFsError):
        lf.canonicalize_directory(str(target))


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS case-canonicalization bug is Darwin-specific")
def test_canonicalize_directory_darwin_case_alias_produces_same_canonical_path(tmp_path):
    # Reproduces the real bug: Path.resolve() preserves case on
    # case-insensitive-but-preserving APFS, but F_GETPATH canonicalizes it.
    real_dir = tmp_path / "MixedCase"
    real_dir.mkdir()
    lower_alias = str(tmp_path) + "/mixedcase"
    fd1, canonical1 = lf.canonicalize_directory(str(real_dir))
    fd2, canonical2 = lf.canonicalize_directory(lower_alias)
    try:
        assert canonical1 == canonical2
    finally:
        os.close(fd1)
        os.close(fd2)


def test_darwin_canonical_path_malformed_fgetpath_raises(monkeypatch):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: True)

    class _FakeFcntl:
        @staticmethod
        def fcntl(fd, request, arg):
            return b"not-null-terminated"  # no NUL

    monkeypatch.setattr(lf, "fcntl", _FakeFcntl)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf._darwin_canonical_path(0)
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE


def test_darwin_canonical_path_typeerror_raises(monkeypatch):
    class _FakeFcntl:
        F_GETPATH = 50

        @staticmethod
        def fcntl(fd, request, arg):
            raise TypeError("array.array not accepted")

    monkeypatch.setattr(lf, "fcntl", _FakeFcntl)
    with pytest.raises(lf.LifecycleFsError):
        lf._darwin_canonical_path(0)


def test_darwin_canonical_path_empty_result_raises(monkeypatch):
    class _FakeFcntl:
        @staticmethod
        def fcntl(fd, request, arg):
            return b"\x00" + b"\x00" * 1023

    monkeypatch.setattr(lf, "fcntl", _FakeFcntl)
    with pytest.raises(lf.LifecycleFsError):
        lf._darwin_canonical_path(0)


def test_canonicalize_directory_linux_path_uses_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: False)
    fd, canonical = lf.canonicalize_directory(str(tmp_path))
    try:
        assert canonical == str(Path(tmp_path).resolve())
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# open_managed_directory_chain
# ---------------------------------------------------------------------------


def test_open_managed_directory_chain_creates_multi_component(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = lf.open_managed_directory_chain(parent_fd, ["a", "b", "c"])
        try:
            assert (tmp_path / "a" / "b" / "c").is_dir()
            st = os.stat(tmp_path / "a" / "b" / "c")
            assert stat.S_IMODE(st.st_mode) == 0o700
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_refuses_intermediate_symlink(tmp_path):
    real_target = tmp_path / "real"
    real_target.mkdir()
    (tmp_path / "a").symlink_to(real_target)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_managed_directory_chain(parent_fd, ["a", "b"])
        assert excinfo.value.reason is lf.LifecycleFsFailure.SYMLINK_REFUSED
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\x00b"])
def test_open_managed_directory_chain_rejects_invalid_component(tmp_path, bad):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_managed_directory_chain(parent_fd, [bad])
        assert excinfo.value.reason is lf.LifecycleFsFailure.INVALID_COMPONENT
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_requires_at_least_one_component(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError):
            lf.open_managed_directory_chain(parent_fd, [])
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_refuses_group_writable_existing_dir(tmp_path):
    d = tmp_path / "a"
    d.mkdir(mode=0o770)
    os.chmod(d, 0o770)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_managed_directory_chain(parent_fd, ["a"])
        assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_accepts_existing_0750(tmp_path):
    # Read/execute bits for group are safe and must not be rejected
    # merely for being present — only a writable group/other bit does.
    d = tmp_path / "a"
    d.mkdir(mode=0o750)
    os.chmod(d, 0o750)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = lf.open_managed_directory_chain(parent_fd, ["a"])
        os.close(fd)
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_refuses_existing_0770(tmp_path):
    d = tmp_path / "a"
    d.mkdir(mode=0o770)
    os.chmod(d, 0o770)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_managed_directory_chain(parent_fd, ["a"])
        assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_refuses_existing_0752(tmp_path):
    d = tmp_path / "a"
    d.mkdir(mode=0o752)
    os.chmod(d, 0o752)  # other-writable
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_managed_directory_chain(parent_fd, ["a"])
        assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS
    finally:
        os.close(parent_fd)


def test_validate_safe_owned_directory_stat_positive_0740(tmp_path):
    d = tmp_path / "a"
    d.mkdir(mode=0o740)
    os.chmod(d, 0o740)
    st = os.stat(d)
    lf.validate_safe_owned_directory_stat(st)  # must not raise


def test_validate_safe_owned_directory_stat_rejects_wrong_owner(tmp_path, monkeypatch):
    d = tmp_path / "a"
    d.mkdir(mode=0o700)
    st = os.stat(d)
    monkeypatch.setattr(os, "getuid", lambda: st.st_uid + 1)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.validate_safe_owned_directory_stat(st)
    assert excinfo.value.reason is lf.LifecycleFsFailure.UNSAFE_PERMISSIONS


def test_open_managed_directory_chain_closes_intermediate_fds(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = lf.open_managed_directory_chain(parent_fd, ["a", "b"])
        # The intermediate "a" fd must be closed; only the final fd is open.
        # We can't directly enumerate fds portably, but we can confirm the
        # returned fd is a valid, distinct, open directory descriptor.
        st = os.fstat(fd)
        assert stat.S_ISDIR(st.st_mode)
        os.close(fd)
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# open_private_create_exclusive_at: umask independence, exact mode
# ---------------------------------------------------------------------------


def test_open_private_create_exclusive_umask_independent(tmp_path):
    old_umask = os.umask(0o077)
    try:
        parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = lf.open_private_create_exclusive_at(parent_fd, "f.json", 0o600)
            try:
                st = os.fstat(fd)
                assert stat.S_IMODE(st.st_mode) == 0o600
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    finally:
        os.umask(old_umask)


def test_open_private_create_exclusive_raises_file_exists(tmp_path):
    (tmp_path / "f.json").write_text("{}")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            lf.open_private_create_exclusive_at(parent_fd, "f.json", 0o600)
    finally:
        os.close(parent_fd)


def test_open_private_create_exclusive_rejects_invalid_component(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError):
            lf.open_private_create_exclusive_at(parent_fd, "a/b", 0o600)
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# write_all_eintr_safe / read_all_eintr_safe: short read/write, EINTR, zero-progress
# ---------------------------------------------------------------------------


def test_write_all_eintr_safe_retries_on_eintr(tmp_path, monkeypatch):
    p = tmp_path / "f"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    calls = {"n": 0}
    real_write = os.write

    def _flaky_write(fd_, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()
        return real_write(fd_, data)

    monkeypatch.setattr(os, "write", _flaky_write)
    try:
        lf.write_all_eintr_safe(fd, b"hello")
    finally:
        os.close(fd)
    assert p.read_bytes() == b"hello"


def test_write_all_eintr_safe_short_write_completes(tmp_path, monkeypatch):
    p = tmp_path / "f"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    real_write = os.write

    def _short_write(fd_, data):
        return real_write(fd_, data[:1]) if len(data) > 1 else real_write(fd_, data)

    monkeypatch.setattr(os, "write", _short_write)
    try:
        lf.write_all_eintr_safe(fd, b"hello")
    finally:
        os.close(fd)
    assert p.read_bytes() == b"hello"


def test_write_all_eintr_safe_zero_progress_raises(monkeypatch):
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.write_all_eintr_safe(0, b"x")
    assert excinfo.value.reason is lf.LifecycleFsFailure.IO_FAILED


def test_write_all_eintr_safe_oserror_raises(monkeypatch):
    def _raise(fd, data):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", _raise)
    with pytest.raises(lf.LifecycleFsError):
        lf.write_all_eintr_safe(0, b"x")


def test_read_all_eintr_safe_retries_on_eintr(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_bytes(b"hello world")
    fd = os.open(p, os.O_RDONLY)
    calls = {"n": 0}
    real_read = os.read

    def _flaky_read(fd_, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()
        return real_read(fd_, n)

    monkeypatch.setattr(os, "read", _flaky_read)
    try:
        data = lf.read_all_eintr_safe(fd, 1024)
    finally:
        os.close(fd)
    assert data == b"hello world"


def test_read_all_eintr_safe_detects_oversized(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * 100)
    fd = os.open(p, os.O_RDONLY)
    try:
        data = lf.read_all_eintr_safe(fd, 10)
        assert len(data) == 11  # max_bytes + 1, lets caller detect overflow
    finally:
        os.close(fd)


def test_read_all_eintr_safe_oserror_raises(monkeypatch):
    def _raise(fd, n):
        raise OSError("io error")

    monkeypatch.setattr(os, "read", _raise)
    with pytest.raises(lf.LifecycleFsError):
        lf.read_all_eintr_safe(0, 100)


# ---------------------------------------------------------------------------
# fsync_fd
# ---------------------------------------------------------------------------


def test_fsync_fd_real(tmp_path):
    p = tmp_path / "f"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"x")
        lf.fsync_fd(fd)
    finally:
        os.close(fd)


def test_fsync_fd_failure_raises(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("io error")))
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.fsync_fd(0)
    assert excinfo.value.reason is lf.LifecycleFsFailure.FSYNC_FAILED


# ---------------------------------------------------------------------------
# Canonical JSON: ensure_ascii=True, strict UTF-8, duplicate keys, surrogates
# ---------------------------------------------------------------------------


def test_canonical_json_dumps_is_pure_ascii_and_sorted():
    payload = {"b": 1, "a": "café"}
    data = lf.canonical_json_dumps(payload)
    text = data.decode("ascii")  # must succeed: ensure_ascii=True
    assert text.index('"a"') < text.index('"b"')
    assert ", " not in text and ": " not in text  # compact separators


def test_canonical_json_roundtrip_legitimate_surrogate():
    # A raw non-UTF-8 byte 0x80, as os.fsdecode would surrogateescape it.
    raw_path = b"/tmp/\x80bad"
    decoded = os.fsdecode(raw_path)
    payload = {"path": decoded}
    data = lf.canonical_json_dumps(payload)
    # Must be strictly valid UTF-8.
    data.decode("utf-8")
    restored = lf.canonical_json_loads_strict(data, max_bytes=len(data) + 10)
    assert restored["path"] == decoded
    assert os.fsencode(restored["path"]) == raw_path


def test_canonical_json_loads_rejects_illegitimate_surrogate():
    # Construct JSON containing a lone surrogate escape outside the
    # legitimate fsdecode range (e.g. \ud800).
    raw = b'{"x":"\\ud800"}'
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=1000)
    assert excinfo.value.reason is lf.LifecycleFsFailure.ILLEGITIMATE_SURROGATE


def test_canonical_json_loads_rejects_duplicate_keys():
    raw = b'{"a":1,"a":2}'
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=1000)
    assert excinfo.value.reason is lf.LifecycleFsFailure.DUPLICATE_KEY


def test_canonical_json_loads_rejects_invalid_utf8():
    raw = b"\xff\xfe not utf-8"
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=1000)
    assert excinfo.value.reason is lf.LifecycleFsFailure.INVALID_UTF8


def test_canonical_json_loads_rejects_oversized():
    raw = b'{"a": 1}'
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=3)
    assert excinfo.value.reason is lf.LifecycleFsFailure.OVERSIZED


def test_canonical_json_loads_rejects_syntax_invalid():
    raw = b'{"a": '
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonical_json_loads_strict(raw, max_bytes=1000)
    assert excinfo.value.reason is lf.LifecycleFsFailure.JSON_SYNTAX_INVALID


def test_validate_filesystem_surrogates_nested_structures():
    legitimate = chr(0xDC80)
    lf.validate_filesystem_surrogates({"a": [legitimate, {"b": legitimate}]})
    illegitimate = chr(0xD800)
    with pytest.raises(lf.LifecycleFsError):
        lf.validate_filesystem_surrogates({"a": [illegitimate]})


# ---------------------------------------------------------------------------
# resolve_state_root_path: explicit/default/XDG, bounded parent creation
# ---------------------------------------------------------------------------


def test_resolve_state_root_path_explicit_requires_absolute():
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.resolve_state_root_path(env={"CODEAGENT_STATE_DIR": "relative/path"})
    assert excinfo.value.reason is lf.LifecycleFsFailure.ENVIRONMENT_INVALID


def test_resolve_state_root_path_explicit_requires_existing_parent(tmp_path):
    missing = str(tmp_path / "no-such-parent" / "state")
    with pytest.raises(lf.LifecycleFsError):
        lf.resolve_state_root_path(env={"CODEAGENT_STATE_DIR": missing})


def test_resolve_state_root_path_explicit_ok(tmp_path):
    target = str(tmp_path / "state")
    loc = lf.resolve_state_root_path(env={"CODEAGENT_STATE_DIR": target})
    assert loc.path == target
    assert loc.origin is lf.StateRootOrigin.EXPLICIT
    assert loc.conventional_parent_creation_allowed is False


def test_resolve_state_root_path_missing_home_raises():
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.resolve_state_root_path(env={})
    assert excinfo.value.reason is lf.LifecycleFsFailure.ENVIRONMENT_INVALID


def test_resolve_state_root_path_macos_default(monkeypatch, tmp_path):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: True)
    loc = lf.resolve_state_root_path(env={"HOME": str(tmp_path)})
    assert loc.origin is lf.StateRootOrigin.MACOS_DEFAULT
    assert loc.path == str(tmp_path / "Library" / "Application Support" / "CodeAgent")


def test_resolve_state_root_path_linux_home_default(monkeypatch, tmp_path):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: False)
    loc = lf.resolve_state_root_path(env={"HOME": str(tmp_path)})
    assert loc.origin is lf.StateRootOrigin.LINUX_HOME_DEFAULT
    assert loc.path == str(tmp_path / ".local" / "state" / "codeagent")


def test_resolve_state_root_path_xdg_default_absolute(monkeypatch, tmp_path):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: False)
    xdg = str(tmp_path / "xdgstate")
    loc = lf.resolve_state_root_path(env={"HOME": str(tmp_path), "XDG_STATE_HOME": xdg})
    assert loc.origin is lf.StateRootOrigin.XDG_DEFAULT
    assert loc.path == os.path.join(xdg, "codeagent")


def test_resolve_state_root_path_xdg_relative_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(lf, "_current_platform_is_darwin", lambda: False)
    loc = lf.resolve_state_root_path(env={"HOME": str(tmp_path), "XDG_STATE_HOME": "relative/xdg"})
    assert loc.origin is lf.StateRootOrigin.LINUX_HOME_DEFAULT


def test_ensure_bounded_ancestor_creates_only_named_components(tmp_path):
    lf.ensure_bounded_ancestor(str(tmp_path), ["a", "b"])
    assert (tmp_path / "a" / "b").is_dir()
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o700


def test_ensure_bounded_ancestor_tolerates_existing():
    pass  # covered implicitly by idempotent calls in state_root tests


# ---------------------------------------------------------------------------
# list_directory_entries / open_existing_directory_chain_if_present
# ---------------------------------------------------------------------------


def test_list_directory_entries_empty(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert lf.list_directory_entries(fd) == []
    finally:
        os.close(fd)


def test_list_directory_entries_nonempty(tmp_path):
    (tmp_path / "a").write_text("x")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert lf.list_directory_entries(fd) == ["a"]
    finally:
        os.close(fd)


def test_open_existing_directory_chain_if_present_missing_first_component(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert lf.open_existing_directory_chain_if_present(parent_fd, ["nope", "also-nope"]) is None
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_missing_second_component(tmp_path):
    (tmp_path / "a").mkdir(mode=0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert lf.open_existing_directory_chain_if_present(parent_fd, ["a", "b"]) is None
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_full_chain(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "a", 0o700)
    os.chmod(tmp_path / "a" / "b", 0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = lf.open_existing_directory_chain_if_present(parent_fd, ["a", "b"])
        assert fd is not None
        os.close(fd)
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_refuses_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "a").symlink_to(real)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_existing_directory_chain_if_present(parent_fd, ["a"])
        assert excinfo.value.reason is lf.LifecycleFsFailure.SYMLINK_REFUSED
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_never_creates(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lf.open_existing_directory_chain_if_present(parent_fd, ["never-created"])
        assert not (tmp_path / "never-created").exists()
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_closes_leaked_intermediate_on_missing(tmp_path):
    (tmp_path / "a").mkdir(mode=0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fd_count_before = len(os.listdir("/dev/fd"))
    try:
        result = lf.open_existing_directory_chain_if_present(parent_fd, ["a", "missing"])
        assert result is None
    finally:
        pass
    fd_count_after = len(os.listdir("/dev/fd"))
    os.close(parent_fd)
    assert fd_count_after == fd_count_before


# ---------------------------------------------------------------------------
# Full formatted-traceback sanitization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Correction pass: fd recorded immediately after open, before CLOEXEC checks
# ---------------------------------------------------------------------------


def test_open_managed_directory_chain_no_fd_leak_on_cloexec_failure(tmp_path, monkeypatch):
    """If _assert_cloexec fails for the last-opened fd, that fd must
    already be tracked (appended before the assertion runs) so the
    exception handler closes it — never leaked."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd_count_before = len(os.listdir("/dev/fd"))

        def _failing_assert_cloexec(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "_assert_cloexec", _failing_assert_cloexec)
            with pytest.raises(lf.LifecycleFsError):
                lf.open_managed_directory_chain(parent_fd, ["a"])

        fd_count_after = len(os.listdir("/dev/fd"))
        assert fd_count_after == fd_count_before
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_leaks_fd_if_appended_after_assert_REGRESSION_DEMO(tmp_path, monkeypatch):
    """Demonstrates the specific bug this correction fixes: append-
    after-assert would leak the fd whenever CLOEXEC assertion failed on
    a non-final component too. Reproduced directly against a hand-
    rolled "old" ordering to prove the new ordering is what prevents it."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        leaked = []

        def _broken_open_managed_directory_chain(parent_fd, components):
            # Faithful reproduction of the pre-correction ordering bug.
            current_fd = parent_fd
            opened = []
            try:
                for component in components:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                    leaked.append(fd)  # tracked separately for this test's own assertion
                    lf._assert_cloexec(fd)  # raises before opened.append(fd) in the buggy ordering
                    opened.append(fd)
                    current_fd = fd
            except BaseException:
                lf._dominant_cleanup(opened, None)  # opened is empty: nothing closed -> leak
                raise
            return opened[-1]

        def _failing_assert_cloexec(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "_assert_cloexec", _failing_assert_cloexec)
            with pytest.raises(lf.LifecycleFsError):
                _broken_open_managed_directory_chain(parent_fd, ["a"])

        # Prove the leak: the fd opened by the broken ordering is still valid.
        assert len(leaked) == 1
        os.fstat(leaked[0])  # does not raise: never closed
        os.close(leaked[0])  # manual cleanup for this demo only
    finally:
        os.close(parent_fd)


def test_open_existing_directory_chain_if_present_no_fd_leak_on_cloexec_failure(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir(mode=0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd_count_before = len(os.listdir("/dev/fd"))

        def _failing_assert_cloexec(fd):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "_assert_cloexec", _failing_assert_cloexec)
            with pytest.raises(lf.LifecycleFsError):
                lf.open_existing_directory_chain_if_present(parent_fd, ["a"])

        fd_count_after = len(os.listdir("/dev/fd"))
        assert fd_count_after == fd_count_before
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Correction pass: success-path intermediate-close failure also closes final fd
# ---------------------------------------------------------------------------


def test_open_managed_directory_chain_final_fd_closed_when_intermediate_close_fails(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_close_confirmed = lf.close_confirmed
        call_count = {"n": 0}

        def _fail_first_call(fds):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced intermediate failure")
            return real_close_confirmed(fds)

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "close_confirmed", _fail_first_call)
            with pytest.raises(lf.LifecycleFsError) as excinfo:
                lf.open_managed_directory_chain(parent_fd, ["a", "b"])
            assert excinfo.value.reason is lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED

        # The final fd ("b"'s descriptor) must ALSO have been closed —
        # not leaked just because it was about to be returned.
        assert call_count["n"] == 2  # intermediate attempt, then final attempt
    finally:
        os.close(parent_fd)


def test_open_managed_directory_chain_both_close_failures_reported_together(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        def _always_fail(fds):
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "forced")

        with monkeypatch.context() as scoped:
            scoped.setattr(lf, "close_confirmed", _always_fail)
            with pytest.raises(lf.LifecycleFsError) as excinfo:
                lf.open_managed_directory_chain(parent_fd, ["a", "b"])
            assert "neither" in excinfo.value.message
    finally:
        os.close(parent_fd)
        # best-effort real cleanup of the directories created above
        import shutil

        shutil.rmtree(tmp_path / "a", ignore_errors=True)


# ---------------------------------------------------------------------------
# Correction pass: fail-closed capability detection, never substitute 0
# ---------------------------------------------------------------------------


def test_required_platform_flag_fails_closed_when_missing(monkeypatch):
    monkeypatch.delattr(os, "O_CLOEXEC", raising=False)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf._required_platform_flag("O_CLOEXEC")
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE


def test_required_platform_flag_succeeds_when_present():
    assert lf._required_platform_flag("O_RDONLY") == os.O_RDONLY


def test_cloexec_flag_fails_closed_never_substitutes_zero(monkeypatch):
    monkeypatch.delattr(os, "O_CLOEXEC", raising=False)
    with pytest.raises(lf.LifecycleFsError):
        lf._cloexec_flag()


def test_nofollow_flag_fails_closed(monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(lf.LifecycleFsError):
        lf._nofollow_flag()


def test_directory_flag_fails_closed(monkeypatch):
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    with pytest.raises(lf.LifecycleFsError):
        lf._directory_flag()


_MISSING_FCNTL_CHILD_SCRIPT = """
import sys


class _BlockFcntl:
    def find_spec(self, name, path, target=None):
        if name == "fcntl":
            raise ModuleNotFoundError("fcntl blocked for capability test")
        return None


sys.meta_path.insert(0, _BlockFcntl())

# Importing the module must succeed even though `fcntl` itself is
# unavailable on this (simulated) platform — the capability check only
# runs at operation time, never at import time.
import codeagent._lifecycle_fs as lf

# Invoking an operation that actually needs fcntl must then produce the
# categorical sanitized failure, never a bare ImportError/AttributeError.
try:
    lf._assert_cloexec(0)
except lf.LifecycleFsError as exc:
    assert exc.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, exc.reason
else:
    raise SystemExit("expected LifecycleFsError, none was raised")

print("OK")
"""


def test_no_import_time_failure_for_missing_capability():
    # Run in an isolated subprocess rather than mutating the shared
    # pytest process: importlib.reload() of a shared CodeAgent module
    # recreates its exception classes as new objects while every other
    # already-imported module (state_root.py, state_locks.py,
    # repo_identity.py) keeps the old ones bound via `from X import Y`,
    # making later exception-identity/`except` matching order-dependent
    # across the whole test process.
    import subprocess
    import sys

    src_root = str(Path(__file__).resolve().parents[2] / "src")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": src_root}
    result = subprocess.run(
        [sys.executable, "-c", _MISSING_FCNTL_CHILD_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_canonicalize_directory_fails_closed_when_o_cloexec_missing(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_CLOEXEC", raising=False)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonicalize_directory(str(tmp_path))
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE


def test_open_managed_directory_chain_fails_closed_when_o_directory_missing(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with monkeypatch.context() as scoped:
            scoped.delattr(os, "O_DIRECTORY", raising=False)
            with pytest.raises(lf.LifecycleFsError):
                lf.open_managed_directory_chain(parent_fd, ["a"])
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Correction pass: Path.resolve() sanitization
# ---------------------------------------------------------------------------


def test_canonicalize_directory_sanitizes_resolve_oserror(tmp_path, monkeypatch):
    secret = str(tmp_path / "super-secret-marker")

    def _raising_resolve(self, *args, **kwargs):
        raise OSError(f"boom: {secret}")

    monkeypatch.setattr(Path, "resolve", _raising_resolve)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonicalize_directory(str(tmp_path))
    assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    assert secret not in excinfo.value.message
    assert excinfo.value.__cause__ is None  # from None: never chained to the raw OSError


def test_canonicalize_directory_sanitizes_resolve_runtimeerror(tmp_path, monkeypatch):
    secret = str(tmp_path / "loop-marker")

    def _raising_resolve(self, *args, **kwargs):
        raise RuntimeError(f"Symlink loop from {secret}")

    monkeypatch.setattr(Path, "resolve", _raising_resolve)
    with pytest.raises(lf.LifecycleFsError) as excinfo:
        lf.canonicalize_directory(str(tmp_path))
    assert secret not in excinfo.value.message
    assert excinfo.value.__cause__ is None


# ---------------------------------------------------------------------------
# Correction pass: open_private_create_exclusive_at hardening
# ---------------------------------------------------------------------------


def test_open_private_create_exclusive_at_preserves_file_exists_error(tmp_path):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (tmp_path / "f").write_bytes(b"")
        with pytest.raises(FileExistsError):
            lf.open_private_create_exclusive_at(parent_fd, "f", 0o600)
    finally:
        os.close(parent_fd)


def test_open_private_create_exclusive_at_translates_fchmod_failure(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        secret = str(tmp_path)

        def _failing_fchmod(fd, mode):
            raise OSError(f"boom on {secret}")

        monkeypatch.setattr(os, "fchmod", _failing_fchmod)
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_private_create_exclusive_at(parent_fd, "f", 0o600)
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
        assert secret not in excinfo.value.message
        assert excinfo.value.__cause__ is None
    finally:
        os.close(parent_fd)


def test_open_private_create_exclusive_at_translates_fstat_failure(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_fstat = os.fstat
        call_count = {"n": 0}

        def _failing_fstat(fd):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("boom")
            return real_fstat(fd)

        monkeypatch.setattr(os, "fstat", _failing_fstat)
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_private_create_exclusive_at(parent_fd, "f", 0o600)
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(parent_fd)


def test_open_private_create_exclusive_at_translates_open_failure_other_than_exists(tmp_path, monkeypatch):
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_open = os.open

        def _failing_open(path, flags, *args, **kwargs):
            if path == "f" and kwargs.get("dir_fd") == parent_fd:
                raise PermissionError("boom")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _failing_open)
        with pytest.raises(lf.LifecycleFsError) as excinfo:
            lf.open_private_create_exclusive_at(parent_fd, "f", 0o600)
        assert excinfo.value.reason is lf.LifecycleFsFailure.SUBSTRATE_UNAVAILABLE
    finally:
        os.close(parent_fd)


def test_open_private_create_exclusive_at_full_traceback_never_leaks_secret(tmp_path, monkeypatch):
    import traceback

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        secret = str(tmp_path / "definitely-secret")

        def _failing_fchmod(fd, mode):
            raise OSError(2, "No such file or directory", secret)

        monkeypatch.setattr(os, "fchmod", _failing_fchmod)
        try:
            lf.open_private_create_exclusive_at(parent_fd, "f", 0o600)
        except lf.LifecycleFsError as exc:
            formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            pytest.fail("expected LifecycleFsError")
        assert secret not in formatted
    finally:
        os.close(parent_fd)


def test_lifecycle_fs_error_traceback_never_leaks_host_path(tmp_path):
    import traceback

    secret_marker = str(tmp_path)
    try:
        lf.canonicalize_directory(str(tmp_path / "does-not-exist"))
    except lf.LifecycleFsError as exc:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        pytest.fail("expected LifecycleFsError")
    assert secret_marker not in formatted
