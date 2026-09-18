"""Milestone 1 slice C: DockerVerifier classification-logic tests.

These mock the `docker` CLI boundary (`executor._run_docker` and
`subprocess.Popen`) rather than requiring a real daemon, so the
failure paths that are hard or slow to trigger for real — a launch
OSError, a timeout, an unconfirmed cleanup — get exercised
deterministically. The happy paths (real PASSED/TEST_FAILURE against
a real container) are covered by tests/integration/test_slice_c.py
against real Docker, matching this project's existing split between
mocked unit tests and real integration tests (see
tests/unit/test_workspace.py vs tests/integration/test_slice_b.py).

`uuid4` is monkeypatched to a fixed value so the generated container
name is known ahead of time — needed to build `docker ps -a` listings
that do or don't contain it.
"""

from __future__ import annotations

import io
import json
import subprocess
import uuid

import pytest

from codeagent import events
from codeagent.errors import ErrorCode
from codeagent.executor import (
    CONTAINER_NAME_PREFIX,
    DEFAULT_IMAGE,
    _MAX_STREAM_BYTES,
    _SECURITY_FLAGS,
    DockerVerifier,
    _BoundedCollector,
    _DockerLaunchError,
    _run_docker,
)
from tests.support.fakes import SteppingClock

_FIXED_UUID = uuid.UUID(int=0)


def _container_name(label: str) -> str:
    return f"{CONTAINER_NAME_PREFIX}{label}-{_FIXED_UUID.hex[:12]}"


def _state_json(status: str = "exited", exit_code: int = 0, oom_killed: bool = False) -> str:
    """Builds the `docker inspect --format '{{json .State}}'` stdout the
    real Docker CLI would produce for a given final container state."""
    return json.dumps({"Status": status, "ExitCode": exit_code, "OOMKilled": oom_killed})


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _listing(names: list[str], returncode: int = 0) -> _FakeCompleted:
    return _FakeCompleted(returncode=returncode, stdout="\n".join(names))


class _FakeProc:
    """Stands in for subprocess.Popen(["docker", "start", "--attach", ...])."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        raise_timeout_first: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._raise_timeout_first = raise_timeout_first
        self._wait_calls = 0
        self.killed = False

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._raise_timeout_first and self._wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def _make_verifier(monkeypatch, tmp_path, *, docker_calls, popen_factory=None) -> DockerVerifier:
    """docker_calls: list of _FakeCompleted / exceptions to return, in the
    exact order `_run_docker` is invoked: create, [inspect], rm, ps -a."""
    calls = list(docker_calls)

    def fake_run_docker(*args: str):
        if not calls:
            raise AssertionError(f"unexpected extra docker call: {args}")
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)

    if popen_factory is not None:
        monkeypatch.setattr(
            "codeagent.executor.subprocess.Popen", lambda *a, **k: popen_factory()
        )

    return DockerVerifier(tmp_path, clock=SteppingClock())


# --------------------------------------------------------------------
# _BoundedCollector
# --------------------------------------------------------------------


def test_bounded_collector_truncates_across_multiple_feeds() -> None:
    collector = _BoundedCollector(limit=10)
    collector.feed(b"0123456789")
    collector.feed(b"more-that-should-be-dropped")
    text = collector.text()
    assert text.startswith("0123456789")
    assert text.endswith("...(truncated)")


def test_bounded_collector_does_not_mark_truncated_when_under_limit() -> None:
    collector = _BoundedCollector(limit=1024)
    collector.feed(b"hello")
    assert collector.text() == "hello"


def test_bounded_collector_decodes_invalid_utf8_safely() -> None:
    collector = _BoundedCollector(limit=1024)
    collector.feed(b"\xff\xfe")
    # Must not raise — replacement characters are fine.
    assert collector.text()


# --------------------------------------------------------------------
# _run_docker: OSError -> _DockerLaunchError
# --------------------------------------------------------------------


def test_run_docker_wraps_oserror(monkeypatch) -> None:
    def raise_oserror(*args, **kwargs):
        raise OSError("docker not found")

    monkeypatch.setattr("codeagent.executor.subprocess.run", raise_oserror)
    with pytest.raises(_DockerLaunchError):
        _run_docker("create")


# --------------------------------------------------------------------
# DockerVerifier.__init__ construction validation
# --------------------------------------------------------------------


def test_rejects_nonexistent_worktree_path(tmp_path) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path / "does-not-exist")


def test_rejects_non_directory_worktree_path(tmp_path) -> None:
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x")
    with pytest.raises(ValueError):
        DockerVerifier(a_file)


def test_rejects_empty_image(tmp_path) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, image="")


def test_rejects_non_digest_pinned_image(tmp_path) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, image="python:3.12-slim")


@pytest.mark.parametrize(
    "image",
    [
        # Too short — not a real 64-character sha256 digest.
        "python:3.12-slim@sha256:abc123",
        # Non-hex characters.
        "python:3.12-slim@sha256:" + "z" * 64,
        # Right length but with trailing garbage after the digest —
        # substring presence of "@sha256:" alone would have accepted
        # this.
        "python:3.12-slim@sha256:" + "a" * 64 + "-extra",
        # sha256: present as a substring elsewhere, not as the actual
        # digest separator.
        "python:3.12-slim@notsha256:" + "a" * 64,
    ],
)
def test_rejects_malformed_digest_shape(tmp_path, image: str) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, image=image)


def test_rejects_empty_command(tmp_path) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, command=())


def test_rejects_command_with_empty_element(tmp_path) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, command=("python3", ""))


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("inf"), float("nan")])
def test_rejects_invalid_timeout(tmp_path, timeout_seconds: float) -> None:
    with pytest.raises(ValueError):
        DockerVerifier(tmp_path, timeout_seconds=timeout_seconds)


def test_accepts_default_pinned_image(tmp_path) -> None:
    verifier = DockerVerifier(tmp_path)
    assert verifier.command == ("python3", "-B", "-m", "unittest", "tests.test_worker")
    assert "@sha256:" in DEFAULT_IMAGE


def test_worktree_path_is_resolved_to_absolute(tmp_path) -> None:
    verifier = DockerVerifier(str(tmp_path), clock=SteppingClock())
    assert verifier._worktree_path == str(tmp_path.resolve())


# --------------------------------------------------------------------
# _cleanup(): confirmation via `docker ps -a`, exact-name matching
# --------------------------------------------------------------------


def test_cleanup_confirms_absence_from_listing(monkeypatch, tmp_path) -> None:
    calls = [
        _FakeCompleted(returncode=0),  # rm
        _listing(["some-other-container"]),  # confirmed absent
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x") is True


def test_cleanup_detects_container_still_present(monkeypatch, tmp_path) -> None:
    calls = [
        _FakeCompleted(returncode=0),  # rm
        _listing(["codeagent-verify-x", "some-other-container"]),  # still present
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x") is False


def test_cleanup_treats_nonzero_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _FakeCompleted(returncode=0),  # rm
        _listing([], returncode=1),  # listing itself failed
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x") is False


def test_cleanup_treats_launch_oserror_on_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _FakeCompleted(returncode=0),  # rm succeeds
        _DockerLaunchError("docker vanished"),  # ps -a fails to launch
    ]

    def fake_run_docker(*args: str):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x") is False


def test_cleanup_exact_name_match_not_fooled_by_similar_names(monkeypatch, tmp_path) -> None:
    calls = [
        _FakeCompleted(returncode=0),  # rm
        _listing(["codeagent-verify-x-extra", "xcodeagent-verify-x"]),  # similar, not equal
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x") is True


# --------------------------------------------------------------------
# DockerVerifier._execute classification paths
# --------------------------------------------------------------------


def test_create_launch_failure_with_confirmed_cleanup_is_command_start_failure(
    monkeypatch, tmp_path
) -> None:
    """`create_attempted` is set immediately before the create call, so a
    create-launch failure still requires cleanup confirmation (ADR 0003
    Amendment 2's structured cleanup-status correction) — this test
    confirms that when cleanup succeeds (nothing was ever created, and
    the listing genuinely confirms it), the outcome is the original
    COMMAND_START_FAILURE with cleanup_status=CONFIRMED_ABSENT, not
    NOT_APPLICABLE (which is legal only for a failure strictly before
    the create invocation, which this is not)."""
    calls = [
        _FakeCompleted(returncode=0),  # rm (cleanup, no-op — nothing exists)
        _listing([]),  # confirmed absent
    ]

    def fake_run_docker(*args: str):
        if args and args[0] == "create":
            raise _DockerLaunchError("docker executable missing")
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.COMMAND_START_FAILURE
    assert result.exit_code is None
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert result.error is not None
    assert result.error.code == ErrorCode.EXECUTOR_COMMAND_START_FAILED


def test_create_launch_failure_with_unconfirmable_cleanup_is_environment_failure(
    monkeypatch, tmp_path
) -> None:
    """When the docker executable is unavailable for every call
    (create *and* the cleanup rm/ps-a calls), cleanup cannot be
    confirmed — the outcome is overridden to ENVIRONMENT_FAILURE with
    cleanup_status=UNCONFIRMED, never silently reported as a
    COMMAND_START_FAILURE that would imply nothing needed checking."""

    def raise_launch_error(*args, **kwargs):
        raise _DockerLaunchError("docker executable missing")

    monkeypatch.setattr("codeagent.executor._run_docker", raise_launch_error)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED
    assert result.error is not None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_create_nonzero_exit_is_environment_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=1, stderr="no such image"),  # create
            _FakeCompleted(returncode=0),  # rm (cleanup)
            _listing([]),  # confirmed absent
        ],
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_create_nonzero_exit_with_unconfirmed_cleanup_overrides_to_unconfirmed(
    monkeypatch, tmp_path
) -> None:
    name = _container_name("baseline")
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=1, stderr="no such image"),  # create
            _FakeCompleted(returncode=0),  # rm (cleanup)
            _listing([name]),  # still present: cleanup cannot be confirmed
        ],
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED


def test_timeout_while_running_is_timeout_outcome(monkeypatch, tmp_path) -> None:
    name = _container_name("baseline")
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0),  # rm (cleanup)
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(raise_timeout_first=True),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.TIMEOUT
    assert result.error.code == ErrorCode.EXECUTOR_TIMEOUT
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT


# --------------------------------------------------------------------
# ContainerCleanupStatus derivation (Milestone 2 slice 2B-2)
# --------------------------------------------------------------------


def test_cleanup_status_for_not_applicable_when_create_never_attempted() -> None:
    """NOT_APPLICABLE is reserved for a failure strictly before the
    create invocation begins — no path in `_attempt` produces this
    today (nothing validates anything before issuing `docker create`),
    so this maps the pure classification function directly rather than
    trying to provoke it through `_attempt`/`_execute`."""
    status = DockerVerifier._cleanup_status_for(create_attempted=False, confirmed_absent=True)
    assert status is events.ContainerCleanupStatus.NOT_APPLICABLE
    # confirmed_absent is irrelevant once create was never attempted.
    status = DockerVerifier._cleanup_status_for(create_attempted=False, confirmed_absent=False)
    assert status is events.ContainerCleanupStatus.NOT_APPLICABLE


def test_cleanup_status_for_confirmed_absent() -> None:
    status = DockerVerifier._cleanup_status_for(create_attempted=True, confirmed_absent=True)
    assert status is events.ContainerCleanupStatus.CONFIRMED_ABSENT


def test_cleanup_status_for_unconfirmed() -> None:
    status = DockerVerifier._cleanup_status_for(create_attempted=True, confirmed_absent=False)
    assert status is events.ContainerCleanupStatus.UNCONFIRMED


def test_unparseable_inspect_output_is_environment_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout="not-json-at-all"),  # inspect
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_inspect_command_nonzero_exit_is_environment_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=1, stderr="no such container"),  # inspect fails
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


@pytest.mark.parametrize(
    "raw_state",
    [
        json.dumps({"ExitCode": 0, "OOMKilled": False}),  # missing Status
        json.dumps({"Status": "exited", "OOMKilled": False}),  # missing ExitCode
        json.dumps({"Status": "exited", "ExitCode": 0}),  # missing OOMKilled
        json.dumps({"Status": 1, "ExitCode": 0, "OOMKilled": False}),  # Status wrong type
        json.dumps({"Status": "exited", "ExitCode": "0", "OOMKilled": False}),  # ExitCode wrong type
        json.dumps({"Status": "exited", "ExitCode": False, "OOMKilled": False}),  # ExitCode is bool
        json.dumps({"Status": "exited", "ExitCode": 0, "OOMKilled": "false"}),  # OOMKilled wrong type
        json.dumps(["exited", 0, False]),  # not a dict at all
    ],
)
def test_malformed_or_wrong_typed_inspect_state_is_environment_failure(
    monkeypatch, tmp_path, raw_state: str
) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=raw_state),  # inspect
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_exited_zero_is_passed(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=0, oom_killed=False)),
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(stdout=b"ok\n"),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.PASSED
    assert result.exit_code == 0
    assert result.error is None
    assert "ok" in result.stdout
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT


def test_exited_nonzero_is_test_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=1, oom_killed=False)),
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(stderr=b"AssertionError\n"),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.TEST_FAILURE
    assert result.exit_code == 1
    assert result.error is None


def test_confirmed_oom_kill_is_environment_failure_with_oom_error_code(
    monkeypatch, tmp_path
) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=137, oom_killed=True)),
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code == 137
    assert result.error is not None
    assert result.error.code == ErrorCode.EXECUTOR_OOM_KILLED
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    # Only a fixed, sanitized message is persisted -- never raw Docker
    # inspect payloads or daemon output.
    assert "OOMKilled" not in result.error.message
    assert "{" not in result.error.message


def test_oom_classification_takes_precedence_over_would_be_passed(monkeypatch, tmp_path) -> None:
    """An OOM-killed container that happens to report exit code 0 must
    still be classified as the OOM environment failure, never PASSED —
    the exit code of a killed container cannot be trusted as a real
    test result."""
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=0, oom_killed=True)),
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_OOM_KILLED


def test_oom_classification_takes_precedence_over_would_be_test_failure(
    monkeypatch, tmp_path
) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=1, oom_killed=True)),
            _FakeCompleted(returncode=0),  # rm
            _listing(["unrelated"]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_OOM_KILLED


def test_unconfirmed_cleanup_overrides_an_oom_result(monkeypatch, tmp_path) -> None:
    """Cleanup confirmation remains authoritative over every provisional
    result, including a would-be OOM classification: if the container
    still shows up in `docker ps -a` afterward, the final outcome must
    be the generic unconfirmed-cleanup ENVIRONMENT_FAILURE, not the OOM
    one."""
    name = _container_name("baseline")
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=137, oom_killed=True)),
            _FakeCompleted(returncode=0),  # rm
            _listing([name]),  # STILL PRESENT
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


# --------------------------------------------------------------------
# _SECURITY_FLAGS: --memory / --memory-swap
# --------------------------------------------------------------------


def test_security_flags_tuple_pins_memory_swap_immediately_beside_memory() -> None:
    """Sanity check on the _SECURITY_FLAGS tuple itself. This proves the
    tuple's own shape, not that a real `docker create` invocation
    actually receives it — see
    test_docker_create_argv_includes_memory_and_memory_swap_flags below
    for that."""
    assert "--memory" in _SECURITY_FLAGS
    memory_index = _SECURITY_FLAGS.index("--memory")
    assert _SECURITY_FLAGS[memory_index : memory_index + 4] == (
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
    )


def test_docker_create_argv_includes_memory_and_memory_swap_flags(monkeypatch, tmp_path) -> None:
    """Proves the flags actually cross the Docker command boundary: the
    real argv passed to `_run_docker` for the `create` subcommand
    (captured here, not just read back off _SECURITY_FLAGS) must
    contain `--memory 512m --memory-swap 512m` in that exact order."""
    captured_create_argv: list[str] = []
    calls = [
        _FakeCompleted(returncode=0),  # create
        _FakeCompleted(returncode=0, stdout=_state_json(exit_code=0, oom_killed=False)),
        _FakeCompleted(returncode=0),  # rm
        _listing(["unrelated"]),  # confirmed gone
    ]

    def fake_run_docker(*args: str):
        if args and args[0] == "create":
            captured_create_argv.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())

    verifier = DockerVerifier(tmp_path, clock=SteppingClock())
    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.PASSED
    joined = " ".join(captured_create_argv)
    assert "--memory 512m --memory-swap 512m" in joined


def test_unconfirmed_cleanup_overrides_a_would_be_passed_result(monkeypatch, tmp_path) -> None:
    """A cleanup attempt is not a cleanup guarantee: even though the
    container genuinely exited 0, if `docker ps -a` still lists it
    afterward, the outcome must be ENVIRONMENT_FAILURE, never PASSED —
    never report a successful run without confirmed cleanup."""
    name = _container_name("baseline")
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=0, oom_killed=False)),
            _FakeCompleted(returncode=0),  # rm
            _listing([name]),  # STILL PRESENT
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_start_launch_failure_with_unconfirmed_cleanup_is_environment_failure(
    monkeypatch, tmp_path
) -> None:
    """Combined case (correction pass item 2): the container may exist
    (create succeeded) but starting it failed to even launch, AND
    cleanup afterward can't confirm removal. The final outcome must be
    ENVIRONMENT_FAILURE — the unconfirmed-cleanup override applies
    regardless of which provisional outcome preceded it, including
    COMMAND_START_FAILURE."""
    name = _container_name("baseline")

    def popen_raises(*args, **kwargs):
        raise OSError("docker start failed to launch")

    calls = [
        _FakeCompleted(returncode=0),  # create succeeds -> created=True
        _FakeCompleted(returncode=0),  # rm (best effort)
        _listing([name]),  # still present -> unconfirmed
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", popen_raises)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)

    verifier = DockerVerifier(tmp_path, clock=SteppingClock())
    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_cleanup_launch_failure_does_not_crash_and_is_unconfirmed(monkeypatch, tmp_path) -> None:
    """If even `docker ps -a` for the confirmation check can't be
    launched, that must not raise out of _execute — it must be treated
    as cleanup-unconfirmed, i.e. ENVIRONMENT_FAILURE."""
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _FakeCompleted(returncode=0),  # create
            _FakeCompleted(returncode=0, stdout=_state_json(exit_code=0, oom_killed=False)),
            _FakeCompleted(returncode=0),  # rm (best effort, succeeds)
            _DockerLaunchError("docker vanished"),  # confirm listing fails to launch
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_start_launch_failure_is_command_start_failure(monkeypatch, tmp_path) -> None:
    name = _container_name("baseline")

    def popen_raises(*args, **kwargs):
        raise OSError("docker start failed to launch")

    calls = [
        _FakeCompleted(returncode=0),  # create succeeds
        _FakeCompleted(returncode=0),  # rm (cleanup, created=True since create succeeded)
        _listing(["unrelated"]),  # confirmed gone
    ]

    def fake_run_docker(*args: str):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", popen_raises)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)

    verifier = DockerVerifier(tmp_path, clock=SteppingClock())
    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.COMMAND_START_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_COMMAND_START_FAILED
