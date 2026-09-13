"""Focused tests for spike_s4.py's own helper logic -- not part of the
main CodeAgent test suite, not collected by pyproject.toml's
`testpaths = ["tests"]`. Run directly:

    pytest spikes/s4/test_spike_s4.py

These test the spike's own harness logic (flag derivation, result
combination, resource-manifest bookkeeping) without requiring Docker.
The isolation checks themselves are exercised for real by running
`spike_s4.py` directly; that real Docker evidence is retained in this
directory's JSON files, not re-derived by these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import spike_s4 as s  # noqa: E402


def test_flags_without_removes_exactly_one_pair() -> None:
    result = s.flags_without("--network")
    assert "--network" not in result
    assert "none" not in result
    # everything else from the real imported tuple is untouched
    assert "--read-only" in result
    assert result.count("--cap-drop") == 1


def test_flags_without_boolean_flag_removes_one_element() -> None:
    result = s.flags_without("--read-only", has_value=False)
    assert "--read-only" not in result
    # the very next real element (--tmpfs) must still be present
    assert "--tmpfs" in result


def test_flags_without_is_derived_from_the_real_imported_tuple() -> None:
    """Never an independently retyped 'equivalent' tuple -- removing
    nothing recognizable from the real _SECURITY_FLAGS must fail loudly."""
    with pytest.raises(ValueError):
        s.flags_without("--this-flag-does-not-exist")


def test_flags_with_appends_without_mutating_the_original() -> None:
    original = tuple(s._SECURITY_FLAGS)
    result = s.flags_with("--cap-add", "SYS_ADMIN")
    assert result[: len(original)] == original
    assert result[-2:] == ("--cap-add", "SYS_ADMIN")
    assert s._SECURITY_FLAGS == original  # not mutated


def test_flags_replacing_user_swaps_only_the_user_pair() -> None:
    result = s.flags_replacing_user("0:0")
    assert "--user" in result
    idx = result.index("--user")
    assert result[idx + 1] == "0:0"
    assert "1000:1000" not in result


def test_combine_pass_pass_is_pass() -> None:
    assert s.combine(s.PASS, s.PASS) == s.PASS


def test_combine_pass_with_no_control_is_pass() -> None:
    assert s.combine(s.PASS, None) == s.PASS


def test_combine_pass_with_failed_control_is_inconclusive_never_pass() -> None:
    assert s.combine(s.PASS, s.FAIL) == s.INCONCLUSIVE


def test_combine_pass_with_inconclusive_control_is_inconclusive() -> None:
    assert s.combine(s.PASS, s.INCONCLUSIVE) == s.INCONCLUSIVE


def test_combine_primary_fail_is_always_fail_regardless_of_control() -> None:
    assert s.combine(s.FAIL, s.PASS) == s.FAIL
    assert s.combine(s.FAIL, None) == s.FAIL


def test_combine_primary_technical_failure_is_always_technical_failure() -> None:
    assert s.combine(s.TECHNICAL_FAILURE, s.PASS) == s.TECHNICAL_FAILURE


def test_manifest_tracks_every_resource_type() -> None:
    m = s.Manifest()
    m.add_container("c1")
    m.add_network("n1")
    m.add_image("i1:tag")
    m.add_dir(Path("/tmp/does-not-matter-for-this-test"))
    assert m.containers == ["c1"]
    assert m.networks == ["n1"]
    assert m.images == ["i1:tag"]
    assert len(m.dirs) == 1


def test_new_scratch_dir_is_tracked_in_the_module_manifest(tmp_path, monkeypatch) -> None:
    fresh_manifest = s.Manifest()
    monkeypatch.setattr(s, "MANIFEST", fresh_manifest)
    monkeypatch.setattr(s.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "created"))
    Path(tmp_path / "created").mkdir()
    d = s.new_scratch_dir("unit-test")
    assert d in fresh_manifest.dirs


def test_new_name_is_unique_across_calls() -> None:
    a = s.new_name("x")
    b = s.new_name("x")
    assert a != b
    assert a.startswith("codeagent-spike-s4-x-")


def test_tested_production_values_are_imported_not_retyped() -> None:
    """The whole point of correction 1: these must come from the real
    module, not a copy that could silently drift."""
    from codeagent import executor

    assert s.DEFAULT_IMAGE is executor.DEFAULT_IMAGE
    assert s._SECURITY_FLAGS is executor._SECURITY_FLAGS
    assert s._MAX_STREAM_BYTES is executor._MAX_STREAM_BYTES
    assert s.DockerVerifier is executor.DockerVerifier
    assert s.CONTAINER_NAME_PREFIX is executor.CONTAINER_NAME_PREFIX
