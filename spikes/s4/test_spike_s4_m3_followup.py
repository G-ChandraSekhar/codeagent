"""Focused tests for spike_s4_m3_followup.py's own harness logic --
not part of the main CodeAgent test suite, not collected by
pyproject.toml's `testpaths = ["tests"]`. Run directly:

    pytest spikes/s4/test_spike_s4_m3_followup.py

These test the follow-up harness's own pure logic (rm --force call
targeting, single-capture extraction, message-sanitization heuristic)
without requiring Docker. The real checks themselves are exercised for
real by running `spike_s4_m3_followup.py` directly; that real evidence
is retained under spikes/s4/evidence/macos-docker-desktop-arm64/
run-m3-followup-<...>/, not re-derived by these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import spike_s4_m3_followup as f  # noqa: E402


# --------------------------------------------------------------------
# is_rm_force_call: precise targeting of the interception
# --------------------------------------------------------------------


def test_is_rm_force_call_matches_exact_shape() -> None:
    assert f.is_rm_force_call(("rm", "--force", "some-container")) == "some-container"


def test_is_rm_force_call_rejects_other_subcommands() -> None:
    assert f.is_rm_force_call(("create", "--name", "x")) is None
    assert f.is_rm_force_call(("ps", "-a", "--format", "{{.Names}}")) is None


def test_is_rm_force_call_rejects_rm_without_force() -> None:
    assert f.is_rm_force_call(("rm", "some-container")) is None


def test_is_rm_force_call_rejects_wrong_arg_count() -> None:
    assert f.is_rm_force_call(("rm", "--force")) is None
    assert f.is_rm_force_call(("rm", "--force", "x", "extra")) is None


# --------------------------------------------------------------------
# make_intercepting_run_docker: always calls through to the real
# function; only inspects on a genuine rm --force call.
# --------------------------------------------------------------------


def test_intercepting_run_docker_calls_through_for_every_call() -> None:
    calls = []

    def fake_original(*args):
        calls.append(args)
        return "real-result"

    captured: dict[str, dict] = {}
    wrapped = f.make_intercepting_run_docker(fake_original, captured)

    assert wrapped("create", "--name", "x") == "real-result"
    assert wrapped("ps", "-a") == "real-result"
    assert calls == [("create", "--name", "x"), ("ps", "-a")]
    assert captured == {}  # neither call was rm --force


def test_intercepting_run_docker_captures_only_on_rm_force(monkeypatch) -> None:
    def fake_inspect(argv, **kwargs):
        class _Result:
            returncode = 0
            stdout = '{"Memory": 536870912, "MemorySwap": 536870912}'
            stderr = ""

        assert argv[:2] == ["docker", "inspect"]
        return _Result()

    monkeypatch.setattr(f.subprocess, "run", fake_inspect)

    calls = []

    def fake_original(*args):
        calls.append(args)
        return "removed"

    captured: dict[str, dict] = {}
    wrapped = f.make_intercepting_run_docker(fake_original, captured)

    result = wrapped("rm", "--force", "my-container")

    assert result == "removed"
    assert calls == [("rm", "--force", "my-container")]
    assert captured == {"my-container": {"Memory": 536870912, "MemorySwap": 536870912}}


def test_intercepting_run_docker_records_inspect_failure_without_crashing(monkeypatch) -> None:
    def fake_inspect(argv, **kwargs):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = "no such container"

        return _Result()

    monkeypatch.setattr(f.subprocess, "run", fake_inspect)

    captured: dict[str, dict] = {}
    wrapped = f.make_intercepting_run_docker(lambda *a: "removed", captured)

    result = wrapped("rm", "--force", "gone-already")

    assert result == "removed"
    assert captured["gone-already"] == {"inspect_error": "no such container"}


def test_intercepting_run_docker_records_exception_without_crashing(monkeypatch) -> None:
    def raise_oserror(argv, **kwargs):
        raise OSError("docker vanished")

    monkeypatch.setattr(f.subprocess, "run", raise_oserror)

    captured: dict[str, dict] = {}
    wrapped = f.make_intercepting_run_docker(lambda *a: "removed", captured)

    result = wrapped("rm", "--force", "x")

    assert result == "removed"
    assert "inspect_exception" in captured["x"]


# --------------------------------------------------------------------
# extract_single_captured_config: fail loudly on zero or multiple
# --------------------------------------------------------------------


def test_extract_single_captured_config_returns_the_one_entry() -> None:
    name, config = f.extract_single_captured_config({"c1": {"Memory": 1}})
    assert name == "c1"
    assert config == {"Memory": 1}


def test_extract_single_captured_config_raises_on_empty() -> None:
    with pytest.raises(AssertionError):
        f.extract_single_captured_config({})


def test_extract_single_captured_config_raises_on_multiple() -> None:
    with pytest.raises(AssertionError):
        f.extract_single_captured_config({"c1": {}, "c2": {}})


# --------------------------------------------------------------------
# is_sanitized_message: mechanical guard against raw payload leakage
# --------------------------------------------------------------------


def test_is_sanitized_message_accepts_the_real_fixed_message() -> None:
    assert f.is_sanitized_message(f._EXPECTED_OOM_MESSAGE) is True


@pytest.mark.parametrize(
    "message",
    [
        '{"OOMKilled": true, "ExitCode": 137}',
        "OOMKilled was true for this container",
        "HostConfig.Memory was 536870912",
        "State.ExitCode: 137",
        "ran docker inspect on the container",
    ],
)
def test_is_sanitized_message_rejects_raw_payload_markers(message: str) -> None:
    assert f.is_sanitized_message(message) is False


# --------------------------------------------------------------------
# Reuse discipline: this follow-up must import spike_s4's real
# primitives and the real production config, never retype them.
# --------------------------------------------------------------------


def test_tested_production_values_are_imported_not_retyped() -> None:
    from codeagent import executor

    assert f.DEFAULT_IMAGE is executor.DEFAULT_IMAGE
    assert f._SECURITY_FLAGS is executor._SECURITY_FLAGS
    assert f.DockerVerifier is executor.DockerVerifier
    assert f.CONTAINER_NAME_PREFIX is executor.CONTAINER_NAME_PREFIX


def test_evidence_dir_is_a_new_subdirectory_of_the_existing_macos_platform_dir() -> None:
    macos_platform_dir = f.base.SPIKE_DIR / "evidence" / "macos-docker-desktop-arm64"
    assert f.EVIDENCE_DIR.parent == macos_platform_dir
    assert f.EVIDENCE_DIR.name.startswith("run-m3-followup-")
    # Never the platform directory itself -- that's where the
    # ORIGINAL S4 run's flat evidence files live; this follow-up must
    # never write there directly.
    assert f.EVIDENCE_DIR != macos_platform_dir
