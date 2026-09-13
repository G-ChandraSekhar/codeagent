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


def test_evidence_dir_is_a_new_subdirectory_of_the_existing_platform_dir() -> None:
    """Whatever platform this test actually runs on (macOS on a dev
    machine, Linux in CI), EVIDENCE_DIR must be a fresh subdirectory of
    THAT platform's existing evidence directory -- reusing spike_s4's
    own _platform_key(), never a retyped copy of it."""
    platform_dir = f.base.SPIKE_DIR / "evidence" / f.base._platform_key()
    assert f.EVIDENCE_DIR.parent == platform_dir
    assert f.EVIDENCE_DIR.name.startswith("run-m3-followup-")
    # Never the platform directory itself -- that's where the
    # ORIGINAL S4 run's flat evidence files live; this follow-up must
    # never write there directly.
    assert f.EVIDENCE_DIR != platform_dir


# --------------------------------------------------------------------
# is_supported_platform: Linux gets a narrower gate (x86_64 only) than
# macOS (any machine) -- unit tested without ever touching the real
# platform module.
# --------------------------------------------------------------------


def test_is_supported_platform_accepts_any_macos_machine() -> None:
    assert f.is_supported_platform("Darwin", "arm64") is True
    assert f.is_supported_platform("Darwin", "x86_64") is True


def test_is_supported_platform_accepts_linux_x86_64_only() -> None:
    assert f.is_supported_platform("Linux", "x86_64") is True
    assert f.is_supported_platform("Linux", "aarch64") is False
    assert f.is_supported_platform("Linux", "arm64") is False


def test_is_supported_platform_rejects_other_systems() -> None:
    assert f.is_supported_platform("Windows", "AMD64") is False
    assert f.is_supported_platform("", "") is False


# --------------------------------------------------------------------
# compute_run_id: GitHub Actions run id + run attempt when present,
# timestamp+random fallback otherwise -- never fabricating a fake run
# id for a local run, and never silently dropping the attempt when a
# run id is present.
# --------------------------------------------------------------------


def test_compute_run_id_uses_github_run_id_and_attempt_when_present() -> None:
    assert f.compute_run_id("34999999999", "1") == "run-m3-followup-34999999999-attempt-1"


def test_compute_run_id_different_attempts_of_the_same_run_id_produce_different_paths() -> None:
    """The whole point of including the attempt: a workflow re-run
    reuses the same run id but increments the attempt, so attempts 1
    and 2 of the SAME run id must never collide."""
    attempt_1 = f.compute_run_id("34999999999", "1")
    attempt_2 = f.compute_run_id("34999999999", "2")
    assert attempt_1 != attempt_2
    assert attempt_1 == "run-m3-followup-34999999999-attempt-1"
    assert attempt_2 == "run-m3-followup-34999999999-attempt-2"


def test_compute_run_id_falls_back_to_timestamp_and_random_id_when_absent() -> None:
    run_id = f.compute_run_id(None, None)
    assert run_id.startswith("run-m3-followup-")
    assert "34999999999" not in run_id
    assert "attempt" not in run_id
    # Two calls without a run id must never collide.
    assert f.compute_run_id(None, None) != f.compute_run_id(None, None)


def test_compute_run_id_treats_empty_run_id_as_absent() -> None:
    run_id = f.compute_run_id("", None)
    assert run_id.startswith("run-m3-followup-")
    assert run_id != "run-m3-followup-"


def test_compute_run_id_ignores_an_attempt_value_when_run_id_is_absent() -> None:
    """An attempt present without a run id is not itself an error --
    there's nothing ambiguous to construct since the fallback naming
    doesn't use the attempt at all. Must not raise."""
    run_id = f.compute_run_id(None, "1")
    assert run_id.startswith("run-m3-followup-")
    assert "attempt" not in run_id


@pytest.mark.parametrize("invalid_attempt", [None, "", "0", "not-a-number", "1.5", "-1", "007"])
def test_compute_run_id_raises_when_run_id_present_but_attempt_missing_or_invalid(
    invalid_attempt: str | None,
) -> None:
    """GITHUB_RUN_ID indicates a real Actions run -- GITHUB_RUN_ATTEMPT
    must then be a canonical positive decimal integer (digits only,
    greater than zero -- "0" is explicitly invalid, same as any
    malformed value), or the evidence path would be ambiguous. Never
    silently fall back to the timestamp+random naming in this case."""
    with pytest.raises(RuntimeError, match="GITHUB_RUN_ATTEMPT"):
        f.compute_run_id("34999999999", invalid_attempt)


@pytest.mark.parametrize("invalid_run_id", ["0", "not-a-number", "1.5", "-1", "007"])
def test_compute_run_id_raises_when_run_id_itself_is_invalid(invalid_run_id: str) -> None:
    """GITHUB_RUN_ID must itself be a canonical positive decimal
    integer -- "0" is explicitly rejected, same as any malformed
    value. The diagnostic must name GITHUB_RUN_ID, not
    GITHUB_RUN_ATTEMPT, since the run id is the field that's actually
    wrong here."""
    with pytest.raises(RuntimeError, match="GITHUB_RUN_ID"):
        f.compute_run_id(invalid_run_id, "1")


def test_compute_run_id_diagnostic_names_run_id_not_attempt_when_run_id_is_invalid() -> None:
    """Even when BOTH fields would be invalid, the diagnostic reported
    is about GITHUB_RUN_ID specifically (checked first) -- it must
    never blame GITHUB_RUN_ATTEMPT for a problem that is actually in
    GITHUB_RUN_ID."""
    with pytest.raises(RuntimeError) as exc_info:
        f.compute_run_id("0", "0")
    assert "GITHUB_RUN_ID" in str(exc_info.value)


def test_compute_run_id_valid_run_id_and_attempt_produce_the_expected_unique_path() -> None:
    """A genuinely valid pair still produces the exact expected,
    unambiguous path -- the stricter validation above must not affect
    the happy path."""
    assert f.compute_run_id("42", "1") == "run-m3-followup-42-attempt-1"
    assert f.compute_run_id("42", "3") == "run-m3-followup-42-attempt-3"
    assert f.compute_run_id("42", "1") != f.compute_run_id("43", "1")


# --------------------------------------------------------------------
# compute_evidence_dir: pure path construction, platform-key-agnostic.
# --------------------------------------------------------------------


def test_compute_evidence_dir_joins_platform_key_and_run_id() -> None:
    spike_dir = Path("/repo/spikes/s4")
    result = f.compute_evidence_dir(spike_dir, "linux-x86_64", "run-m3-followup-123")
    assert result == spike_dir / "evidence" / "linux-x86_64" / "run-m3-followup-123"


# --------------------------------------------------------------------
# compute_workflow_url: only constructed when every part is present.
# --------------------------------------------------------------------


def test_compute_workflow_url_builds_the_real_actions_url() -> None:
    url = f.compute_workflow_url("https://github.com", "G-ChandraSekhar/codeagent", "34999999999")
    assert url == "https://github.com/G-ChandraSekhar/codeagent/actions/runs/34999999999"


@pytest.mark.parametrize(
    "server_url,repository,run_id",
    [
        (None, "G-ChandraSekhar/codeagent", "1"),
        ("https://github.com", None, "1"),
        ("https://github.com", "G-ChandraSekhar/codeagent", None),
        (None, None, None),
    ],
)
def test_compute_workflow_url_is_none_when_any_part_is_missing(server_url, repository, run_id) -> None:
    assert f.compute_workflow_url(server_url, repository, run_id) is None


# --------------------------------------------------------------------
# interpret_ancestor_check_returncode: 0 and 1 are both confirmed,
# legitimate answers; anything else is a technical failure of the
# check itself, never silently folded into "not an ancestor".
# --------------------------------------------------------------------


def test_interpret_ancestor_check_returncode_zero_is_confirmed_ancestor() -> None:
    assert f.interpret_ancestor_check_returncode(0, commit="abc123") is True


def test_interpret_ancestor_check_returncode_one_is_confirmed_not_ancestor() -> None:
    assert f.interpret_ancestor_check_returncode(1, commit="abc123") is False


@pytest.mark.parametrize("returncode", [2, 128, -1, 255])
def test_interpret_ancestor_check_returncode_other_codes_raise_as_technical_failure(
    returncode: int,
) -> None:
    with pytest.raises(RuntimeError, match="not a confirmed ancestor/not-ancestor answer"):
        f.interpret_ancestor_check_returncode(returncode, commit="abc123")


def test_interpret_ancestor_check_returncode_includes_stderr_in_the_failure_message() -> None:
    with pytest.raises(RuntimeError, match="unknown revision"):
        f.interpret_ancestor_check_returncode(
            128, commit="abc123", stderr="fatal: unknown revision or path not in the working tree"
        )


def test_interpret_ancestor_check_returncode_tolerates_missing_stderr() -> None:
    with pytest.raises(RuntimeError):
        f.interpret_ancestor_check_returncode(128, commit="abc123")


# --------------------------------------------------------------------
# validate_provenance: fails loudly, not merely records, on either
# provenance problem.
# --------------------------------------------------------------------


def test_validate_provenance_accepts_a_clean_ci_run() -> None:
    f.validate_provenance(
        harness_source_commit="abc123",
        github_sha="abc123",
        production_hardening_is_ancestor=True,
    )  # must not raise


def test_validate_provenance_accepts_a_clean_local_run_without_github_sha() -> None:
    f.validate_provenance(
        harness_source_commit="abc123",
        github_sha=None,
        production_hardening_is_ancestor=True,
    )  # must not raise -- no GITHUB_SHA to compare against on a local run


def test_validate_provenance_raises_when_hardening_commit_is_not_an_ancestor() -> None:
    with pytest.raises(RuntimeError, match="not an ancestor"):
        f.validate_provenance(
            harness_source_commit="abc123",
            github_sha=None,
            production_hardening_is_ancestor=False,
        )


def test_validate_provenance_raises_when_checkout_commit_differs_from_github_sha() -> None:
    with pytest.raises(RuntimeError, match="does not match"):
        f.validate_provenance(
            harness_source_commit="abc123",
            github_sha="def456",
            production_hardening_is_ancestor=True,
        )


def test_validate_provenance_ancestor_check_takes_precedence() -> None:
    """If both problems are present, the more fundamental one (the
    hardening commit itself missing) is the one reported."""
    with pytest.raises(RuntimeError, match="not an ancestor"):
        f.validate_provenance(
            harness_source_commit="abc123",
            github_sha="def456",
            production_hardening_is_ancestor=False,
        )
