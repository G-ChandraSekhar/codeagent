"""Tests for `codeagent.abandonment` and the exclusive hard-link publication
primitive it uses (ADR 0004 section 11, Amendment 18)."""

from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

import pytest

from codeagent import _lifecycle_fs as lf
from codeagent import abandonment as ab

LID = "a" * 32
RKEY = "b" * 32
SRID = "c" * 32
MID = "d" * 32
TS = "2026-10-04T00:00:00Z"
UNRESOLVED_SUMMARY = {
    "baseline_container": "present",
    "verification_container": "absent",
    "worktree_registration": "absent",
    "worktree_admin_entry": "absent",
    "worktree_directory": "unknown",
    "checkpoint_ref": "absent",
}


def _marker(disposition=ab.AbandonmentDisposition.ABANDONED, **overrides):
    values = dict(
        lifecycle_id=LID,
        repo_key=RKEY,
        state_root_id=SRID,
        disposition=disposition,
        maintenance_id=MID,
        timestamp=TS,
    )
    if disposition is ab.AbandonmentDisposition.ABANDONED_UNRESOLVED:
        values.update(reason="operator says so", remaining=dict(UNRESOLVED_SUMMARY))
    values.update(overrides)
    return ab.AbandonmentMarker(**values)


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def run_fd(run_dir):
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _load(run_fd, run_dir):
    return ab.load_abandonment_marker(
        run_fd,
        names=os.listdir(run_dir),
        expected_lifecycle_id=LID,
        expected_repo_key=RKEY,
        expected_state_root_id=SRID,
    )


def _write(path: Path, data: bytes, mode=0o600):
    path.write_bytes(data)
    path.chmod(mode)


def _payload(disposition="ABANDONED", **overrides):
    p = {
        "schema_version": 1,
        "lifecycle_id": LID,
        "repo_key": RKEY,
        "state_root_id": SRID,
        "disposition": disposition,
        "maintenance_id": MID,
        "timestamp": TS,
    }
    if disposition == "ABANDONED_UNRESOLVED":
        p.update(reason="r", remaining=dict(UNRESOLVED_SUMMARY))
    p.update(overrides)
    return p


def _temp_name(i=0):
    return f".abandonment.json.tmp-{i:016x}"


# ---------------------------------------------------------------------------
# validate_reason (U9)
# ---------------------------------------------------------------------------


def test_reason_accepts_ordinary_text_unchanged():
    text = "container left by a crashed CI job; removed by hand — ticket 42"
    assert ab.validate_reason(text) == text


def test_reason_accepts_exactly_256_bytes():
    assert ab.validate_reason("x" * 256) == "x" * 256


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "\t\n",
        "x" * 257,
        "é" * 129,  # 258 UTF-8 bytes
        "line\nbreak",
        "carriage\rreturn",
        "tab\there",
        "esc\x1b[31mred",
        "del\x7f",
        "nel\u0085",
        "bidi‮override",
        "line separator",
        "para separator",
        "zero​width",
        "lone\ud800surrogate",
        None,
        42,
    ],
)
def test_reason_rejected_without_echo(bad):
    with pytest.raises(ValueError) as excinfo:
        ab.validate_reason(bad)
    if isinstance(bad, str) and bad.strip():
        assert bad not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Marker schema and repr (C2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("disposition", list(ab.AbandonmentDisposition))
def test_marker_round_trip(disposition, run_fd, run_dir):
    marker = _marker(disposition)
    data = ab.marker_to_bytes(marker)
    assert len(data) <= ab.MARKER_MAX_BYTES
    assert data == lf.canonical_json_dumps(json.loads(data))
    _write(run_dir / ab.MARKER_FILENAME, data)
    assert _load(run_fd, run_dir) == marker


def test_marker_repr_never_contains_the_reason():
    secret = "/Users/SENTINEL-REASON-9b1c/secret"
    marker = _marker(ab.AbandonmentDisposition.ABANDONED_UNRESOLVED, reason=secret)
    assert secret not in repr(marker)
    assert secret not in str(marker)


def test_marker_to_bytes_refuses_unresolved_with_nothing_remaining():
    with pytest.raises(ab.AbandonmentMarkerError):
        ab.marker_to_bytes(
            _marker(ab.AbandonmentDisposition.ABANDONED_UNRESOLVED, remaining={k: "absent" for k in ab.RESOURCE_FIELDS})
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_returns_none_without_a_final_marker(run_fd, run_dir):
    assert _load(run_fd, run_dir) is None


@pytest.mark.parametrize("count", [1, 3, 16])
def test_loader_returns_none_when_only_temps_exist(run_fd, run_dir, count):
    """C1: a recognized temp alone never proves abandonment."""
    for i in range(count):
        _write(run_dir / _temp_name(i), ab.marker_to_bytes(_marker()))
    assert _load(run_fd, run_dir) is None


def _refused(run_fd, run_dir):
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        _load(run_fd, run_dir)
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.REFUSED
    return excinfo.value


def test_loader_refuses_symlink(run_fd, run_dir, tmp_path):
    target = tmp_path / "elsewhere.json"
    _write(target, ab.marker_to_bytes(_marker()))
    (run_dir / ab.MARKER_FILENAME).symlink_to(target)
    _refused(run_fd, run_dir)


def test_loader_refuses_directory(run_fd, run_dir):
    (run_dir / ab.MARKER_FILENAME).mkdir()
    _refused(run_fd, run_dir)


def test_loader_refuses_mode_0644(run_fd, run_dir):
    _write(run_dir / ab.MARKER_FILENAME, ab.marker_to_bytes(_marker()), mode=0o644)
    _refused(run_fd, run_dir)


def test_loader_refuses_foreign_owner(run_fd, run_dir, monkeypatch):
    _write(run_dir / ab.MARKER_FILENAME, ab.marker_to_bytes(_marker()))
    uid = os.getuid()
    monkeypatch.setattr(ab.os, "getuid", lambda: uid + 1)
    _refused(run_fd, run_dir)


def test_loader_refuses_hard_link_without_twin(run_fd, run_dir, tmp_path):
    """M14: nlink 2 with no recognized temp twin."""
    final = run_dir / ab.MARKER_FILENAME
    _write(final, ab.marker_to_bytes(_marker()))
    os.link(final, tmp_path / "outside-link")
    _refused(run_fd, run_dir)


def test_loader_refuses_hard_link_with_different_inode_temp(run_fd, run_dir, tmp_path):
    final = run_dir / ab.MARKER_FILENAME
    _write(final, ab.marker_to_bytes(_marker()))
    os.link(final, tmp_path / "outside-link")
    _write(run_dir / _temp_name(), ab.marker_to_bytes(_marker()))  # different inode
    _refused(run_fd, run_dir)


def test_loader_refuses_link_count_three(run_fd, run_dir, tmp_path):
    final = run_dir / ab.MARKER_FILENAME
    _write(final, ab.marker_to_bytes(_marker()))
    os.link(final, run_dir / _temp_name())
    os.link(final, tmp_path / "outside-link")
    _refused(run_fd, run_dir)


def test_loader_accepts_same_inode_twin_with_other_stale_temps(run_fd, run_dir):
    """C1/U2: the crash between link and unlink, with older stale temps."""
    final = run_dir / ab.MARKER_FILENAME
    _write(final, ab.marker_to_bytes(_marker()))
    os.link(final, run_dir / _temp_name(1))
    _write(run_dir / _temp_name(2), b"stale")
    _write(run_dir / _temp_name(3), b"")
    assert _load(run_fd, run_dir) == _marker()


def test_loader_refuses_two_same_inode_twins(run_fd, run_dir):
    final = run_dir / ab.MARKER_FILENAME
    _write(final, ab.marker_to_bytes(_marker()))
    os.link(final, run_dir / _temp_name(1))
    os.link(final, run_dir / _temp_name(2))
    _refused(run_fd, run_dir)


def test_loader_refuses_oversized(run_fd, run_dir):
    _write(run_dir / ab.MARKER_FILENAME, b" " * (ab.MARKER_MAX_BYTES + 1))
    _refused(run_fd, run_dir)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"schema_version":1',
        b'{"a":1,"a":2}',
        b"\xff\xfe",
        b"[]",
    ],
)
def test_loader_refuses_malformed_content(run_fd, run_dir, data):
    _write(run_dir / ab.MARKER_FILENAME, data)
    _refused(run_fd, run_dir)


@pytest.mark.parametrize(
    "payload",
    [
        _payload(extra=1),
        {k: v for k, v in _payload().items() if k != "timestamp"},
        _payload(disposition="CLEAN"),
        _payload(schema_version=2),
        _payload(schema_version=True),
        _payload(lifecycle_id="e" * 32),
        _payload(repo_key="e" * 32),
        _payload(state_root_id="e" * 32),
        _payload(maintenance_id="not-hex"),
        _payload(timestamp="yesterday"),
        _payload(reason="r"),  # reason on ABANDONED
        {k: v for k, v in _payload("ABANDONED_UNRESOLVED").items() if k != "remaining"},
        _payload("ABANDONED_UNRESOLVED", remaining={k: "absent" for k in ab.RESOURCE_FIELDS}),
        _payload("ABANDONED_UNRESOLVED", remaining={**UNRESOLVED_SUMMARY, "checkpoint_ref": "gone"}),
        _payload("ABANDONED_UNRESOLVED", remaining={"baseline_container": "present"}),
        _payload("ABANDONED_UNRESOLVED", reason="bad\nreason"),
        _payload("ABANDONED_UNRESOLVED", reason=""),
    ],
)
def test_loader_refuses_schema_violations(run_fd, run_dir, payload):
    _write(run_dir / ab.MARKER_FILENAME, lf.canonical_json_dumps(payload))
    _refused(run_fd, run_dir)


def test_loader_inspection_failure_is_substrate_unavailable(run_fd, run_dir, monkeypatch):
    _write(run_dir / ab.MARKER_FILENAME, ab.marker_to_bytes(_marker()))
    real = os.lstat

    def lstat(name, *a, **k):
        if name == ab.MARKER_FILENAME:
            raise PermissionError(13, "denied")
        return real(name, *a, **k)

    monkeypatch.setattr(ab.os, "lstat", lstat)
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        _load(run_fd, run_dir)
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# count_stale_marker_temps (C1)
# ---------------------------------------------------------------------------


def test_count_exact_pattern_only(run_fd, run_dir):
    _write(run_dir / _temp_name(1), b"x")
    for near_miss in (
        ".abandonment.json.tmp-xyz",
        ".abandonment.json.tmp-" + "0" * 15,
        ".abandonment.json.tmp-" + "0" * 17,
        ".abandonment.json.tmp-" + "A" * 16,
        "abandonment.json.tmp-" + "0" * 16,
    ):
        _write(run_dir / near_miss, b"x")
    assert ab.count_stale_marker_temps(run_fd, os.listdir(run_dir)) == 1


def test_count_stops_after_the_bound(run_fd, run_dir, monkeypatch):
    for i in range(30):
        _write(run_dir / _temp_name(i), b"x")
    calls = []
    real = os.lstat
    monkeypatch.setattr(ab.os, "lstat", lambda name, *a, **k: calls.append(name) or real(name, *a, **k))
    assert ab.count_stale_marker_temps(run_fd, os.listdir(run_dir)) == ab.ABANDONMENT_TEMP_MAX + 1
    assert len(calls) == ab.ABANDONMENT_TEMP_MAX + 1


@pytest.mark.parametrize("shape", ["symlink", "mode", "nlink", "directory"])
def test_count_refuses_unsafe_temp(run_fd, run_dir, tmp_path, shape):
    temp = run_dir / _temp_name()
    if shape == "symlink":
        temp.symlink_to(tmp_path)
    elif shape == "mode":
        _write(temp, b"x", mode=0o644)
    elif shape == "nlink":
        _write(temp, b"x")
        os.link(temp, tmp_path / "other")
    else:
        temp.mkdir()
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        ab.count_stale_marker_temps(run_fd, os.listdir(run_dir))
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.REFUSED


# ---------------------------------------------------------------------------
# Publication (U2, C5)
# ---------------------------------------------------------------------------


def test_publish_confirmed_leaves_only_the_final_marker(run_fd, run_dir):
    result = ab.publish_abandonment_marker(run_fd, _marker())
    assert result == lf.ExclusivePublication(durable=True, temp_removed=True)
    assert sorted(os.listdir(run_dir)) == [ab.MARKER_FILENAME]
    st = os.stat(run_dir / ab.MARKER_FILENAME)
    assert st.st_nlink == 1 and (st.st_mode & 0o777) == 0o600
    assert _load(run_fd, run_dir) == _marker()


def test_publish_never_replaces_an_existing_marker(run_fd, run_dir):
    """M6: an existing final name is never replaced; the temp is removed."""
    original = ab.marker_to_bytes(_marker())
    _write(run_dir / ab.MARKER_FILENAME, original)
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        ab.publish_abandonment_marker(run_fd, _marker(ab.AbandonmentDisposition.ABANDONED_UNRESOLVED))
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.ALREADY_EXISTS
    assert (run_dir / ab.MARKER_FILENAME).read_bytes() == original
    assert os.listdir(run_dir) == [ab.MARKER_FILENAME]


def test_publish_link_error_installs_nothing(run_fd, run_dir, monkeypatch):
    """C5/M27: a link failure is not a recorded disposition."""
    monkeypatch.setattr(lf.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        ab.publish_abandonment_marker(run_fd, _marker())
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.PUBLICATION_FAILED
    assert os.listdir(run_dir) == []


@pytest.mark.parametrize("stage", ["write", "fsync", "close"])
def test_publish_pre_link_failure_installs_nothing(run_fd, run_dir, monkeypatch, stage):
    if stage == "write":
        monkeypatch.setattr(lf, "write_all_eintr_safe", lambda *a: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.IO_FAILED, "x")))
    elif stage == "fsync":
        monkeypatch.setattr(lf, "fsync_fd", lambda *a: (_ for _ in ()).throw(lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "x")))
    else:
        real = lf.close_confirmed
        calls = []

        def close_confirmed(fds):
            calls.append(fds)
            if len(calls) == 1:
                real(fds)
                raise lf.LifecycleFsError(lf.LifecycleFsFailure.CLEANUP_UNCONFIRMED, "x")
            return real(fds)

        monkeypatch.setattr(lf, "close_confirmed", close_confirmed)
    with pytest.raises(ab.AbandonmentMarkerError):
        ab.publish_abandonment_marker(run_fd, _marker())
    assert ab.MARKER_FILENAME not in os.listdir(run_dir)
    assert os.listdir(run_dir) == []


def test_publish_temp_cleanup_failure_before_link_reports_one_stale_temp(run_fd, run_dir, monkeypatch):
    monkeypatch.setattr(lf.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    monkeypatch.setattr(lf.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    with pytest.raises(ab.AbandonmentMarkerError) as excinfo:
        ab.publish_abandonment_marker(run_fd, _marker())
    assert excinfo.value.reason is ab.AbandonmentMarkerFailure.TEMP_CLEANUP_UNCONFIRMED
    names = os.listdir(run_dir)
    assert len(names) == 1 and ab.MARKER_TEMP_RE.fullmatch(names[0])
    assert _load(run_fd, run_dir) is None


def test_publish_dir_fsync_failure_after_link_is_installed_but_not_durable(run_fd, run_dir, monkeypatch):
    """C5: logically installed at link; durability unconfirmed."""
    real = lf.fsync_fd

    def fsync_fd(fd):
        if fd == run_fd:
            raise lf.LifecycleFsError(lf.LifecycleFsFailure.FSYNC_FAILED, "x")
        return real(fd)

    monkeypatch.setattr(lf, "fsync_fd", fsync_fd)
    result = ab.publish_abandonment_marker(run_fd, _marker())
    assert result == lf.ExclusivePublication(durable=False, temp_removed=True)
    assert _load(run_fd, run_dir) == _marker()


def test_publish_temp_unlink_failure_leaves_a_valid_twin(run_fd, run_dir, monkeypatch):
    monkeypatch.setattr(lf.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    result = ab.publish_abandonment_marker(run_fd, _marker())
    assert result == lf.ExclusivePublication(durable=True, temp_removed=False)
    assert os.stat(run_dir / ab.MARKER_FILENAME).st_nlink == 2
    assert _load(run_fd, run_dir) == _marker()


def test_publish_ordering_link_then_fsync_then_unlink_then_fsync(run_fd, run_dir, monkeypatch):
    """M28: the temp is never removed before the first directory fsync."""
    events = []
    real_fsync, real_link, real_unlink = lf.fsync_fd, lf.os.link, lf.os.unlink

    def fsync_fd(fd):
        events.append("dir_fsync" if fd == run_fd else "file_fsync")
        return real_fsync(fd)

    monkeypatch.setattr(lf, "fsync_fd", fsync_fd)
    monkeypatch.setattr(lf.os, "link", lambda *a, **k: events.append("link") or real_link(*a, **k))
    monkeypatch.setattr(lf.os, "unlink", lambda *a, **k: events.append("unlink") or real_unlink(*a, **k))
    ab.publish_abandonment_marker(run_fd, _marker())
    assert events == ["file_fsync", "link", "dir_fsync", "unlink", "dir_fsync"]


def test_abandonment_module_contains_no_removal_or_rename_call():
    """I16: the leaf never removes, renames, or replaces anything itself."""
    tree = ast.parse(inspect.getsource(ab))
    forbidden = {"unlink", "remove", "rmdir", "rmtree", "rename", "replace", "renames"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            assert name not in forbidden, name
