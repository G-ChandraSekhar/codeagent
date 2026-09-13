"""
CodeAgent Stage 2 -- S4 post-hardening follow-up: validates the
Milestone 3 executor changes (commit 00063d445d9c25f957c0c6474701fb61d1217f67)
that were made *in response to* the original S4 spike's open questions.

Scope: macOS/Docker Desktop and Linux/x86_64 (each its own evidence
domain -- see docs/threat-model.md's A9). The same three checks run on
both platforms; nothing here claims cross-platform equivalence beyond
what each platform's own retained evidence actually shows. This is a
distinct, narrower run -- not a rerun of the original 14-check
spike_s4.py, and it never touches that script's own retained evidence.

What changed in production since the original S4 run, and what this
follow-up checks:
  1. `_SECURITY_FLAGS` gained `--memory-swap 512m` alongside the
     existing `--memory 512m`, closing the combined memory+swap gap
     the original spike found (Memory=512 MiB, MemorySwap=1024 MiB
     total -- ~512 MiB of extra swap headroom). This follow-up
     directly inspects the actual configured values post-fix.
  2. `DockerVerifier._attempt` now classifies a Docker-confirmed OOM
     kill (`State.OOMKilled == true`) as `ENVIRONMENT_FAILURE` with
     `ErrorCode.EXECUTOR_OOM_KILLED`, taking precedence over the
     ordinary nonzero-exit -> TEST_FAILURE rule, with a fixed sanitized
     message and the exit code preserved. The original spike could
     only infer this gap indirectly (a confirmed-OOM twin plus a
     separate DockerVerifier run that happened to also exit 137); this
     follow-up observes the real classification directly, through the
     actual public `DockerVerifier` API.

Evidence-only interception (kept entirely inside this file, never in
src/codeagent): to obtain the REAL `HostConfig.Memory`/`HostConfig.
MemorySwap` values for a container `DockerVerifier` itself created and
ran -- not merely a hand-built twin using the same imported flags --
this script temporarily monkeypatches `codeagent.executor._run_docker`
(the same module-level attribute the project's own unit tests patch)
to intercept the `rm --force <name>` call `DockerVerifier._cleanup`
issues, running `docker inspect` on that exact container immediately
before letting the real removal proceed. This is a read-only
observation squeezed into the last instant before a resource
`DockerVerifier` was always going to remove anyway; it changes no
decision `DockerVerifier` makes and is restored immediately afterward.
This is DIRECT observation of the real production code path's own
container, clearly distinguished below from the separate, ALSO direct
(but twin-based, not production-path) hand-controlled Class B
observation reusing spike_s4.py's own `_memory_experiment` machinery.

Result vocabulary, resource classing (Class A: real DockerVerifier,
self-cleaning; Class B: hand-rolled, tracked in a Manifest), and
exact-equality resource listing are all reused from spike_s4.py rather
than re-implemented -- see that module's own docstring for the
rationale. This file adds only what's specific to the post-hardening
follow-up.

Evidence is written under a fresh, run-specific subdirectory of the
EXISTING per-platform evidence directory (reusing spike_s4.py's own
`_platform_key()`, never a retyped copy):

    spikes/s4/evidence/macos-docker-desktop-<machine>/run-m3-followup-<timestamp>-<id>/
    spikes/s4/evidence/linux-x86_64/run-m3-followup-<github-run-id>-attempt-<github-run-attempt>/

never into either platform directory's own flat files (the original S4
run's evidence), which this script never opens for writing. On Linux,
the run directory is named after the actual GitHub Actions run id AND
run attempt (`GITHUB_RUN_ID`/`GITHUB_RUN_ATTEMPT`) rather than a
timestamp -- a re-run of the same workflow run (e.g. after a transient
failure) reuses the same run id but increments the attempt, and
without the attempt suffix a second attempt's evidence would silently
overwrite the first's. This extends, rather than replaces,
`spike_s4.py`'s own Linux evidence convention
(`spikes/s4/evidence/linux-x86_64/run-<run-id>/`). macOS keeps its
original timestamp+random-id naming unchanged, since there is no
run id/attempt to key off of on a manual local run.

Provenance, recorded in RUN_INFO.json and validated (fail loudly, not
merely recorded) before any check runs:
  - `production_hardening_commit`: the fixed Milestone 3 commit this
    follow-up exists to validate.
  - `harness_source_commit`: this checkout's actual `git rev-parse
    HEAD` -- independently computed, not merely echoed from an env var.
  - `production_hardening_is_ancestor`: whether
    `production_hardening_commit` is a real ancestor of
    `harness_source_commit`, determined by precisely interpreting `git
    merge-base --is-ancestor`'s exit code (0 = confirmed ancestor, 1 =
    confirmed NOT an ancestor, anything else = the check itself failed
    and raises rather than being silently folded into "not an
    ancestor") -- if not a confirmed ancestor, the hardening this
    follow-up exists to validate isn't even present in what's checked
    out, so the run refuses to proceed.
  - `workflow_run_id` / `workflow_run_attempt` / `workflow_url`:
    populated from the standard `GITHUB_RUN_ID`/`GITHUB_RUN_ATTEMPT`/
    `GITHUB_SERVER_URL`/`GITHUB_REPOSITORY` environment variables
    GitHub Actions always sets for a running job; `None` on a local
    (non-CI) run, e.g. macOS. When `GITHUB_RUN_ID` is set but
    `GITHUB_RUN_ATTEMPT` is missing or not a plain positive integer,
    the run refuses to proceed rather than construct an ambiguous
    evidence path.
  - When `GITHUB_SHA` is set (i.e. running inside Actions), it must
    equal `harness_source_commit` -- otherwise the checkout does not
    match what the workflow believes it triggered on, and the run
    refuses to proceed.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SPIKE_DIR.parents[1] / "src"
sys.path.insert(0, str(_SPIKE_DIR))
sys.path.insert(0, str(_SRC_DIR))

import spike_s4 as base  # noqa: E402 -- reuse harness primitives, never retype them

import codeagent.executor as executor_module  # noqa: E402
from codeagent import events  # noqa: E402
from codeagent.errors import ErrorCode  # noqa: E402
from codeagent.executor import (  # noqa: E402
    CONTAINER_NAME_PREFIX,
    DEFAULT_IMAGE,
    DockerVerifier,
    _SECURITY_FLAGS,
)

PASS, FAIL, INCONCLUSIVE, TECHNICAL_FAILURE = (
    base.PASS,
    base.FAIL,
    base.INCONCLUSIVE,
    base.TECHNICAL_FAILURE,
)

PRODUCTION_HARDENING_COMMIT = "00063d445d9c25f957c0c6474701fb61d1217f67"


def is_supported_platform(system: str, machine: str) -> bool:
    """macOS on any machine (unchanged from the original macOS-only
    pass) or Linux specifically on x86_64 (the only Linux evidence
    domain this project has ever tested -- see docs/threat-model.md's
    A9). Pulled out as a pure function so platform selection is unit
    testable without actually running on each platform."""
    if system == "Darwin":
        return True
    return system == "Linux" and machine == "x86_64"


# A canonical positive decimal integer: digits only, no sign, no
# leading zero (so "0", "007", "-1", "1.5", "1e3", and "" are all
# rejected) -- matches the shape GitHub Actions actually assigns to
# GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT (both start at 1, never 0).
_CANONICAL_POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")


def _is_canonical_positive_integer(value: str | None) -> bool:
    return value is not None and bool(_CANONICAL_POSITIVE_INTEGER_RE.fullmatch(value))


def compute_run_id(github_run_id: str | None, github_run_attempt: str | None = None) -> str:
    """Keys the run directory off the real GitHub Actions run id AND
    run attempt when running in that workflow (extending spike_s4.py's
    own Linux evidence convention, `run-<run-id>/`, with the attempt
    number) -- a workflow re-run reuses the same run id but increments
    the attempt, so the attempt must be part of the path or a second
    attempt's evidence would silently overwrite the first's. Falls
    back to the original timestamp+random-id naming for a local (e.g.
    macOS Docker Desktop) run, where no run id/attempt exists -- an
    absent `github_run_attempt` is simply ignored in that case, never
    treated as an error.

    Fails loudly rather than guessing whenever `github_run_id` is
    present but either field is not a canonical positive decimal
    integer (digits only, > 0 -- "0" is explicitly invalid, as is any
    malformed/empty value) -- that combination would otherwise
    silently construct an ambiguous or misleading Linux evidence run
    id. The diagnostic names exactly which field failed and why,
    rather than reporting both fields identically.
    """
    if github_run_id:
        if not _is_canonical_positive_integer(github_run_id):
            raise RuntimeError(
                f"GITHUB_RUN_ID is set but is not a canonical positive decimal "
                f"integer (digits only, greater than zero): {github_run_id!r} -- "
                "refusing to construct an ambiguous Linux evidence run id."
            )
        if not _is_canonical_positive_integer(github_run_attempt):
            raise RuntimeError(
                f"GITHUB_RUN_ID is set ({github_run_id!r}) but GITHUB_RUN_ATTEMPT is "
                f"not a canonical positive decimal integer (digits only, greater "
                f"than zero): {github_run_attempt!r} -- refusing to construct an "
                "ambiguous Linux evidence run id."
            )
        return f"run-m3-followup-{github_run_id}-attempt-{github_run_attempt}"
    return f"run-m3-followup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def compute_evidence_dir(spike_dir: Path, platform_key: str, run_id: str) -> Path:
    return spike_dir / "evidence" / platform_key / run_id


def compute_workflow_url(server_url: str | None, repository: str | None, run_id: str | None) -> str | None:
    if server_url and repository and run_id:
        return f"{server_url}/{repository}/actions/runs/{run_id}"
    return None


def interpret_ancestor_check_returncode(returncode: int, *, commit: str, stderr: str = "") -> bool:
    """Precisely interprets `git merge-base --is-ancestor <commit>
    HEAD`'s exit code: 0 and 1 are both legitimate, confirmed answers
    (ancestor / not an ancestor respectively) -- neither is an error.
    Any other exit code (e.g. a bad/unknown revision from a shallow
    clone, or an internal git failure) means the check itself did not
    produce a real answer at all, and must be reported as a technical
    provenance-check failure rather than silently folded into "not an
    ancestor", which would misrepresent a broken check as a confirmed
    negative result."""
    if returncode == 0:
        return True
    if returncode == 1:
        return False
    detail = f": {stderr.strip()}" if stderr and stderr.strip() else ""
    raise RuntimeError(
        f"'git merge-base --is-ancestor {commit} HEAD' failed with unexpected exit "
        f"code {returncode} (neither 0 nor 1) -- this is a provenance-check failure, "
        f"not a confirmed ancestor/not-ancestor answer{detail}"
    )


def validate_provenance(
    *,
    harness_source_commit: str,
    github_sha: str | None,
    production_hardening_is_ancestor: bool,
    production_hardening_commit: str = PRODUCTION_HARDENING_COMMIT,
) -> None:
    """Fails loudly (raises) rather than merely recording a problem,
    per two explicit requirements: the production hardening commit
    this follow-up exists to validate must actually be checked out,
    and -- when running inside GitHub Actions (GITHUB_SHA set) -- the
    real checkout must match what the workflow believes it triggered
    on. A pure function so this validation is unit testable without a
    real git repository or a real Actions environment."""
    if not production_hardening_is_ancestor:
        raise RuntimeError(
            f"production hardening commit {production_hardening_commit} is not an "
            f"ancestor of the checked-out HEAD ({harness_source_commit}) -- this "
            "follow-up exists to validate that commit's changes and refuses to "
            "proceed without them."
        )
    if github_sha is not None and harness_source_commit != github_sha:
        raise RuntimeError(
            f"checked-out commit ({harness_source_commit}) does not match "
            f"GITHUB_SHA ({github_sha}) -- refusing to proceed with mismatched "
            "provenance."
        )


_PLATFORM_KEY = base._platform_key()
_GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID")
_GITHUB_RUN_ATTEMPT = os.environ.get("GITHUB_RUN_ATTEMPT")
_RUN_ID = compute_run_id(_GITHUB_RUN_ID, _GITHUB_RUN_ATTEMPT)
EVIDENCE_DIR = compute_evidence_dir(base.SPIKE_DIR, _PLATFORM_KEY, _RUN_ID)

# The fixed, sanitized OOM message defined in executor.py's _attempt.
# Reproduced here as the CURRENTLY OBSERVED literal so this follow-up
# can assert real equality, not just "looks sanitized" -- if
# executor.py's wording ever changes, this constant (and this
# follow-up's expectation) would need updating alongside it. The
# property actually being validated -- fixed, sanitized, no raw
# inspect/daemon data -- is checked independently below regardless of
# exact wording.
_EXPECTED_OOM_MESSAGE = "verification container was killed for exceeding its memory limit"


def log(text: str = "") -> None:
    base.log(text)


def write(path: Path, content: str) -> None:
    base.write(path, content)


# --------------------------------------------------------------------
# Evidence-only interception: capture a real DockerVerifier container's
# own HostConfig immediately before its self-cleanup removes it.
# --------------------------------------------------------------------


def is_rm_force_call(args: tuple[str, ...]) -> str | None:
    """Returns the container name being removed if `args` is exactly a
    `rm --force <name>` invocation, else None. Pulled out as its own
    pure function so the interception's targeting logic can be unit
    tested without any real subprocess/Docker involvement."""
    if len(args) == 3 and args[0] == "rm" and args[1] == "--force":
        return args[2]
    return None


def make_intercepting_run_docker(original_run_docker, captured: dict[str, dict]):
    """Builds a wrapper around the real `_run_docker` that, only for a
    `rm --force <name>` call, first runs a real, read-only `docker
    inspect --format '{{json .HostConfig}}' <name>` and records the
    result into `captured[name]` -- then always calls through to the
    real `_run_docker` so removal proceeds exactly as it would
    unpatched. Never alters args, never suppresses or replaces the
    real call, never invoked for anything but `rm --force`."""

    def intercepting_run_docker(*args: str):
        name = is_rm_force_call(args)
        if name is not None:
            try:
                inspect = subprocess.run(
                    ["docker", "inspect", "--format", "{{json .HostConfig}}", name],
                    capture_output=True,
                    text=True,
                )
                if inspect.returncode == 0:
                    captured[name] = json.loads(inspect.stdout)
                else:
                    captured[name] = {"inspect_error": inspect.stderr.strip()}
            except Exception as exc:  # noqa: BLE001 -- record, never swallow silently
                captured[name] = {"inspect_exception": repr(exc)}
        return original_run_docker(*args)

    return intercepting_run_docker


def extract_single_captured_config(captured: dict[str, dict]) -> tuple[str, dict]:
    """Fails loudly rather than guessing if the interception captured
    zero or more than one container's HostConfig -- this follow-up's
    check assumes exactly one real DockerVerifier container ran while
    interception was active."""
    if len(captured) != 1:
        raise AssertionError(
            f"expected exactly one intercepted rm --force call, got {len(captured)}: "
            f"{sorted(captured)!r}"
        )
    (name, config), = captured.items()
    return name, config


def is_sanitized_message(message: str) -> bool:
    """A loose, defensive check that an OperationalError.message carries
    no raw Docker inspect/daemon payload -- no JSON braces, no
    "OOMKilled"/"HostConfig" field names, no embedded exit-code-shaped
    standalone integers. Not a substitute for reading the actual
    message (still recorded verbatim in evidence), just a mechanical
    guard against the sanitization property silently regressing."""
    if "{" in message or "}" in message:
        return False
    lowered = message.lower()
    for marker in ("oomkilled", "hostconfig", "exitcode", "state.", "docker inspect"):
        if marker in lowered:
            return False
    return True


# --------------------------------------------------------------------
# Check 1: memory/swap configuration -- two independent direct
# observations, never one fused into the other.
# --------------------------------------------------------------------


def check_memory_swap_configuration() -> dict:
    # Observation A: a hand-controlled Class B twin built from the
    # REAL imported _SECURITY_FLAGS (which now includes
    # --memory-swap 512m) via spike_s4.py's own, unmodified
    # _memory_experiment machinery -- inspected directly before
    # removal, tracked in spike_s4's MANIFEST. This is the same
    # mechanism the original spike used for its own memory_limit
    # check; reused here rather than reimplemented, now exercised
    # against the corrected production flag set.
    twin = base._memory_experiment(base.new_name("m3-followup-twin"), 200)

    # Observation B: the REAL DockerVerifier's own container,
    # inspected via the evidence-only interception above -- direct
    # observation of the actual production code path, not a twin.
    captured: dict[str, dict] = {}
    original_run_docker = executor_module._run_docker
    executor_module._run_docker = make_intercepting_run_docker(original_run_docker, captured)
    try:
        workdir = base.new_scratch_dir("m3-followup-hostconfig-workdir")
        verifier = DockerVerifier(workdir, command=("python3", "-B", "-c", "print('ok')"))
        result = verifier.run_baseline()
    finally:
        executor_module._run_docker = original_run_docker

    verifier_outcome_passed = result.outcome is events.VerificationOutcome.PASSED
    try:
        captured_name, captured_config = extract_single_captured_config(captured)
        capture_ok = True
        capture_error = None
    except AssertionError as exc:
        captured_name, captured_config = None, {}
        capture_ok = False
        capture_error = str(exc)

    real_verifier_memory = captured_config.get("Memory")
    real_verifier_memswap = captured_config.get("MemorySwap")

    expected_bytes = 512 * 1024 * 1024  # 512 MiB, as configured by "512m"

    twin_matches = (
        twin["configured_memory_bytes"] == str(expected_bytes)
        and twin["configured_memswap_bytes"] == str(expected_bytes)
    )
    real_verifier_matches = (
        capture_ok
        and real_verifier_memory == expected_bytes
        and real_verifier_memswap == expected_bytes
    )

    if not verifier_outcome_passed:
        classification = TECHNICAL_FAILURE
        notes = [
            f"the real DockerVerifier baseline used purely to observe its own "
            f"container's HostConfig did not PASS (outcome={result.outcome.value!r}) "
            "-- this is this follow-up's own setup problem, not a security finding."
        ]
    elif not capture_ok:
        classification = TECHNICAL_FAILURE
        notes = [f"interception did not capture exactly one HostConfig: {capture_error}"]
    elif twin_matches and real_verifier_matches:
        classification = PASS
        notes = [
            "both the hand-controlled twin (built from the real imported "
            "_SECURITY_FLAGS) and the real DockerVerifier's own container "
            "(observed via evidence-only interception of its self-cleanup call) "
            f"report Memory={expected_bytes} MemorySwap={expected_bytes} -- "
            "no additional swap beyond the configured 512 MiB memory limit, "
            "closing the gap the original S4 run found "
            "(Memory=536870912, MemorySwap=1073741824)."
        ]
    else:
        classification = FAIL
        notes = [
            "expected Memory == MemorySwap == 512 MiB on both the twin and the "
            "real DockerVerifier container; at least one did not match -- see "
            "recorded fields.",
        ]

    return {
        "check": "memory_swap_configuration",
        "expected_bytes": expected_bytes,
        "hand_controlled_twin": {
            "method": "spike_s4._memory_experiment against the real imported "
            "_SECURITY_FLAGS -- direct docker inspect of a Class B container, "
            "NOT the real DockerVerifier code path.",
            "name": twin["name"],
            "configured_memory_bytes": twin["configured_memory_bytes"],
            "configured_memswap_bytes": twin["configured_memswap_bytes"],
            "matches_expected": twin_matches,
        },
        "real_docker_verifier_container": {
            "method": "evidence-only interception of DockerVerifier._cleanup's "
            "own 'rm --force' call -- DIRECT observation of the real production "
            "code path's own container, not a twin/inference.",
            "captured_name": captured_name,
            "capture_ok": capture_ok,
            "capture_error": capture_error,
            "HostConfig.Memory": real_verifier_memory,
            "HostConfig.MemorySwap": real_verifier_memswap,
            "matches_expected": real_verifier_matches,
            "verifier_outcome": result.outcome.value,
        },
        "notes": notes,
        "classification": classification,
    }


# --------------------------------------------------------------------
# Check 2: a real, Docker-confirmed OOM kill through the actual public
# DockerVerifier API classifies as ENVIRONMENT_FAILURE / EXECUTOR_OOM_KILLED,
# never TEST_FAILURE, with the exit code preserved and a fixed,
# sanitized message.
# --------------------------------------------------------------------


def check_oom_classification() -> dict:
    workdir = base.new_scratch_dir("m3-followup-oom-workdir")
    verifier = DockerVerifier(
        workdir,
        command=("python3", "-B", "-c", base._MEMORY_SCRIPT_LARGE),
        timeout_seconds=20,
    )
    result = verifier.run(0)

    outcome_is_environment_failure = result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    error_present = result.error is not None
    error_code_is_oom = error_present and result.error.code is ErrorCode.EXECUTOR_OOM_KILLED
    exit_code_preserved_137 = result.exit_code == 137
    message = result.error.message if error_present else None
    message_sanitized = message is not None and is_sanitized_message(message)
    message_matches_expected_literal = message == _EXPECTED_OOM_MESSAGE
    not_test_failure = result.outcome is not events.VerificationOutcome.TEST_FAILURE

    all_good = (
        outcome_is_environment_failure
        and error_code_is_oom
        and exit_code_preserved_137
        and message_sanitized
        and not_test_failure
    )
    classification = PASS if all_good else FAIL

    notes = []
    if not message_matches_expected_literal:
        notes.append(
            "observed message differs from this follow-up's recorded expected "
            f"literal ({_EXPECTED_OOM_MESSAGE!r}); observed={message!r}. This "
            "does not by itself fail the check -- sanitization is verified "
            "independently -- but is recorded since a wording change in "
            "executor.py would otherwise go unnoticed here."
        )

    return {
        "check": "oom_classification",
        "verifier_outcome": result.outcome.value,
        "outcome_is_environment_failure": outcome_is_environment_failure,
        "error_present": error_present,
        "error_code": result.error.code.value if error_present else None,
        "error_code_is_oom": error_code_is_oom,
        "exit_code": result.exit_code,
        "exit_code_preserved_137": exit_code_preserved_137,
        "error_message": message,
        "message_sanitized": message_sanitized,
        "message_matches_expected_literal": message_matches_expected_literal,
        "not_test_failure": not_test_failure,
        "notes": notes,
        "classification": classification,
    }


# --------------------------------------------------------------------
# Check 3: negative control -- an ordinary nonzero-exit test (no OOM
# involved) through the same real DockerVerifier API must still
# classify as TEST_FAILURE with no operational error, proving the OOM
# classification above is not simply "any nonzero exit is now an
# error."
# --------------------------------------------------------------------


def check_negative_control_ordinary_test_failure() -> dict:
    workdir = base.new_scratch_dir("m3-followup-negctrl-workdir")
    verifier = DockerVerifier(
        workdir,
        command=("python3", "-B", "-c", "import sys; sys.exit(1)"),
        timeout_seconds=20,
    )
    result = verifier.run(0)

    outcome_is_test_failure = result.outcome is events.VerificationOutcome.TEST_FAILURE
    exit_code_is_1 = result.exit_code == 1
    error_is_none = result.error is None

    all_good = outcome_is_test_failure and exit_code_is_1 and error_is_none
    classification = PASS if all_good else FAIL

    return {
        "check": "negative_control_ordinary_test_failure",
        "verifier_outcome": result.outcome.value,
        "outcome_is_test_failure": outcome_is_test_failure,
        "exit_code": result.exit_code,
        "exit_code_is_1": exit_code_is_1,
        "error_is_none": error_is_none,
        "classification": classification,
    }


CHECKS: list[tuple[str, str]] = [
    ("memory_swap_configuration", "check_memory_swap_configuration"),
    ("oom_classification", "check_oom_classification"),
    ("negative_control_ordinary_test_failure", "check_negative_control_ordinary_test_failure"),
]


def main() -> None:
    system = platform.system()
    machine = platform.machine()
    if not is_supported_platform(system, machine):
        raise RuntimeError(
            "spike_s4_m3_followup.py supports macOS/Docker Desktop (any machine) "
            f"or Linux/x86_64 only -- refusing to run on system={system!r} "
            f"machine={machine!r}."
        )

    harness_source_commit = base.run(["git", "rev-parse", "HEAD"], cwd=base.SPIKE_DIR).stdout.strip()

    github_sha = os.environ.get("GITHUB_SHA")
    ancestor_check = base.run(
        ["git", "merge-base", "--is-ancestor", PRODUCTION_HARDENING_COMMIT, "HEAD"],
        cwd=base.SPIKE_DIR,
        check=False,
    )
    production_hardening_is_ancestor = interpret_ancestor_check_returncode(
        ancestor_check.returncode, commit=PRODUCTION_HARDENING_COMMIT, stderr=ancestor_check.stderr
    )
    validate_provenance(
        harness_source_commit=harness_source_commit,
        github_sha=github_sha,
        production_hardening_is_ancestor=production_hardening_is_ancestor,
    )

    docker_client_version = base.run(["docker", "version", "--format", "{{.Client.Version}}"]).stdout.strip()
    docker_server_version = base.run(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip()
    cgroup_version = base.run(["docker", "info", "--format", "{{.CgroupVersion}}"]).stdout.strip()
    if not docker_client_version or not docker_server_version or not cgroup_version:
        raise RuntimeError(
            "host metadata query returned an empty value "
            f"(docker_client_version={docker_client_version!r}, "
            f"docker_server_version={docker_server_version!r}, "
            f"cgroup_version={cgroup_version!r})"
        )

    base.run(["docker", "pull", DEFAULT_IMAGE])
    image_platform = base.run(
        ["docker", "inspect", "--format", "{{.Os}}/{{.Architecture}}", DEFAULT_IMAGE]
    ).stdout.strip()
    if system == "Linux" and image_platform != "linux/amd64":
        raise RuntimeError(
            f"expected the pinned verification image to report linux/amd64 on "
            f"Linux, got {image_platform!r}"
        )

    host_info = {
        "system": system,
        "release": platform.release(),
        "machine": machine,
        "python_version": platform.python_version(),
        "docker_client_version": docker_client_version,
        "docker_server_version": docker_server_version,
        "cgroup_version": cgroup_version,
        "image_platform": image_platform,
    }
    write(EVIDENCE_DIR / "host.json", json.dumps(host_info, indent=2))
    log("host: " + json.dumps(host_info))
    log(
        f"PLATFORM: {_PLATFORM_KEY} (threat-model.md A9 -- a separate evidence "
        f"domain from any other platform). Evidence directory: {EVIDENCE_DIR}"
    )

    github_run_id = _GITHUB_RUN_ID
    github_run_attempt = _GITHUB_RUN_ATTEMPT
    workflow_url = compute_workflow_url(
        os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY"), github_run_id
    )
    run_info = {
        "purpose": "S4 post-hardening follow-up: validate the Milestone 3 "
        "executor changes (--memory-swap, OOM classification) made in "
        "direct response to the original S4 spike's open questions.",
        "run_id": _RUN_ID,
        "production_hardening_commit": PRODUCTION_HARDENING_COMMIT,
        "harness_source_commit": harness_source_commit,
        "production_hardening_is_ancestor": production_hardening_is_ancestor,
        "workflow_run_id": github_run_id,
        "workflow_run_attempt": github_run_attempt,
        "workflow_url": workflow_url,
        "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write(EVIDENCE_DIR / "RUN_INFO.json", json.dumps(run_info, indent=2))
    log("run info: " + json.dumps(run_info))

    tested_config = {"image": DEFAULT_IMAGE, "security_flags": list(_SECURITY_FLAGS)}
    write(EVIDENCE_DIR / "tested_config.json", json.dumps(tested_config, indent=2))
    log("tested production config: " + json.dumps(tested_config))

    class_a_baseline_names = base.all_container_names()

    results: dict[str, dict] = {}
    try:
        namespace = globals()
        for key, func_name in CHECKS:
            log(f"\n--- CHECK: {key} ---")
            func = namespace[func_name]
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001 -- record, don't crash the whole run
                result = {"check": key, "classification": TECHNICAL_FAILURE, "error": repr(exc)}
                log(f"TECHNICAL_FAILURE in {key}: {exc!r}")
            results[key] = result
            write(EVIDENCE_DIR / f"check_{key}.json", json.dumps(result, indent=2, default=str))
    finally:
        # Class B: hand-controlled twin container(s) tracked in
        # spike_s4's own MANIFEST -- best-effort removal, then
        # independent re-verification of absence.
        class_b_cleanup = base.MANIFEST.cleanup_and_verify()

        # Class A: real DockerVerifier containers, self-cleaning --
        # never added to any manifest. Any name present now that
        # starts with CONTAINER_NAME_PREFIX and was not present at the
        # baseline snapshot is an unexpected leftover: a real finding
        # about production code, not absorbed into "all clean". A
        # failed listing itself (base.all_container_names raises via
        # base.run's check=True default) is never treated as
        # confirmed-clean -- it propagates as an exception instead.
        class_a_final_names = base.all_container_names()
        class_a_unexpected_delta = sorted(
            n
            for n in (class_a_final_names - class_a_baseline_names)
            if n.startswith(CONTAINER_NAME_PREFIX)
        )
        class_a_removed_by_extra_cleanup: dict[str, bool] = {}
        for name in class_a_unexpected_delta:
            base.run(["docker", "rm", "--force", name], check=False)
            class_a_removed_by_extra_cleanup[name] = base.container_absent(name)
        class_a_leftover_after_extra_cleanup = [
            n for n, removed in class_a_removed_by_extra_cleanup.items() if not removed
        ]
        class_a_clean = not class_a_unexpected_delta

        cleanup = {
            "class_a_self_cleanup": {
                "description": "Real DockerVerifier containers (memory_swap_"
                "configuration's HostConfig-observation run, oom_classification, "
                "negative_control_ordinary_test_failure) confirm their own "
                "removal internally -- never tracked in a manifest here, "
                "caught instead by this baseline/final container-name-prefix "
                "delta.",
                "baseline_names": sorted(class_a_baseline_names),
                "final_names": sorted(class_a_final_names),
                "unexpected_delta": class_a_unexpected_delta,
                "removed_by_extra_cleanup": class_a_removed_by_extra_cleanup,
                "leftover_after_extra_cleanup": class_a_leftover_after_extra_cleanup,
                "clean": class_a_clean,
            },
            "class_b_hand_controlled_cleanup": {
                "description": "The memory_swap_configuration check's hand-"
                "controlled twin container, tracked in spike_s4.MANIFEST and "
                "verified absent independently of Class A's prefix-delta check.",
                **class_b_cleanup,
            },
            "all_clean": class_a_clean and class_b_cleanup["all_clean"],
        }
        write(EVIDENCE_DIR / "cleanup.json", json.dumps(cleanup, indent=2))

    classifications = {key: r.get("classification") for key, r in results.items()}
    any_fail = any(c == FAIL for c in classifications.values())
    any_technical_failure = any(c == TECHNICAL_FAILURE for c in classifications.values())
    any_inconclusive = any(c == INCONCLUSIVE for c in classifications.values())

    if not cleanup["all_clean"]:
        overall = "FAIL"  # cleanup failure overrides a successful scenario claim
    elif any_fail or any_technical_failure:
        overall = "FAIL"
    elif any_inconclusive:
        overall = "PASS_WITH_OPEN_RISKS"
    else:
        overall = "PASS"

    summary = {
        "host": host_info,
        "run_info": run_info,
        "tested_image": DEFAULT_IMAGE,
        "tested_security_flags": list(_SECURITY_FLAGS),
        "classifications": classifications,
        "cleanup_all_clean": cleanup["all_clean"],
        "overall_verdict": overall,
    }
    write(EVIDENCE_DIR / "summary.json", json.dumps(summary, indent=2))
    log("\n\nSUMMARY:\n" + json.dumps(summary, indent=2))

    write(EVIDENCE_DIR / "run.log", "\n".join(base._LOG_LINES) + "\n")

    log(f"\nEvidence written to: {EVIDENCE_DIR}")
    sys.exit(base.exit_code_for_overall_verdict(overall))


if __name__ == "__main__":
    main()
