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


def test_exit_code_for_overall_verdict_fail_is_nonzero() -> None:
    assert s.exit_code_for_overall_verdict(s.FAIL) == 1


def test_exit_code_for_overall_verdict_pass_is_zero() -> None:
    assert s.exit_code_for_overall_verdict(s.PASS) == 0


def test_exit_code_for_overall_verdict_pass_with_open_risks_is_zero() -> None:
    """PASS_WITH_OPEN_RISKS is a successful evidence-gathering run --
    an inconclusive check is reported accurately, not treated as a
    workflow failure."""
    assert s.exit_code_for_overall_verdict("PASS_WITH_OPEN_RISKS") == 0


# --------------------------------------------------------------------
# classify_mounts_check: incorrect/unexpected mount config or a
# visible host canary is a real security FAIL; an inaccessible
# intended fixture, malformed probe output, or a docker-exec
# infrastructure failure is a TECHNICAL_FAILURE, never a security FAIL.
# --------------------------------------------------------------------

_GOOD_PROBE = {
    "expected_readable": True,
    "expected_content_matches": True,
    "host_secret_absent_at_root": True,
    "host_secret_absent_at_host_path": True,
}


def test_classify_mounts_check_all_good_is_pass() -> None:
    assert s.classify_mounts_check(True, True, 0, _GOOD_PROBE) == s.PASS


def test_classify_mounts_check_bad_mounts_config_is_fail() -> None:
    assert s.classify_mounts_check(False, True, 0, _GOOD_PROBE) == s.FAIL


def test_classify_mounts_check_bad_tmpfs_config_is_fail() -> None:
    assert s.classify_mounts_check(True, False, 0, _GOOD_PROBE) == s.FAIL


def test_classify_mounts_check_visible_canary_at_root_is_fail() -> None:
    probe = {**_GOOD_PROBE, "host_secret_absent_at_root": False}
    assert s.classify_mounts_check(True, True, 0, probe) == s.FAIL


def test_classify_mounts_check_visible_canary_at_host_path_is_fail() -> None:
    probe = {**_GOOD_PROBE, "host_secret_absent_at_host_path": False}
    assert s.classify_mounts_check(True, True, 0, probe) == s.FAIL


def test_classify_mounts_check_exec_infra_failure_is_technical_failure_not_fail() -> None:
    """A docker-exec failure means we learned nothing about isolation
    -- it must never be reported as if a security check failed."""
    assert s.classify_mounts_check(True, True, 1, None) == s.TECHNICAL_FAILURE


def test_classify_mounts_check_unparseable_probe_is_technical_failure() -> None:
    assert s.classify_mounts_check(True, True, 0, None) == s.TECHNICAL_FAILURE


def test_classify_mounts_check_unreadable_intended_fixture_is_technical_failure() -> None:
    """The intended-readable fixture being unreadable is this
    harness's own problem (e.g. a permissions fix regression), not a
    security finding about the container's isolation."""
    probe = {**_GOOD_PROBE, "expected_readable": False}
    assert s.classify_mounts_check(True, True, 0, probe) == s.TECHNICAL_FAILURE


def test_classify_mounts_check_wrong_fixture_content_is_technical_failure() -> None:
    probe = {**_GOOD_PROBE, "expected_content_matches": False}
    assert s.classify_mounts_check(True, True, 0, probe) == s.TECHNICAL_FAILURE


def test_classify_mounts_check_bad_mounts_config_beats_exec_failure() -> None:
    """A real security-relevant config problem is reported as FAIL
    even if the exec step also failed -- the mount misconfiguration is
    the more important fact to surface."""
    assert s.classify_mounts_check(False, True, 1, None) == s.FAIL
