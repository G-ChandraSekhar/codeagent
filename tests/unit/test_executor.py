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
that do or don't contain it. Milestone 3 Slice 3B-4 adds a deterministic
fake full container ID (`_FIXED_CONTAINER_ID`) used by every "create
succeeded" fake from that slice onward, since a successful create must
now also produce a strictly valid ID before start/inspect/rm ever run.
`_run_docker`'s own fakes now return `codeagent._bounded_subprocess.
BoundedProcessResult` (bytes stdout), matching its real Slice 3B-4
return type.
"""

from __future__ import annotations

import io
import json
import subprocess
import uuid

import pytest

from codeagent import events
from codeagent._bounded_subprocess import BoundedProcessResult
from codeagent.errors import ErrorCode
from codeagent.executor import (
    CONTAINER_NAME_PREFIX,
    DEFAULT_IMAGE,
    _MAX_STREAM_BYTES,
    _SECURITY_FLAGS,
    DockerVerifier,
    _BoundedCollector,
    _DockerControlPlaneFailure,
    _DockerLaunchError,
    _parse_cleanup_listing,
    _parse_create_id,
    _run_docker,
)
from tests.support.fakes import SteppingClock

_FIXED_UUID = uuid.UUID(int=0)
_FIXED_CONTAINER_ID = "a" * 64
_OTHER_CONTAINER_ID = "b" * 64


def _container_name(label: str) -> str:
    return f"{CONTAINER_NAME_PREFIX}{label}-{_FIXED_UUID.hex[:12]}"


def _state_json(status: str = "exited", exit_code: int = 0, oom_killed: bool = False) -> str:
    """Builds the `docker inspect --format '{{json .State}}'` stdout the
    real Docker CLI would produce for a given final container state."""
    return json.dumps({"Status": status, "ExitCode": exit_code, "OOMKilled": oom_killed})


def _result(returncode: int, stdout: str | bytes = "") -> BoundedProcessResult:
    """Builds the `BoundedProcessResult` `_run_docker`'s real Slice
    3B-4 implementation returns — `stdout` may be given as `str` for
    convenience (UTF-8 encoded here) or already as `bytes`."""
    raw = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    return BoundedProcessResult(returncode=returncode, stdout=raw)


def _create_success(container_id: str = _FIXED_CONTAINER_ID) -> BoundedProcessResult:
    """A successful `docker create`'s exact real stdout shape: the full
    64-hex container ID plus exactly one trailing LF."""
    return _result(0, f"{container_id}\n")


def _listing(pairs: list[tuple[str, str]], returncode: int = 0) -> BoundedProcessResult:
    """Builds the `docker ps -a --no-trunc --format '{{.ID}}\\t{{.Names}}'`
    stdout the real Docker CLI would produce for a given set of
    (id, name) rows."""
    return _result(returncode, "\n".join(f"{cid}\t{name}" for cid, name in pairs))


def _name_only_listing(names: list[str], returncode: int = 0) -> BoundedProcessResult:
    """A listing whose rows' IDs are irrelevant to the test (each paired
    with a distinct, deterministic dummy ID so multiple names never
    collide on the same ID) — used by tests scoped to name-only
    confirmation (no valid created ID exists yet)."""
    return _listing(
        [(format(0xF00 + i, "064x"), name) for i, name in enumerate(names)], returncode=returncode
    )


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
    """docker_calls: list of BoundedProcessResult / exceptions to return,
    in the exact order `_run_docker` is invoked: create, [inspect], rm,
    ps -a. `fake_run_docker` accepts and ignores the real `limit`/
    `timeout` keyword arguments Slice 3B-4 added to `_run_docker`."""
    calls = list(docker_calls)

    def fake_run_docker(*args: str, **kwargs):
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
# _run_docker (Slice 3B-4): bounded, per-command, wraps BoundedProcessError
# --------------------------------------------------------------------


def test_run_docker_wraps_launch_failure(monkeypatch) -> None:
    def raise_oserror(*args, **kwargs):
        raise OSError("docker not found")

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", raise_oserror)
    with pytest.raises(_DockerLaunchError):
        _run_docker("create", limit=65)


def test_run_docker_wraps_timeout_as_control_plane_failure(monkeypatch) -> None:
    """Uses a real, slow-but-harmless subprocess (not a real `docker`
    call) to prove `_run_docker` maps a genuine timeout to
    `_DockerControlPlaneFailure`, distinct from `_DockerLaunchError`.
    Captures the *real* `Popen` before patching, since the patched
    attribute lives on the one shared `subprocess` module object."""
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        "codeagent._bounded_subprocess.subprocess.Popen",
        lambda *a, **k: real_popen(
            ["python3", "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ),
    )
    with pytest.raises(_DockerControlPlaneFailure):
        _run_docker("ps", "-a", limit=1024, timeout=0.2)


# --------------------------------------------------------------------
# _parse_create_id (Slice 3B-4): strict full-container-ID grammar
# --------------------------------------------------------------------


def test_parse_create_id_accepts_exact_valid_shape() -> None:
    assert _parse_create_id((_FIXED_CONTAINER_ID + "\n").encode()) == _FIXED_CONTAINER_ID


@pytest.mark.parametrize(
    "raw",
    [
        b"",  # empty output
        _FIXED_CONTAINER_ID.encode(),  # missing newline
        (_FIXED_CONTAINER_ID + "\n" + _FIXED_CONTAINER_ID + "\n").encode(),  # multiple lines
        (_FIXED_CONTAINER_ID + "\r\n").encode(),  # CRLF
        (_FIXED_CONTAINER_ID.upper() + "\n").encode(),  # uppercase
        b"abc123\n",  # abbreviated ID
        ("g" * 64 + "\n").encode(),  # non-hex characters
        (_FIXED_CONTAINER_ID[:-1] + "\x00\n").encode(),  # embedded NUL
        (" " + _FIXED_CONTAINER_ID + "\n").encode(),  # leading whitespace
        (_FIXED_CONTAINER_ID + " \n").encode(),  # trailing whitespace before newline
        (_FIXED_CONTAINER_ID + "\nextra").encode(),  # trailing data, no final newline
        ("a" * 63 + "\n").encode(),  # one short
        ("a" * 65 + "\n").encode(),  # one long
    ],
)
def test_parse_create_id_rejects_every_malformed_shape(raw: bytes) -> None:
    assert _parse_create_id(raw) is None


def test_parse_create_id_rejects_non_ascii_bytes() -> None:
    assert _parse_create_id(b"\xff" * 64 + b"\n") is None


# --------------------------------------------------------------------
# _parse_cleanup_listing (Slice 3B-4): strict dual-identity grammar
# --------------------------------------------------------------------


def test_parse_cleanup_listing_accepts_well_formed_rows() -> None:
    raw = f"{_FIXED_CONTAINER_ID}\tcodeagent-verify-x\n{_OTHER_CONTAINER_ID}\tunrelated\n".encode()
    ids, names = _parse_cleanup_listing(raw)
    assert ids == frozenset({_FIXED_CONTAINER_ID, _OTHER_CONTAINER_ID})
    assert names == frozenset({"codeagent-verify-x", "unrelated"})


def test_parse_cleanup_listing_accepts_empty_listing() -> None:
    assert _parse_cleanup_listing(b"") == (frozenset(), frozenset())


def test_parse_cleanup_listing_rejects_invalid_utf8() -> None:
    assert _parse_cleanup_listing(b"\xff\xfe\tname\n") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"no-tab-separator\n",  # not exactly two fields
        f"{_FIXED_CONTAINER_ID}\tname\textra\n".encode(),  # too many fields
        b"abc123\tname\n",  # abbreviated ID
        (_FIXED_CONTAINER_ID.upper() + "\tname\n").encode(),  # uppercase ID
        f"{_FIXED_CONTAINER_ID}\t\n".encode(),  # empty name
    ],
)
def test_parse_cleanup_listing_rejects_malformed_rows(raw: bytes) -> None:
    assert _parse_cleanup_listing(raw) is None


def test_parse_cleanup_listing_rejects_duplicate_id() -> None:
    raw = f"{_FIXED_CONTAINER_ID}\tname-one\n{_FIXED_CONTAINER_ID}\tname-two\n".encode()
    assert _parse_cleanup_listing(raw) is None


def test_parse_cleanup_listing_rejects_duplicate_name() -> None:
    raw = f"{_FIXED_CONTAINER_ID}\tsame-name\n{_OTHER_CONTAINER_ID}\tsame-name\n".encode()
    assert _parse_cleanup_listing(raw) is None


# --------------------------------------------------------------------
# Correction pass, finding 7: strict Docker container-name grammar
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        _container_name("baseline"),  # a real generated CodeAgent name
        "codeagent-verification-" + "a" * 32,  # a deterministic-style name
        "some_unrelated.container-1",  # a valid unrelated Docker-style name
        "A",  # single uppercase alnum character
        "9start-with-digit",
    ],
)
def test_parse_cleanup_listing_accepts_every_valid_name_shape(name: str) -> None:
    raw = f"{_FIXED_CONTAINER_ID}\t{name}\n".encode()
    parsed = _parse_cleanup_listing(raw)
    assert parsed is not None
    assert name in parsed[1]


@pytest.mark.parametrize(
    "name_bytes",
    [
        b" leading-space",
        b"trailing-space ",
        b"embedded\rcr",
        b"embedded\x00nul",
        b"unicode-\xc3\xa9",  # "unicode-é"
        b"-leading-hyphen",
        b".leading-period",
        b"_leading-underscore",
        b"embedded@sign",
        b"embedded!bang",
        b"embedded$dollar",
        b"embedded/slash",
        b"embedded space",
    ],
)
def test_parse_cleanup_listing_rejects_every_invalid_name_shape(name_bytes: bytes) -> None:
    raw = _FIXED_CONTAINER_ID.encode() + b"\t" + name_bytes + b"\n"
    assert _parse_cleanup_listing(raw) is None


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
# _cleanup(): confirmation via one dual-identity `docker ps -a` listing
# --------------------------------------------------------------------


def test_cleanup_confirms_absence_from_listing_no_id(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _name_only_listing(["some-other-container"]),  # confirmed absent
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is True


def test_cleanup_detects_container_still_present_by_name(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _name_only_listing(["codeagent-verify-x", "some-other-container"]),  # still present
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is False


def test_cleanup_treats_nonzero_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _name_only_listing([], returncode=1),  # listing itself failed
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is False


def test_cleanup_treats_launch_failure_on_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm succeeds
        _DockerLaunchError("docker vanished"),  # ps -a fails to launch
    ]

    def fake_run_docker(*args: str, **kwargs):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is False


def test_cleanup_treats_control_plane_failure_on_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    """A timeout/overflow/monitoring failure on the confirmation listing
    itself (Slice 3B-4's new failure category) must be treated exactly
    like a launch failure or nonzero exit: unconfirmed, never crashing
    out of `_cleanup`."""
    calls = [
        _result(0),  # rm succeeds
        _DockerControlPlaneFailure("listing timed out"),
    ]

    def fake_run_docker(*args: str, **kwargs):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is False


def test_cleanup_rm_control_plane_failure_still_reaches_listing(monkeypatch, tmp_path) -> None:
    """rm's own outcome is never authoritative: a timeout/overflow on
    the rm call itself must not skip the independent confirmation
    listing."""
    calls = [
        _DockerControlPlaneFailure("rm timed out"),
        _name_only_listing([]),  # confirmed absent regardless
    ]

    def fake_run_docker(*args: str, **kwargs):
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is True


def test_cleanup_exact_name_match_not_fooled_by_similar_names(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _name_only_listing(["codeagent-verify-x-extra", "xcodeagent-verify-x"]),  # similar, not equal
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is True


def test_cleanup_rejects_malformed_listing_as_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _result(0, "not\ttab\tformatted\tcorrectly\n"),  # malformed row
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", None) is False


# --------------------------------------------------------------------
# _cleanup(): dual-identity (name + validated ID) confirmation
# --------------------------------------------------------------------


def test_cleanup_with_id_both_absent_is_confirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm (targets container_id)
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # both absent
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", _FIXED_CONTAINER_ID) is True


def test_cleanup_with_id_name_absent_but_id_present_under_another_name_is_unconfirmed(
    monkeypatch, tmp_path
) -> None:
    calls = [
        _result(0),  # rm
        _listing([(_FIXED_CONTAINER_ID, "renamed-container")]),  # name gone, ID reappears
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", _FIXED_CONTAINER_ID) is False


def test_cleanup_with_id_id_absent_but_name_present_is_unconfirmed(monkeypatch, tmp_path) -> None:
    calls = [
        _result(0),  # rm
        _listing([(_OTHER_CONTAINER_ID, "codeagent-verify-x")]),  # ID gone, name occupied
    ]

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    assert verifier._cleanup("codeagent-verify-x", _FIXED_CONTAINER_ID) is False


def test_cleanup_targets_removal_by_id_when_available(monkeypatch, tmp_path) -> None:
    """Once a valid ID exists, `docker rm --force` must target that
    immutable ID, never the mutable generated name."""
    captured_rm_argv: list[str] = []
    calls = [
        _result(0),  # rm
        _listing([]),
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "rm":
            captured_rm_argv.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    verifier._cleanup("codeagent-verify-x", _FIXED_CONTAINER_ID)

    assert captured_rm_argv == ["rm", "--force", _FIXED_CONTAINER_ID]


def test_cleanup_targets_removal_by_name_when_no_id_available(monkeypatch, tmp_path) -> None:
    captured_rm_argv: list[str] = []
    calls = [
        _result(0),  # rm
        _name_only_listing([]),
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "rm":
            captured_rm_argv.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    verifier._cleanup("codeagent-verify-x", None)

    assert captured_rm_argv == ["rm", "--force", "codeagent-verify-x"]


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
        _result(0),  # rm (cleanup, no-op — nothing exists)
        _name_only_listing([]),  # confirmed absent
    ]

    def fake_run_docker(*args: str, **kwargs):
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


def test_create_control_plane_failure_is_environment_failure(monkeypatch, tmp_path) -> None:
    """A timeout/overflow/monitoring failure on `docker create` itself
    (Slice 3B-4's new failure category) is ENVIRONMENT_FAILURE, not
    COMMAND_START_FAILURE — the executable did launch."""
    calls = [
        _result(0),  # rm (cleanup)
        _name_only_listing([]),  # confirmed absent
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "create":
            raise _DockerControlPlaneFailure("create timed out")
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_create_nonzero_exit_is_environment_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _result(1, "no such image"),  # create
            _result(0),  # rm (cleanup)
            _name_only_listing([]),  # confirmed absent
        ],
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_create_nonzero_exit_output_is_never_trusted_as_id(monkeypatch, tmp_path) -> None:
    """Even if a nonzero create's stdout happens to look exactly like a
    valid container ID, it must never be trusted or used to target
    cleanup by ID — cleanup falls back to the exact generated name."""
    captured_rm_argv: list[str] = []
    calls = [
        _create_success(),  # create: returncode overridden below to 1
        _result(0),  # rm
        _name_only_listing([]),
    ]
    # Force the "create" fake to report a nonzero exit while still
    # carrying a validly-shaped ID in stdout.
    calls[0] = BoundedProcessResult(returncode=1, stdout=calls[0].stdout)

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "rm":
            captured_rm_argv.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert captured_rm_argv[-1] == _container_name("baseline")


def test_create_success_with_malformed_id_prevents_start_and_cleans_up_by_name(
    monkeypatch, tmp_path
) -> None:
    """A zero-exit create whose stdout does not strictly parse as a
    valid container ID must never start the container, and cleanup
    must still target the exact generated name (a real container may
    exist even though its reported identity is untrusted)."""
    start_called = False

    def popen_should_not_be_called(*a, **k):
        nonlocal start_called
        start_called = True
        raise AssertionError("docker start must not be called after a malformed create ID")

    captured_rm_argv: list[str] = []
    calls = [
        _result(0, "not-a-valid-id\n"),  # create: zero exit, malformed ID
        _result(0),  # rm
        _name_only_listing([]),
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "rm":
            captured_rm_argv.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", popen_should_not_be_called)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert not start_called
    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE
    assert captured_rm_argv[-1] == _container_name("baseline")


def test_valid_id_targets_start_inspect_and_remove(monkeypatch, tmp_path) -> None:
    """Once create succeeds with a strictly valid ID, `docker start
    --attach`, the final-state `docker inspect`, and `docker rm
    --force` must all target that exact ID, never the mutable name."""
    captured_start_argv: list[str] = []
    captured_inspect_argv: list[str] = []
    captured_rm_argv: list[str] = []
    calls = [
        _create_success(),  # create
        _result(0, _state_json(exit_code=0, oom_killed=False)),  # inspect
        _result(0),  # rm
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "inspect":
            captured_inspect_argv.extend(args)
        if args and args[0] == "rm":
            captured_rm_argv.extend(args)
        return calls.pop(0)

    def popen_factory(*a, **k):
        captured_start_argv.extend(a[0])
        return _FakeProc()

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", popen_factory)
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.PASSED
    assert captured_start_argv == ["docker", "start", "--attach", _FIXED_CONTAINER_ID]
    assert captured_inspect_argv == ["inspect", "--format", "{{json .State}}", _FIXED_CONTAINER_ID]
    assert captured_rm_argv == ["rm", "--force", _FIXED_CONTAINER_ID]


def test_create_nonzero_exit_with_unconfirmed_cleanup_overrides_to_unconfirmed(
    monkeypatch, tmp_path
) -> None:
    name = _container_name("baseline")
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _result(1, "no such image"),  # create
            _result(0),  # rm (cleanup)
            _name_only_listing([name]),  # still present: cleanup cannot be confirmed
        ],
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED


def test_timeout_while_running_is_timeout_outcome(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _create_success(),  # create
            _result(0),  # rm (cleanup)
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone (name-wise)
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
            _create_success(),  # create
            _result(0, "not-json-at-all"),  # inspect
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(1, "no such container"),  # inspect fails
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_inspect_control_plane_failure_is_environment_failure(monkeypatch, tmp_path) -> None:
    """A timeout/overflow/monitoring failure on the inspect call itself
    (Slice 3B-4's new failure category) is folded into the same
    malformed-inspection ENVIRONMENT_FAILURE path as a launch failure
    or a nonzero exit — see `_start_and_inspect`'s own docstring."""
    calls = [
        _create_success(),  # create
        _result(0),  # rm
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "inspect":
            raise _DockerControlPlaneFailure("inspect timed out")
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())
    verifier = DockerVerifier(tmp_path, clock=SteppingClock())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.exit_code is None
    assert result.error.code == ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE


def test_inspect_invalid_utf8_output_is_environment_failure(monkeypatch, tmp_path) -> None:
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _create_success(),  # create
            _result(0, b"\xff\xfe not utf-8"),  # inspect
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, raw_state),  # inspect
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=0, oom_killed=False)),
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=1, oom_killed=False)),
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=137, oom_killed=True)),
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=0, oom_killed=True)),
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=1, oom_killed=True)),
            _result(0),  # rm
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=137, oom_killed=True)),
            _result(0),  # rm
            _listing([(_FIXED_CONTAINER_ID, name)]),  # STILL PRESENT (by ID and name)
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
        _create_success(),  # create
        _result(0, _state_json(exit_code=0, oom_killed=False)),
        _result(0),  # rm
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
    ]

    def fake_run_docker(*args: str, **kwargs):
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
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _create_success(),  # create
            _result(0, _state_json(exit_code=0, oom_killed=False)),
            _result(0),  # rm
            _listing([(_FIXED_CONTAINER_ID, "still-here")]),  # STILL PRESENT (by ID)
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

    def popen_raises(*args, **kwargs):
        raise OSError("docker start failed to launch")

    calls = [
        _create_success(),  # create succeeds -> created=True, container_id captured
        _result(0),  # rm (best effort, targets container_id)
        _listing([(_FIXED_CONTAINER_ID, "still-here")]),  # still present -> unconfirmed
    ]

    def fake_run_docker(*args: str, **kwargs):
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
            _create_success(),  # create
            _result(0, _state_json(exit_code=0, oom_killed=False)),
            _result(0),  # rm (best effort, succeeds)
            _DockerLaunchError("docker vanished"),  # confirm listing fails to launch
        ],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_start_launch_failure_is_command_start_failure(monkeypatch, tmp_path) -> None:
    calls = [
        _create_success(),  # create succeeds, container_id captured
        _result(0),  # rm (cleanup, created=True since create succeeded)
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),  # confirmed gone
    ]

    def popen_raises(*args, **kwargs):
        raise OSError("docker start failed to launch")

    def fake_run_docker(*args: str, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", popen_raises)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)

    verifier = DockerVerifier(tmp_path, clock=SteppingClock())
    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.COMMAND_START_FAILURE
    assert result.error.code == ErrorCode.EXECUTOR_COMMAND_START_FAILED
