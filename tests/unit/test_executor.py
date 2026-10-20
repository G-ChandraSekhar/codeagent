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
import signal
import subprocess
import time
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
    """Stands in for subprocess.Popen(["docker", "start", "--attach", ...]).

    `raise_timeout_first` models a client that keeps running until it is
    killed: `poll()` is None and every `wait(timeout)` times out until
    `kill()` (D3: the attach wait is sliced, so a single early timeout no
    longer means the budget expired). Deliberately has no `pid`: a test that
    reaches the group-kill path must patch `os.killpg`, so a fake can never
    signal a real process group."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        raise_timeout_first: bool = False,
        pid: int | None = None,
    ) -> None:
        if pid is not None:
            self.pid = pid
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._raise_timeout_first = raise_timeout_first
        self._wait_calls = 0
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        return None if self._raise_timeout_first else 0

    def wait(self, timeout=None):
        self._wait_calls += 1
        if self._raise_timeout_first and not self.killed:
            time.sleep(min(timeout, 0.01) if timeout is not None else 0)
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)
        return self.poll()

    def kill(self) -> None:
        self.killed = True


# Never a real process: every test using it patches `os.killpg`.
_FAKE_PID = 4_000_123


def _record_killpg(monkeypatch, *, raises: BaseException | None = None) -> list:
    calls: list = []

    def killpg(pid, sig):
        calls.append((pid, sig))
        if raises is not None:
            raise raises

    monkeypatch.setattr("codeagent.executor.os.killpg", killpg)
    return calls


def _make_verifier(
    monkeypatch, tmp_path, *, docker_calls, popen_factory=None, timeout_seconds=None, cancellation=None
) -> DockerVerifier:
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

    kwargs = {}
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    if cancellation is not None:
        kwargs["cancellation"] = cancellation
    return DockerVerifier(tmp_path, clock=SteppingClock(), **kwargs)


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
        popen_factory=lambda: _FakeProc(raise_timeout_first=True, pid=_FAKE_PID),
        timeout_seconds=0.2,
    )
    killpg_calls = _record_killpg(monkeypatch, raises=ProcessLookupError())

    result = verifier.run_baseline()

    assert result.outcome == events.VerificationOutcome.TIMEOUT
    assert result.error.code == ErrorCode.EXECUTOR_TIMEOUT
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    # D3: the timeout path kills the client's process group first; the
    # direct kill is the fallback when the group kill raises.
    assert killpg_calls == [(_FAKE_PID, signal.SIGKILL)]


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


# ---------------------------------------------------------------------------
# Milestone 3 Slice 3B-6: lifecycle-aware container naming/labeling/
# publication (ADR 0004 Amendment 6). Mocks the Docker CLI boundary
# exactly like the rest of this file; the real end-to-end path
# (real prepare_lifecycle() lease + real writer + real
# LifecycleContainerPublisher + real Docker, plus the three real
# SIGKILL boundary scenarios) lives in
# tests/integration/test_slice_3b6.py.
# ---------------------------------------------------------------------------

from codeagent import container_lifecycle as cl
from codeagent import _docker_ownership as docker_ownership_module
from codeagent.executor import DockerVerifierLifecycleContext, _CleanupDisposition

_LIFECYCLE_ID = "a" * 32
_STATE_ROOT_ID = "b" * 32


# Mirrors `lifecycle_store._CONTAINER_TRANSITION_EDGES` (the real
# live-owner edge table) so `_FakeContainerPublisher` below can catch an
# illegal-edge bug locally, without a real writer -- the exact class of
# bug a correction pass found (a fall-through from a fully successful
# occupied-name recovery straight to publishing `PRESENT`, skipping the
# required `ABSENT->CREATING` re-publish first).
_FAKE_PUBLISHER_EDGES = frozenset(
    {
        (cl.ContainerIntent.ABSENT, cl.ContainerIntent.CREATING),
        (cl.ContainerIntent.CREATING, cl.ContainerIntent.PRESENT),
        (cl.ContainerIntent.CREATING, cl.ContainerIntent.ABSENT),
        (cl.ContainerIntent.PRESENT, cl.ContainerIntent.REMOVING),
        (cl.ContainerIntent.REMOVING, cl.ContainerIntent.ABSENT),
    }
)


class _FakeContainerPublisher:
    """Records every call in order and can be told to raise a
    `ContainerPublicationError` exactly once for a given (role, intent)
    pair — enough to exercise every write-ahead stop-on-failure branch
    without a real lifecycle store.

    Enforces `_FAKE_PUBLISHER_EDGES` per role (an exact no-op is also
    permitted, matching the real writer's own no-op short-circuit) —
    an illegal transition raises `AssertionError` immediately, the same
    class of bug a real writer would refuse via `ILLEGAL_TRANSITION`.

    Exposes `lifecycle_id`/`state_root_id` as read-only properties
    (Slice 3B-6 correction pass), matching the real
    `container_lifecycle.ContainerTransitionPublisher` contract exactly
    — this is the *only* identity `DockerVerifierLifecycleContext`
    trusts; there is no separate, independently suppliable identity for
    it to disagree with."""

    def __init__(self, *, lifecycle_id: str = _LIFECYCLE_ID, state_root_id: str = _STATE_ROOT_ID) -> None:
        self._lifecycle_id = lifecycle_id
        self._state_root_id = state_root_id
        self.calls: list[tuple] = []
        self._fail_once: dict[tuple, Exception] = {}
        self._state: dict = {cl.ContainerRole.BASELINE: cl.ContainerIntent.ABSENT, cl.ContainerRole.VERIFICATION: cl.ContainerIntent.ABSENT}

    @property
    def lifecycle_id(self) -> str:
        return self._lifecycle_id

    @property
    def state_root_id(self) -> str:
        return self._state_root_id

    def fail_once(self, role, intent, exc: Exception) -> None:
        self._fail_once[(role, intent)] = exc

    def publish(self, *, role, intent, id):
        self.calls.append((role, intent, id))
        key = (role, intent)
        if key in self._fail_once:
            raise self._fail_once.pop(key)
        current = self._state[role]
        if intent != current and (current, intent) not in _FAKE_PUBLISHER_EDGES:
            raise AssertionError(f"illegal fake-publisher transition for {role}: {current} -> {intent}")
        self._state[role] = intent


def _pub_error(reason=cl.ContainerPublicationFailure.ILLEGAL_TRANSITION) -> cl.ContainerPublicationError:
    return cl.ContainerPublicationError(reason, "forced")


def _lifecycle_ctx(publisher: _FakeContainerPublisher | None = None) -> DockerVerifierLifecycleContext:
    return DockerVerifierLifecycleContext(publisher=publisher or _FakeContainerPublisher())


def _baseline_name() -> str:
    return cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=_LIFECYCLE_ID)


def _make_lifecycle_verifier(
    monkeypatch,
    tmp_path,
    *,
    publisher: _FakeContainerPublisher,
    docker_calls,
    ps_results=(),
    inspect_results=(),
    popen_factory=None,
) -> DockerVerifier:
    """`docker_calls`: fed to `_run_docker` in call order (create,
    [inspect-state], [rm], ...), exactly like `_make_verifier`.
    `ps_results`/`inspect_results`: fed to
    `docker_ownership.docker_ps_all_id_name_pairs`/`docker_inspect_ownership`
    in call order — each entry is either a return value or an
    exception instance to raise."""
    calls = list(docker_calls)

    def fake_run_docker(*args: str, **kwargs):
        if not calls:
            raise AssertionError(f"unexpected extra docker call: {args}")
        outcome = calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    ps_queue = list(ps_results)
    inspect_queue = list(inspect_results)

    def fake_ps(**kwargs):
        if not ps_queue:
            raise AssertionError("unexpected extra docker_ps_all_id_name_pairs call")
        outcome = ps_queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fake_inspect(candidate_id, **kwargs):
        if not inspect_queue:
            raise AssertionError("unexpected extra docker_inspect_ownership call")
        outcome = inspect_queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", fake_ps)
    monkeypatch.setattr(docker_ownership_module, "docker_inspect_ownership", fake_inspect)

    if popen_factory is not None:
        monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: popen_factory())

    return DockerVerifier(tmp_path, clock=SteppingClock(), lifecycle_context=_lifecycle_ctx(publisher))


def test_omitted_lifecycle_context_behavior_is_unchanged(tmp_path):
    """`lifecycle_context` defaults to `None` — every one of this
    file's other 113 tests exercises exactly that legacy path,
    unmodified; this is a direct construction-time sanity check that
    the parameter is genuinely optional."""
    verifier = DockerVerifier(tmp_path)
    assert verifier._lifecycle_context is None


def test_lifecycle_context_rejects_malformed_lifecycle_id():
    with pytest.raises(ValueError):
        DockerVerifierLifecycleContext(publisher=_FakeContainerPublisher(lifecycle_id="not-hex32"))


def test_lifecycle_context_rejects_malformed_state_root_id():
    with pytest.raises(ValueError):
        DockerVerifierLifecycleContext(publisher=_FakeContainerPublisher(state_root_id="ZZ"))


def test_lifecycle_context_derives_identity_from_publisher_alone():
    """There is no second, independently suppliable identity to
    disagree with the publisher's own — `lifecycle_id`/`state_root_id`
    are always read straight from it."""
    publisher = _FakeContainerPublisher(lifecycle_id="1" * 32, state_root_id="2" * 32)
    ctx = DockerVerifierLifecycleContext(publisher=publisher)
    assert ctx.lifecycle_id == "1" * 32
    assert ctx.state_root_id == "2" * 32
    assert ctx.lifecycle_id == publisher.lifecycle_id
    assert ctx.state_root_id == publisher.state_root_id


def test_no_docker_operation_occurs_for_a_mismatched_configuration(monkeypatch):
    """A malformed publisher identity fails at
    `DockerVerifierLifecycleContext` construction, strictly before a
    `DockerVerifier` bound to it could ever exist — no Docker listing,
    inspect, create, start, or removal call is reachable."""

    def _fail(*args, **kwargs):
        raise AssertionError(f"no Docker operation may occur for a mismatched configuration: {args}")

    monkeypatch.setattr("codeagent.executor._run_docker", _fail)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", _fail)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", _fail)
    monkeypatch.setattr(docker_ownership_module, "docker_inspect_ownership", _fail)

    with pytest.raises(ValueError):
        DockerVerifierLifecycleContext(publisher=_FakeContainerPublisher(lifecycle_id="not-hex32"))


def test_names_and_labels_derive_from_the_same_publisher_identity(monkeypatch, tmp_path):
    """Uses a distinctive, non-default lifecycle_id/state_root_id to
    prove the real create-time name and labels are threaded through
    from the publisher's own identity, not from some other hardcoded
    or duplicated value."""
    distinct_lifecycle_id = "3" * 32
    distinct_state_root_id = "4" * 32
    publisher = _FakeContainerPublisher(lifecycle_id=distinct_lifecycle_id, state_root_id=distinct_state_root_id)
    ctx = DockerVerifierLifecycleContext(publisher=publisher)

    create_argv: list[str] = []
    real_calls = [_create_success(), _result(0, _state_json(status="exited", exit_code=0)), _result(0)]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "create":
            create_argv.extend(args)
        outcome = real_calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", lambda **k: ({}, {}))
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())

    verifier = DockerVerifier(tmp_path, clock=SteppingClock(), lifecycle_context=ctx)
    result = verifier.run_baseline()

    assert result.outcome is events.VerificationOutcome.PASSED
    expected_name = cl.deterministic_container_name(
        role=cl.ContainerRole.BASELINE, lifecycle_id=distinct_lifecycle_id
    )
    assert "--name" in create_argv
    assert create_argv[create_argv.index("--name") + 1] == expected_name
    assert f"{cl.CONTAINER_LABEL_STATE_ROOT_ID}={distinct_state_root_id}" in create_argv
    assert f"{cl.CONTAINER_LABEL_ID}={distinct_lifecycle_id}" in create_argv


def test_creating_publication_failure_is_not_applicable(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    publisher.fail_once(cl.ContainerRole.BASELINE, cl.ContainerIntent.CREATING, _pub_error())
    verifier = _make_lifecycle_verifier(monkeypatch, tmp_path, publisher=publisher, docker_calls=[])

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.NOT_APPLICABLE
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert publisher.calls == [(cl.ContainerRole.BASELINE, cl.ContainerIntent.CREATING, None)]


def test_pre_create_listing_failure_is_not_applicable(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch, tmp_path, publisher=publisher, docker_calls=[],
        ps_results=[docker_ownership_module.DockerListingError("boom")],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.NOT_APPLICABLE
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_pre_create_occupied_name_inspect_failure_is_not_applicable(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch, tmp_path, publisher=publisher, docker_calls=[],
        ps_results=[({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()})],
        inspect_results=[docker_ownership_module.DockerInspectError("boom")],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.NOT_APPLICABLE
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    # No mutation of any kind is authorized on an unproven candidate.
    assert not any(c[1] is cl.ContainerIntent.REMOVING for c in publisher.calls)


def test_pre_create_foreign_occupant_is_not_applicable_and_never_removed(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    foreign_proof = docker_ownership_module.InspectOwnership(
        id=_OTHER_CONTAINER_ID, name=_baseline_name(), labels={}
    )
    verifier = _make_lifecycle_verifier(
        monkeypatch, tmp_path, publisher=publisher, docker_calls=[],
        ps_results=[({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()})],
        inspect_results=[foreign_proof],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.NOT_APPLICABLE
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert not any(c[1] is cl.ContainerIntent.REMOVING for c in publisher.calls)


def test_occupied_owned_recovery_full_success_falls_through_to_create(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    owned_proof = docker_ownership_module.InspectOwnership(
        id=_OTHER_CONTAINER_ID,
        name=_baseline_name(),
        labels=cl.required_labels(
            state_root_id=_STATE_ROOT_ID, lifecycle_id=_LIFECYCLE_ID, role=cl.ContainerRole.BASELINE
        ),
    )
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[
            _result(0),  # rm(OTHER_CONTAINER_ID) -- recovery
            _create_success(),  # this invocation's own create
            _result(0, _state_json(status="exited", exit_code=0)),  # inspect state
            _result(0),  # rm(FIXED_CONTAINER_ID) -- this invocation's own final cleanup
        ],
        ps_results=[
            ({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()}),  # occupied check
            ({}, {}),  # recovery cleanup confirm: absent
            ({}, {}),  # this invocation's own final cleanup confirm: absent
        ],
        inspect_results=[owned_proof],
        popen_factory=lambda: _FakeProc(),
    )

    result = verifier.run_baseline()

    assert result.outcome is events.VerificationOutcome.PASSED
    intents = [c[1] for c in publisher.calls]
    assert intents == [
        cl.ContainerIntent.CREATING,
        cl.ContainerIntent.PRESENT,  # recovery: candidate marked present
        cl.ContainerIntent.REMOVING,  # recovery: candidate marked removing
        cl.ContainerIntent.ABSENT,  # recovery: confirmed absent
        cl.ContainerIntent.CREATING,  # re-published: no direct ABSENT->PRESENT edge exists
        cl.ContainerIntent.PRESENT,  # this invocation's own container
        cl.ContainerIntent.REMOVING,  # this invocation's own container, before cleanup
        cl.ContainerIntent.ABSENT,  # this invocation's own final cleanup
    ]


def test_occupied_owned_recovery_removal_unconfirmed_is_unconfirmed(monkeypatch, tmp_path):
    """create_attempted is False for this invocation, yet the outcome
    is still UNCONFIRMED -- proof that disposition, not
    create_attempted, drives the public cleanup status."""
    publisher = _FakeContainerPublisher()
    owned_proof = docker_ownership_module.InspectOwnership(
        id=_OTHER_CONTAINER_ID,
        name=_baseline_name(),
        labels=cl.required_labels(
            state_root_id=_STATE_ROOT_ID, lifecycle_id=_LIFECYCLE_ID, role=cl.ContainerRole.BASELINE
        ),
    )
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[_result(0)],  # rm(OTHER_CONTAINER_ID) -- still present afterward
        ps_results=[
            ({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()}),  # occupied
            ({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()}),  # still present
        ],
        inspect_results=[owned_proof],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_post_create_confirmed_absence_even_when_projection_remains_creating(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[_DockerControlPlaneFailure("create failed")],
        ps_results=[
            ({}, {}),  # pre-create occupied-name check: free
            ({}, {}),  # post-uncertain-create observation: still free
        ],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    # The lifecycle projection is never collapsed to ABSENT by this
    # invocation for this row -- only the initial CREATING publish ever
    # happened.
    assert publisher.calls == [(cl.ContainerRole.BASELINE, cl.ContainerIntent.CREATING, None)]


def test_post_create_foreign_conflicting_candidate_is_unconfirmed(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    foreign_proof = docker_ownership_module.InspectOwnership(
        id=_OTHER_CONTAINER_ID, name=_baseline_name(), labels={}
    )
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[_DockerControlPlaneFailure("create failed")],
        ps_results=[
            ({}, {}),  # pre-create occupied-name check: free
            ({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()}),
        ],
        inspect_results=[foreign_proof],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_post_create_unobservable_state_is_unconfirmed(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[_DockerControlPlaneFailure("create failed")],
        ps_results=[
            ({}, {}),  # pre-create occupied-name check: free
            docker_ownership_module.DockerListingError("boom"),
        ],
    )

    result = verifier.run_baseline()

    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE


def test_no_post_create_branch_ever_returns_not_applicable(monkeypatch, tmp_path):
    """Every uncertain-create-recovery branch above (confirmed absent,
    foreign/conflicting, unobservable) resolves to CONFIRMED_ABSENT or
    UNCONFIRMED -- never NOT_APPLICABLE, since this invocation's own
    `docker create` was genuinely entered even though it failed."""
    for ps_results, inspect_results, expected in [
        ([({}, {})], [], events.ContainerCleanupStatus.CONFIRMED_ABSENT),
        (
            [({_baseline_name(): _OTHER_CONTAINER_ID}, {_OTHER_CONTAINER_ID: _baseline_name()})],
            [docker_ownership_module.InspectOwnership(id=_OTHER_CONTAINER_ID, name=_baseline_name(), labels={})],
            events.ContainerCleanupStatus.UNCONFIRMED,
        ),
        ([docker_ownership_module.DockerListingError("boom")], [], events.ContainerCleanupStatus.UNCONFIRMED),
    ]:
        publisher = _FakeContainerPublisher()
        verifier = _make_lifecycle_verifier(
            monkeypatch,
            tmp_path,
            publisher=publisher,
            docker_calls=[_DockerControlPlaneFailure("create failed")],
            ps_results=[({}, {}), *ps_results],
            inspect_results=inspect_results,
        )
        result = verifier.run_baseline()
        assert result.cleanup_status is not events.ContainerCleanupStatus.NOT_APPLICABLE
        assert result.cleanup_status is expected


def test_attempt_result_preserves_exit_code_stdout_and_stderr(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[
            _create_success(),
            _result(0, _state_json(status="exited", exit_code=7)),
            _result(0),  # rm
        ],
        ps_results=[({}, {}), ({}, {})],
        popen_factory=lambda: _FakeProc(stdout=b"hello-stdout", stderr=b"hello-stderr"),
    )

    result = verifier.run_baseline()

    assert result.outcome is events.VerificationOutcome.TEST_FAILURE
    assert result.exit_code == 7
    assert result.stdout == "hello-stdout"
    assert result.stderr == "hello-stderr"


def test_lifecycle_aware_execution_never_calls_legacy_cleanup(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[
            _create_success(),
            _result(0, _state_json(status="exited", exit_code=0)),
            _result(0),  # rm
        ],
        ps_results=[({}, {}), ({}, {})],
        popen_factory=lambda: _FakeProc(),
    )

    def _boom(self, name, container_id):
        raise AssertionError("legacy _cleanup must never be called from the lifecycle-aware path")

    monkeypatch.setattr(DockerVerifier, "_cleanup", _boom)

    result = verifier.run_baseline()
    assert result.outcome is events.VerificationOutcome.PASSED


def test_lifecycle_aware_execution_never_issues_docker_rm_by_name(monkeypatch, tmp_path):
    publisher = _FakeContainerPublisher()
    rm_targets: list[str] = []
    real_calls = [
        _create_success(),
        _result(0, _state_json(status="exited", exit_code=0)),
        _result(0),  # rm
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "rm":
            rm_targets.append(args[2])
        outcome = real_calls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", lambda **k: ({}, {}))
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())

    verifier = DockerVerifier(tmp_path, clock=SteppingClock(), lifecycle_context=_lifecycle_ctx(publisher))
    result = verifier.run_baseline()

    assert result.outcome is events.VerificationOutcome.PASSED
    assert rm_targets == [_FIXED_CONTAINER_ID]
    assert _baseline_name() not in rm_targets


def test_removing_id_none_is_unreachable(monkeypatch, tmp_path):
    """Every `(REMOVING, id)` call this file's own scenarios produce
    always carries a non-None id -- a spy-publisher exhaustive check
    across the happy path and the occupied-recovery path."""
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[
            _create_success(),
            _result(0, _state_json(status="exited", exit_code=0)),
            _result(0),  # rm
        ],
        ps_results=[({}, {}), ({}, {})],
        popen_factory=lambda: _FakeProc(),
    )
    verifier.run_baseline()

    removing_calls = [c for c in publisher.calls if c[1] is cl.ContainerIntent.REMOVING]
    assert removing_calls
    assert all(c[2] is not None for c in removing_calls)


# --------------------------------------------------------------------
# ADR 0004 Amendment 15: the container runs as the effective host
# identity, never a fixed 1000:1000, and fails closed otherwise.
# --------------------------------------------------------------------

import os  # noqa: E402

from codeagent.executor import _effective_container_user  # noqa: E402

_FAKE_EUID, _FAKE_EGID = 4242, 4343


def _fake_effective_identity(monkeypatch, uid=_FAKE_EUID, gid=_FAKE_EGID):
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    monkeypatch.setattr(os, "getegid", lambda: gid)


def test_security_flags_carry_no_fixed_user_and_are_otherwise_unchanged() -> None:
    """Every pre-Amendment-15 isolation flag is present, byte-identical and
    in order; only the fixed `--user 1000:1000` pair was removed."""
    assert _SECURITY_FLAGS == (
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m",
        "--memory", "512m",
        "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "128",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    )
    assert "--user" not in _SECURITY_FLAGS and "1000:1000" not in _SECURITY_FLAGS


def test_effective_container_user_is_the_real_effective_identity() -> None:
    assert _effective_container_user() == f"{os.geteuid()}:{os.getegid()}"


def _assert_create_argv_uses_effective_identity(argv: list[str]) -> None:
    assert "1000:1000" not in argv
    assert argv.count("--user") == 1
    assert argv[argv.index("--user") + 1] == f"{_FAKE_EUID}:{_FAKE_EGID}"
    start = argv.index(_SECURITY_FLAGS[0])
    assert tuple(argv[start : start + len(_SECURITY_FLAGS)]) == _SECURITY_FLAGS


def test_legacy_create_argv_passes_the_effective_identity(monkeypatch, tmp_path) -> None:
    _fake_effective_identity(monkeypatch)
    captured: list[str] = []
    calls = [
        _create_success(),
        _result(0, _state_json(exit_code=0, oom_killed=False)),
        _result(0),
        _listing([(_OTHER_CONTAINER_ID, "unrelated")]),
    ]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "create":
            captured.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr("codeagent.executor.uuid4", lambda: _FIXED_UUID)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())

    result = DockerVerifier(tmp_path, clock=SteppingClock()).run_baseline()
    assert result.outcome == events.VerificationOutcome.PASSED
    _assert_create_argv_uses_effective_identity(captured)


def test_lifecycle_aware_create_argv_passes_the_effective_identity(monkeypatch, tmp_path) -> None:
    _fake_effective_identity(monkeypatch)
    captured: list[str] = []
    calls = [_create_success(), _result(0, _state_json(status="exited", exit_code=0)), _result(0)]

    def fake_run_docker(*args: str, **kwargs):
        if args and args[0] == "create":
            captured.extend(args)
        return calls.pop(0)

    monkeypatch.setattr("codeagent.executor._run_docker", fake_run_docker)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", lambda **k: ({}, {}))
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", lambda *a, **k: _FakeProc())

    ctx = DockerVerifierLifecycleContext(publisher=_FakeContainerPublisher())
    result = DockerVerifier(tmp_path, clock=SteppingClock(), lifecycle_context=ctx).run_baseline()
    assert result.outcome is events.VerificationOutcome.PASSED
    _assert_create_argv_uses_effective_identity(captured)


@pytest.mark.parametrize("missing", ["geteuid", "getegid"])
def test_unsupported_platform_fails_closed_without_fallback(monkeypatch, tmp_path, missing) -> None:
    """No POSIX effective IDs: construction is refused before any Docker
    call — never a silent fallback to 1000:1000."""
    monkeypatch.delattr(os, missing)
    monkeypatch.setattr("codeagent.executor._run_docker", lambda *a, **k: pytest.fail("Docker must not be called"))
    with pytest.raises(ValueError, match="unsupported"):
        DockerVerifier(tmp_path, clock=SteppingClock())


_UID0_MESSAGE = "verification containers refuse effective uid 0"
_GID0_MESSAGE = "verification containers refuse effective gid 0 (the root group)"


@pytest.mark.parametrize(
    "uid, gid, message",
    [
        (0, 4343, _UID0_MESSAGE),  # uid 0 would make the process root
        (4242, 0, _GID0_MESSAGE),  # root group only; the process is not root
        (0, 0, _UID0_MESSAGE),  # the uid check runs first
    ],
)
def test_effective_uid_0_and_gid_0_are_refused(monkeypatch, tmp_path, uid, gid, message) -> None:
    """Both are refused before any Docker call, each with its exact fixed
    message: uid 0 would make the formerly non-root container root; gid 0
    with a nonzero uid would grant the root-group identity."""
    _fake_effective_identity(monkeypatch, uid, gid)
    monkeypatch.setattr("codeagent.executor._run_docker", lambda *a, **k: pytest.fail("Docker must not be called"))
    with pytest.raises(ValueError) as excinfo:
        DockerVerifier(tmp_path, clock=SteppingClock())
    assert str(excinfo.value) == message


# --------------------------------------------------------------------
# ADR 0005 Amendment 1 (ledger D3): verifier cancellation
# --------------------------------------------------------------------

from codeagent.cancellation import CancellationSource  # noqa: E402


class _TokenAfter:
    """Reports cancellation from its (n+1)th `is_cancelled()` call on."""

    def __init__(self, n: int) -> None:
        self._n = n
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls > self._n


def _cancelled_token():
    source = CancellationSource()
    source.request("test")
    return source.token


def _killable_procs(monkeypatch):
    """A running fake attach client whose group-kill marks it killed."""
    procs: list[_FakeProc] = []

    def factory():
        proc = _FakeProc(raise_timeout_first=True, pid=_FAKE_PID)
        procs.append(proc)
        return proc

    calls: list = []

    def killpg(pid, sig):
        calls.append((pid, sig))
        for proc in procs:
            if proc.pid == pid:
                proc.killed = True

    monkeypatch.setattr("codeagent.executor.os.killpg", killpg)
    return factory, procs, calls


@pytest.mark.parametrize("lifecycle", [False, True])
def test_entry_cancelled_returns_cancelled_not_applicable_with_zero_docker_calls(
    monkeypatch, tmp_path, lifecycle
) -> None:
    def no_docker(*a, **k):
        raise AssertionError("no Docker call may happen after an entry cancellation")

    monkeypatch.setattr("codeagent.executor._run_docker", no_docker)
    monkeypatch.setattr("codeagent.executor.subprocess.Popen", no_docker)
    monkeypatch.setattr(docker_ownership_module, "docker_ps_all_id_name_pairs", no_docker)
    publisher = _FakeContainerPublisher()
    verifier = DockerVerifier(
        tmp_path,
        clock=SteppingClock(),
        lifecycle_context=_lifecycle_ctx(publisher) if lifecycle else None,
        cancellation=_cancelled_token(),
    )
    for result in (verifier.run_baseline(), verifier.run(0)):
        assert result.outcome is events.VerificationOutcome.CANCELLED
        assert result.cleanup_status is events.ContainerCleanupStatus.NOT_APPLICABLE
        assert result.exit_code is None and result.error is None
    assert publisher.calls == []  # before CREATING is published


def test_attach_running_cancel_group_kills_then_removes_by_id(monkeypatch, tmp_path) -> None:
    factory, procs, killpg_calls = _killable_procs(monkeypatch)
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch,
        tmp_path,
        publisher=publisher,
        docker_calls=[_create_success(), _result(0)],  # create, rm (no inspect)
        ps_results=[({}, {}), ({}, {})],
        popen_factory=factory,
    )
    verifier._cancellation = _TokenAfter(1)  # entry check passes; observed in the wait

    result = verifier.run_baseline()

    assert killpg_calls == [(_FAKE_PID, signal.SIGKILL)]
    assert procs[0].killed
    intents = [intent for _, intent, _ in publisher.calls]
    assert intents == [cl.ContainerIntent.CREATING, cl.ContainerIntent.PRESENT,
                       cl.ContainerIntent.REMOVING, cl.ContainerIntent.ABSENT]
    assert result.outcome is events.VerificationOutcome.CANCELLED
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert result.exit_code is None and result.error is None


def test_legacy_attach_running_cancel_is_cancelled_and_confirmed_absent(monkeypatch, tmp_path) -> None:
    factory, _, killpg_calls = _killable_procs(monkeypatch)
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
        popen_factory=factory,
        cancellation=_TokenAfter(1),
    )
    result = verifier.run_baseline()
    assert killpg_calls == [(_FAKE_PID, signal.SIGKILL)]
    assert result.outcome is events.VerificationOutcome.CANCELLED
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT


@pytest.mark.parametrize("lifecycle", [False, True])
def test_cleanup_unconfirmed_after_cancel_is_environment_failure(monkeypatch, tmp_path, lifecycle) -> None:
    """M13: never CANCELLED with an unconfirmed cleanup."""
    factory, _, _ = _killable_procs(monkeypatch)
    if lifecycle:
        verifier = _make_lifecycle_verifier(
            monkeypatch,
            tmp_path,
            publisher=_FakeContainerPublisher(),
            docker_calls=[_create_success(), _result(0)],
            ps_results=[({}, {}), ({_baseline_name(): _FIXED_CONTAINER_ID}, {_FIXED_CONTAINER_ID: _baseline_name()})],
            popen_factory=factory,
        )
        verifier._cancellation = _TokenAfter(1)
    else:
        verifier = _make_verifier(
            monkeypatch,
            tmp_path,
            docker_calls=[_create_success(), _result(0), _listing([(_FIXED_CONTAINER_ID, "still-here")])],
            popen_factory=factory,
            cancellation=_TokenAfter(1),
        )
    result = verifier.run_baseline()
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED


def test_client_exited_first_keeps_the_genuine_outcome(monkeypatch, tmp_path) -> None:
    """M30: a token set after the client exited never overrides its result."""
    killpg_calls = _record_killpg(monkeypatch)
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[
            _create_success(),
            _result(0, _state_json(status="exited", exit_code=1)),
            _result(0),
            _listing([(_OTHER_CONTAINER_ID, "unrelated")]),
        ],
        popen_factory=lambda: _FakeProc(),  # already exited
        cancellation=_TokenAfter(1),
    )
    result = verifier.run_baseline()
    assert result.outcome is events.VerificationOutcome.TEST_FAILURE
    assert killpg_calls == []


def test_attach_wait_is_sliced(monkeypatch, tmp_path) -> None:
    """M5: every wait on the attach client is at most one 0.1 s slice."""
    factory, procs, _ = _killable_procs(monkeypatch)
    waits: list = []
    real_wait = _FakeProc.wait

    def recording_wait(self, timeout=None):
        waits.append(timeout)
        return real_wait(self, timeout)

    monkeypatch.setattr(_FakeProc, "wait", recording_wait)
    verifier = _make_verifier(
        monkeypatch,
        tmp_path,
        docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
        popen_factory=factory,
        cancellation=_TokenAfter(4),
    )
    verifier.run_baseline()
    slices = waits[:-1]  # the last wait is the post-kill reap
    assert len(slices) == 3 and all(t is not None and t <= 0.1 for t in slices)


# ADR 0005 Amendment 1 (ledger D3): L2 real-process session tests (a
# `docker` PATH shim; no Docker daemon needed).
from tests.support import process_shims as _shims  # noqa: E402


def test_l2_attach_client_leads_its_own_session(tmp_path, monkeypatch):
    _shims.check_session("L2", tmp_path, monkeypatch)


def test_l2_group_sigint_never_reaches_the_attach_client(tmp_path):
    _shims.check_group_sigint_spared("L2", tmp_path)


def test_l2_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    _shims.check_abort_group_kill("L2", tmp_path, monkeypatch)


# ADR 0005 Amendment 1 (D3 joint-review correction): the attach client's
# group kill is fail-closed. ProcessLookupError = group confirmed absent;
# any other failure still kills and reaps the client exactly once and
# removes the container by id, but the result is never CANCELLED/TIMEOUT.

_EXE_GROUP_TEXT = "SENTINEL-group-kill-/secret/path"


def _group_procs(monkeypatch, killpg_exc):
    procs: list[_FakeProc] = []
    kills: list[int] = []

    class _Counting(_FakeProc):
        def kill(self):
            kills.append(1)
            super().kill()

    def factory():
        proc = _Counting(raise_timeout_first=True, pid=_FAKE_PID)
        procs.append(proc)
        return proc

    calls = _record_killpg(monkeypatch, raises=killpg_exc)
    return factory, calls, kills


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("how", ["cancel", "timeout"])
def test_attach_group_kill_failure_fails_closed(monkeypatch, tmp_path, lifecycle, how):
    factory, calls, kills = _group_procs(monkeypatch, PermissionError(_EXE_GROUP_TEXT))
    token = _TokenAfter(1) if how == "cancel" else None
    timeout = 30.0 if how == "cancel" else 0.2
    if lifecycle:
        publisher = _FakeContainerPublisher()
        verifier = _make_lifecycle_verifier(
            monkeypatch, tmp_path, publisher=publisher,
            docker_calls=[_create_success(), _result(0)], ps_results=[({}, {}), ({}, {})], popen_factory=factory,
        )
        verifier._timeout_seconds = timeout
        if token is not None:
            verifier._cancellation = token
    else:
        verifier = _make_verifier(
            monkeypatch, tmp_path,
            docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
            popen_factory=factory, timeout_seconds=timeout, cancellation=token,
        )
    result = verifier.run_baseline()
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code is ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE
    assert result.error.message == "the verification attach client could not be confirmed terminated"
    # The container itself was still removed by id and confirmed absent.
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert [sig for _, sig in calls] == [signal.SIGKILL, 0] and len(kills) == 1
    if lifecycle:
        assert [i for _, i, _ in publisher.calls][-2:] == [cl.ContainerIntent.REMOVING, cl.ContainerIntent.ABSENT]


@pytest.mark.parametrize("how", ["cancel", "timeout"])
def test_attach_group_absent_keeps_the_genuine_cancel_or_timeout(monkeypatch, tmp_path, how):
    factory, calls, kills = _group_procs(monkeypatch, ProcessLookupError())
    verifier = _make_verifier(
        monkeypatch, tmp_path,
        docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
        popen_factory=factory,
        timeout_seconds=30.0 if how == "cancel" else 0.2,
        cancellation=_TokenAfter(1) if how == "cancel" else None,
    )
    result = verifier.run_baseline()
    expected = events.VerificationOutcome.CANCELLED if how == "cancel" else events.VerificationOutcome.TIMEOUT
    assert result.outcome is expected
    assert [sig for _, sig in calls] == [signal.SIGKILL] and len(kills) == 1


@pytest.mark.parametrize("how", ["cancel", "timeout"])
def test_attach_post_reap_probe_confirms_an_absent_group(monkeypatch, tmp_path, how):
    """macOS EPERM for a group holding only its exited leader: the
    post-reap signal-0 probe's ProcessLookupError keeps the genuine result."""
    procs: list[_FakeProc] = []

    def factory():
        proc = _FakeProc(raise_timeout_first=True, pid=_FAKE_PID)
        procs.append(proc)
        return proc

    calls = []

    def killpg(pid, sig):
        calls.append(sig)
        raise PermissionError(_EXE_GROUP_TEXT) if sig == signal.SIGKILL else ProcessLookupError()

    monkeypatch.setattr("codeagent.executor.os.killpg", killpg)
    verifier = _make_verifier(
        monkeypatch, tmp_path,
        docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
        popen_factory=factory,
        timeout_seconds=30.0 if how == "cancel" else 0.2,
        cancellation=_TokenAfter(1) if how == "cancel" else None,
    )
    result = verifier.run_baseline()
    expected = events.VerificationOutcome.CANCELLED if how == "cancel" else events.VerificationOutcome.TIMEOUT
    assert result.outcome is expected and calls == [signal.SIGKILL, 0]


# ADR 0005 Amendment 1 §12 (second joint-review correction): every attach-
# abort failure is retained, never raised raw, and the known container id is
# still removed. Contract: one group kill; a direct kill only if it raised;
# a bounded reap; if unconfirmed, exactly one retry kill and one more reap.

from codeagent import executor as _exe  # noqa: E402


class _ScriptedProc:
    """`kills`/`waits` scripts: each entry is None (succeed), "timeout", or an
    exception instance to raise. Never a real pid (killpg is patched)."""

    def __init__(self, *, kills=(), waits=()):
        self.pid = _FAKE_PID
        self._kills, self._waits = list(kills), list(waits)
        self.kill_calls = 0
        self.wait_calls = 0
        self.returncode = None
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        action = self._kills.pop(0) if self._kills else None
        if isinstance(action, BaseException):
            raise action

    def wait(self, timeout=None):
        self.wait_calls += 1
        action = self._waits.pop(0) if self._waits else None
        if action == "timeout":
            raise subprocess.TimeoutExpired(["docker", "start", "/secret/path"], timeout)
        if isinstance(action, BaseException):
            raise action
        self.returncode = -9
        return -9


def _patch_group(monkeypatch, group_exc=None, probe_exc=ProcessLookupError()):
    calls = []

    def killpg(pid, sig):
        calls.append(sig)
        if sig == signal.SIGKILL:
            if group_exc is not None:
                raise group_exc
            return
        raise probe_exc

    monkeypatch.setattr("codeagent.executor.os.killpg", killpg)
    return calls


def _sentinel(name):
    return RuntimeError(f"SENTINEL-{name}-/secret/path")


@pytest.mark.parametrize("probe_absent", [True, False])
def test_l2_abort_group_fails_and_direct_kill_raises(monkeypatch, probe_absent):
    a, b = PermissionError("SENTINEL-a"), _sentinel("b")
    calls = _patch_group(monkeypatch, a, ProcessLookupError() if probe_absent else PermissionError("p"))
    proc = _ScriptedProc(kills=[b], waits=[None])
    record = _exe._abort_attach_client(proc, _exe._AttachAbort())
    assert record.group_failure is a and record.kill_failures == [b] and record.reap_failures == []
    assert (proc.kill_calls, proc.wait_calls, record.kills, record.reaps) == (1, 1, 1, 1)
    assert calls == [signal.SIGKILL, 0]
    assert record.confirmed is probe_absent


@pytest.mark.parametrize("second_wait, confirmed", [(None, True), ("timeout", False)])
def test_l2_abort_reap_timeout_then_retry_kill_raises(monkeypatch, second_wait, confirmed):
    c = _sentinel("c")
    calls = _patch_group(monkeypatch)  # the group kill succeeds
    proc = _ScriptedProc(kills=[c], waits=["timeout", second_wait])
    record = _exe._abort_attach_client(proc, _exe._AttachAbort())
    # No direct kill first (the group kill succeeded); exactly one retry.
    assert (proc.kill_calls, proc.wait_calls) == (1, 2)
    assert record.kill_failures == [c] and record.reap_failures == []
    assert calls == [signal.SIGKILL] and record.confirmed is confirmed


@pytest.mark.parametrize("waits, confirmed", [(["D", None], True), (["D", "D2"], False)])
def test_l2_abort_wait_raises_a_non_timeout_sentinel(monkeypatch, waits, confirmed):
    d, d2 = _sentinel("d"), _sentinel("d2")
    _patch_group(monkeypatch, PermissionError("SENTINEL-a"))
    script = [d if w == "D" else d2 if w == "D2" else w for w in waits]
    proc = _ScriptedProc(waits=script)
    record = _exe._abort_attach_client(proc, _exe._AttachAbort())
    assert record.reap_failures == [x for x in script if isinstance(x, BaseException)]
    assert (proc.kill_calls, proc.wait_calls) == (2, 2)
    assert record.reaped is confirmed


def _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc):
    publisher = _FakeContainerPublisher()
    verifier = _make_lifecycle_verifier(
        monkeypatch, tmp_path, publisher=publisher,
        docker_calls=[_create_success(), _result(0)],  # create, rm --force <id>
        ps_results=[({}, {}), ({}, {})],  # pre-create; fresh post-rm absence
        popen_factory=lambda: proc,
    )
    verifier._cancellation = _TokenAfter(1)
    return verifier, publisher


def _assert_removed_by_id_and_failed_closed(result, publisher):
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.error.code is ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE
    assert result.error.message == "the verification attach client could not be confirmed terminated"
    assert "SENTINEL" not in repr(result) and "/secret/path" not in repr(result)
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert [(i, x) for _, i, x in publisher.calls][-2:] == [
        (cl.ContainerIntent.REMOVING, _FIXED_CONTAINER_ID), (cl.ContainerIntent.ABSENT, None)
    ]


def test_l2_multiple_abort_failures_still_remove_the_container_by_id(monkeypatch, tmp_path):
    a, b, d, e = PermissionError("SENTINEL-a"), _sentinel("b"), _sentinel("d"), _sentinel("e")
    _patch_group(monkeypatch, a, PermissionError("p"))
    proc = _ScriptedProc(kills=[b, e], waits=[d, "timeout"])
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    result = verifier.run_baseline()
    _assert_removed_by_id_and_failed_closed(result, publisher)
    record = verifier._last_attach_abort
    assert record.group_failure is a and record.kill_failures == [b, e] and record.reap_failures == [d]
    assert (proc.kill_calls, proc.wait_calls, record.reaped) == (2, 2, False)


def test_l2_failed_attach_wait_still_removes_the_container_by_id(monkeypatch, tmp_path):
    w = _sentinel("w")
    _patch_group(monkeypatch)

    def failing_wait(process, **kwargs):
        raise w

    monkeypatch.setattr(_exe, "wait_cancellable", failing_wait)
    proc = _ScriptedProc(waits=[None])
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    result = verifier.run_baseline()
    _assert_removed_by_id_and_failed_closed(result, publisher)
    assert verifier._last_attach_abort.wait_failure is w
    assert (proc.kill_calls, proc.wait_calls) == (0, 1)  # the group kill succeeded


def test_l2_legacy_path_abort_failures_still_remove_by_id(monkeypatch, tmp_path):
    _patch_group(monkeypatch, PermissionError("SENTINEL-a"), PermissionError("p"))
    proc = _ScriptedProc(kills=[_sentinel("b")], waits=[None])
    removed = []
    verifier = _make_verifier(
        monkeypatch, tmp_path,
        docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
        popen_factory=lambda: proc, cancellation=_TokenAfter(1),
    )
    real_cleanup = DockerVerifier._cleanup
    monkeypatch.setattr(DockerVerifier, "_cleanup", lambda self, name, cid: (removed.append(cid), real_cleanup(self, name, cid))[1])
    result = verifier.run_baseline()
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    assert removed == [_FIXED_CONTAINER_ID]


@pytest.mark.parametrize(
    "scenario",
    ["group_fails_direct_kill_raises", "reap_timeout_retry_kill_raises", "wait_raises_sentinel"],
)
def test_l2_each_abort_failure_still_removes_the_container_by_id(monkeypatch, tmp_path, scenario):
    """Path-level: through `run_baseline`, each named abort failure is
    retained (never raised raw), the attempt fails closed, and the container
    is still removed by immutable id. Exact kill/reap counts are pinned."""
    if scenario == "group_fails_direct_kill_raises":
        group_exc, kills, waits, counts = PermissionError("SENTINEL-a"), [_sentinel("b")], [None], (1, 1)
        probe = PermissionError("p")
    elif scenario == "reap_timeout_retry_kill_raises":
        group_exc, kills, waits, counts = None, [_sentinel("c")], ["timeout", None], (1, 2)
        probe = ProcessLookupError()
    else:
        group_exc, kills, waits, counts = None, [], [_sentinel("d"), "timeout"], (1, 2)
        probe = ProcessLookupError()
    _patch_group(monkeypatch, group_exc, probe)
    proc = _ScriptedProc(kills=kills, waits=waits)
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    result = verifier.run_baseline()
    record = verifier._last_attach_abort
    assert (proc.kill_calls, proc.wait_calls) == counts == (record.kills, record.reaps)
    assert record.kill_failures == [k for k in kills if isinstance(k, BaseException)]
    assert record.reap_failures == [w for w in waits if isinstance(w, BaseException)]
    if scenario == "reap_timeout_retry_kill_raises":
        # Reaped on the retry, group killed: a confirmed cancel despite the
        # retained kill failure (confirmation is by observation).
        assert result.outcome is events.VerificationOutcome.CANCELLED
        assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    else:
        _assert_removed_by_id_and_failed_closed(result, publisher)


# ADR 0005 Amendment 1 §12 (third joint-review correction): a BaseException
# (not an Exception) at any attach-abort step is retained, every remaining
# step runs, the container is removed by immutable id and its absence
# confirmed (ABSENT published), and only then is the exact instance raised.


class _InjectedBase(BaseException):
    pass


def _patch_group_steps(monkeypatch, *, group=None, probe=ProcessLookupError()):
    calls = []

    def killpg(pid, sig):
        calls.append(sig)
        action = group if sig == signal.SIGKILL else probe
        if action is not None:
            raise action

    monkeypatch.setattr("codeagent.executor.os.killpg", killpg)
    return calls


@pytest.mark.parametrize("make", [lambda: _InjectedBase("SENTINEL-base"), lambda: KeyboardInterrupt("SENTINEL-ki")])
@pytest.mark.parametrize("site", ["wait", "group_kill", "direct_kill", "reap", "probe"])
def test_l2_base_exception_cannot_bypass_removal_by_id(monkeypatch, tmp_path, site, make):
    inst = make()
    group, probe, kills, waits = None, ProcessLookupError(), [], [None]
    if site == "wait":
        def failing_wait(process, **kwargs):
            raise inst

        monkeypatch.setattr(_exe, "wait_cancellable", failing_wait)
    elif site == "group_kill":
        group = inst  # a direct kill follows; the probe confirms absence
    elif site == "direct_kill":
        group, kills = PermissionError("SENTINEL-g"), [inst]
    elif site == "reap":
        waits = [inst, None]  # retry kill and a second, confirmed reap
    else:
        group, probe = PermissionError("SENTINEL-g"), inst
    calls = _patch_group_steps(monkeypatch, group=group, probe=probe)
    proc = _ScriptedProc(kills=kills, waits=waits)
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    removed = []
    real_cleanup = DockerVerifier._cleanup_by_id
    monkeypatch.setattr(
        DockerVerifier, "_cleanup_by_id", lambda self, cid, name: (removed.append(cid), real_cleanup(self, cid, name))[1]
    )

    if site == "probe":
        # The probe's own failure leaves the group unconfirmed: cleanup
        # dominance (fourth correction) -- no raise; the sanitized failure
        # carries the exact interrupt.
        result = _without_escape(verifier.run_baseline)
        assert result.deferred_interrupt is inst
        assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
        assert "SENTINEL" not in repr(result)
    else:
        with pytest.raises(type(inst)) as info:
            verifier.run_baseline()
        assert info.value is inst
    # Removal by immutable id ran and absence was confirmed in every case.
    assert removed == [_FIXED_CONTAINER_ID]
    assert [(i, x) for _, i, x in publisher.calls][-2:] == [
        (cl.ContainerIntent.REMOVING, _FIXED_CONTAINER_ID), (cl.ContainerIntent.ABSENT, None)
    ]
    record = verifier._last_attach_abort
    step = {"direct_kill": "kill"}.get(site, site)
    assert (step, inst) in record.failures_in_order and record.deferred_interrupt is inst
    expected = {  # (direct kills, reaps, killpg signals)
        "wait": (0, 1, [signal.SIGKILL]),
        "group_kill": (1, 1, [signal.SIGKILL, 0]),
        "direct_kill": (1, 1, [signal.SIGKILL, 0]),
        "reap": (1, 2, [signal.SIGKILL]),
        "probe": (1, 1, [signal.SIGKILL, 0]),
    }[site]
    assert (proc.kill_calls, proc.wait_calls, calls) == expected


# ADR 0005 Amendment 1 §12 (fourth correction): the retained interrupt is
# re-raised only after FULLY confirmed cleanup. Any unconfirmed attach,
# group, removal, absence-confirmation or ABSENT-publication obligation ->
# the sanitized failure is returned, carrying the exact interrupt.

from codeagent import _docker_ownership as _own  # noqa: E402


def _wait_interrupt(monkeypatch, inst):
    def failing_wait(process, **kwargs):
        raise inst

    monkeypatch.setattr(_exe, "wait_cancellable", failing_wait)


_STILL_THERE = ({_baseline_name(): _FIXED_CONTAINER_ID}, {_FIXED_CONTAINER_ID: _baseline_name()})


def _without_escape(fn):
    """Call `fn`; any escaping `BaseException` (including KeyboardInterrupt,
    which would otherwise abort the pytest session) becomes a test failure."""
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"{type(exc).__name__} escaped instead of being retained")


@pytest.mark.parametrize("failure", ["removal_unconfirmed", "absence_unobservable", "absent_publication", "attach_group"])
def test_l2_lifecycle_cleanup_dominates_a_retained_interrupt(monkeypatch, tmp_path, failure):
    inst = KeyboardInterrupt("SENTINEL-ki")
    _wait_interrupt(monkeypatch, inst)
    group, probe = (PermissionError("SENTINEL-g"), PermissionError("SENTINEL-p")) if failure == "attach_group" else (None, ProcessLookupError())
    _patch_group_steps(monkeypatch, group=group, probe=probe)
    post_rm = {
        "removal_unconfirmed": _STILL_THERE,
        "absence_unobservable": _own.DockerListingError("SENTINEL-listing"),
    }.get(failure, ({}, {}))
    publisher = _FakeContainerPublisher()
    if failure == "absent_publication":
        publisher.fail_once(cl.ContainerRole.BASELINE, cl.ContainerIntent.ABSENT, _pub_error())
    verifier = _make_lifecycle_verifier(
        monkeypatch, tmp_path, publisher=publisher,
        docker_calls=[_create_success(), _result(0)], ps_results=[({}, {}), post_rm],
        popen_factory=lambda: _ScriptedProc(kills=[None], waits=[None]),
    )
    result = _without_escape(verifier.run_baseline)  # never raises: cleanup dominance
    assert result.deferred_interrupt is inst
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert "SENTINEL" not in repr(result) and "SENTINEL" not in result.error.message
    expected_status = {
        "removal_unconfirmed": events.ContainerCleanupStatus.UNCONFIRMED,
        "absence_unobservable": events.ContainerCleanupStatus.UNCONFIRMED,
    }.get(failure, events.ContainerCleanupStatus.CONFIRMED_ABSENT)
    assert result.cleanup_status is expected_status
    # Removal by immutable id was still attempted in every case.
    assert (cl.ContainerIntent.REMOVING, _FIXED_CONTAINER_ID) in [(i, x) for _, i, x in publisher.calls]


@pytest.mark.parametrize("failure", ["removal_unconfirmed", "absence_unobservable", "attach_group"])
def test_l2_legacy_cleanup_dominates_a_retained_interrupt(monkeypatch, tmp_path, failure):
    inst = _InjectedBase("SENTINEL-base")
    _wait_interrupt(monkeypatch, inst)
    group, probe = (PermissionError("SENTINEL-g"), PermissionError("SENTINEL-p")) if failure == "attach_group" else (None, ProcessLookupError())
    _patch_group_steps(monkeypatch, group=group, probe=probe)
    listing = {
        "removal_unconfirmed": _listing([(_FIXED_CONTAINER_ID, "still-here")]),
        "absence_unobservable": _result(1),
    }.get(failure, _listing([(_OTHER_CONTAINER_ID, "unrelated")]))
    verifier = _make_verifier(
        monkeypatch, tmp_path, docker_calls=[_create_success(), _result(0), listing],
        popen_factory=lambda: _ScriptedProc(kills=[None], waits=[None]),
    )
    result = _without_escape(verifier.run_baseline)
    assert result.deferred_interrupt is inst
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert "SENTINEL" not in repr(result)
    if failure == "attach_group":
        assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
    else:
        assert result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED


@pytest.mark.parametrize("lifecycle", [False, True])
def test_l2_fully_confirmed_cleanup_reraises_the_exact_interrupt(monkeypatch, tmp_path, lifecycle):
    inst = SystemExit("SENTINEL-exit")
    _wait_interrupt(monkeypatch, inst)
    _patch_group_steps(monkeypatch)
    if lifecycle:
        publisher = _FakeContainerPublisher()
        verifier = _make_lifecycle_verifier(
            monkeypatch, tmp_path, publisher=publisher, docker_calls=[_create_success(), _result(0)],
            ps_results=[({}, {}), ({}, {})], popen_factory=lambda: _ScriptedProc(waits=[None]),
        )
    else:
        verifier = _make_verifier(
            monkeypatch, tmp_path,
            docker_calls=[_create_success(), _result(0), _listing([(_OTHER_CONTAINER_ID, "unrelated")])],
            popen_factory=lambda: _ScriptedProc(waits=[None]),
        )
    with pytest.raises(SystemExit) as info:
        verifier.run_baseline()
    assert info.value is inst
    if lifecycle:
        assert [i for _, i, _ in publisher.calls][-1] is cl.ContainerIntent.ABSENT


def test_verification_result_deferred_interrupt_must_be_a_non_exception():
    from codeagent.controller import VerificationResult

    with pytest.raises(ValueError):
        VerificationResult(
            outcome=events.VerificationOutcome.PASSED, exit_code=0, duration_seconds=0.0, stdout="", stderr="",
            cleanup_status=events.ContainerCleanupStatus.CONFIRMED_ABSENT, deferred_interrupt=RuntimeError("x"),
        )


# ADR 0005 Amendment 1 §12 (fifth correction), applied to L2 for one model.


def test_l2_ordinary_wait_failure_stays_primary_over_a_cleanup_interrupt(monkeypatch, tmp_path):
    body, ki = RuntimeError("SENTINEL-body"), KeyboardInterrupt("SENTINEL-ki")
    _wait_interrupt(monkeypatch, body)
    _patch_group_steps(monkeypatch, group=ki)  # probe confirms absence
    proc = _ScriptedProc(kills=[None], waits=[None])
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    result = _without_escape(verifier.run_baseline)
    assert result.outcome is events.VerificationOutcome.ENVIRONMENT_FAILURE
    assert result.deferred_interrupt is ki and "SENTINEL" not in repr(result)
    assert [i for _, i, _ in publisher.calls][-1] is cl.ContainerIntent.ABSENT


def test_l2_reraised_interrupt_carries_every_other_failure_in_order(monkeypatch, tmp_path):
    ki1, ki2 = KeyboardInterrupt("SENTINEL-1"), _InjectedBase("SENTINEL-2")
    cause, context = RuntimeError("c"), RuntimeError("x")
    ki1.__cause__, ki1.__context__ = cause, context
    _wait_interrupt(monkeypatch, ki1)
    _patch_group_steps(monkeypatch, group=ki2)  # probe confirms absence
    proc = _ScriptedProc(kills=[None], waits=[None])
    verifier, _publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    with pytest.raises(KeyboardInterrupt) as info:
        verifier.run_baseline()
    assert info.value is ki1 and ki1.codeagent_cleanup_failures == (ki2,)
    assert ki1.__cause__ is cause and ki1.__context__ is context
    assert (proc.kill_calls, proc.wait_calls) == (1, 1)


# Sixth correction: an attribute-refusing interrupt is still re-raised.
from tests.support.refusing import REFUSALS, refusing  # noqa: E402


@pytest.mark.parametrize("make_refusal", REFUSALS)
def test_l2_refusing_interrupt_is_still_reraised_exactly(monkeypatch, tmp_path, make_refusal):
    ki1 = refusing(KeyboardInterrupt, make_refusal())
    _wait_interrupt(monkeypatch, ki1)
    _patch_group_steps(monkeypatch, group=_InjectedBase("SENTINEL-2"))  # probe confirms absence
    proc = _ScriptedProc(kills=[None], waits=[None])
    verifier, publisher = _cancel_lifecycle_verifier(monkeypatch, tmp_path, proc)
    with pytest.raises(KeyboardInterrupt) as info:
        verifier.run_baseline()
    assert info.value is ki1
    assert [i for _, i, _ in publisher.calls][-1] is cl.ContainerIntent.ABSENT
