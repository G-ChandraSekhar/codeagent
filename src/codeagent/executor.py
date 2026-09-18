"""Milestone 1 slice C: a real, sandboxed Docker verifier.

Implements `codeagent.controller.Verifier` against a real Docker
container — the same disposable-worktree, structured-argv, no-shell
discipline as `codeagent.workspace.GitWorktree` and
`codeagent.patch.GitPatchApplier`.

Explicitly provisional (see docs/threat-model.md and
docs/CODEAGENT_LLM_HANDOFF.md's Stage 2 spike list): the resource
limits below (memory/CPU/PID caps, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`) are configured, and this module's
own tests exercise the *ordinary* failure paths (timeout, environment
failure, command-start failure). Whether these limits actually hold
under adversarial load, and what happens if the host process is killed
mid-`docker run`, are the Stage-2 isolation and interruption spikes'
job — both still unrun. This module does not claim Milestone-3-grade
sandboxing.

Ordinary Docker verification (the happy/expected-failure paths this
module's own tests and the real end-to-end slice-C test exercise) has
now passed on two platforms: macOS/arm64 (Docker Desktop, manual/local
verification) and GitHub-hosted Ubuntu 24.04 x86_64 (`.github/
workflows/ci.yml`, run 34734760525, commit a845cb3). That is evidence
of ordinary correctness on both, not of adversarial resource-limit
enforcement or interruption safety on either — those remain
unvalidated on both platforms pending the Stage-2 isolation and
interruption spikes, and no other OS/architecture combination has been
exercised at all (see the threat model's A9).

Container lifecycle, deliberately inspectable rather than a single
`docker run`: `create` → `start --attach` (bounded, streaming output
collection) → `inspect` for the container's own recorded exit state →
`rm --force` in a `finally`, followed by `docker ps -a` to confirm the
container's name is genuinely absent afterward. A Docker CLI client's
own exit code is never used to classify the outcome — only the
container's inspected `State.Status`/`State.ExitCode` are trusted for
PASSED/TEST_FAILURE. Cleanup confirmation runs unconditionally
regardless of how (or whether) create/start/inspect failed — a
container is never left un-checked just because an earlier stage
failed — and if absence cannot be confirmed (nonzero listing, an
OSError launching the listing itself, or the name still present),
the outcome is always ENVIRONMENT_FAILURE, overriding whatever the
container's own exit state would otherwise have produced: a cleanup
*attempt* is not a cleanup *guarantee*.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from codeagent import events
from codeagent.controller import Clock, SystemClock, VerificationResult
from codeagent.errors import ErrorCode, OperationalError

CONTAINER_NAME_PREFIX = "codeagent-verify-"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_IMAGE = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
DEFAULT_COMMAND: tuple[str, ...] = ("python3", "-B", "-m", "unittest", "tests.test_worker")

# The complete digest shape, not substring presence — "@sha256:" alone
# would accept a truncated or malformed digest (e.g. "@sha256:zz" or a
# value with trailing garbage after the hex) as if it were a real pin.
_DIGEST_PINNED_IMAGE_RE = re.compile(r"@sha256:[0-9a-fA-F]{64}$")

_MAX_STREAM_BYTES = 64 * 1024

_SECURITY_FLAGS: tuple[str, ...] = (
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,size=64m",
    "--memory",
    "512m",
    # Without --memory-swap, --memory alone does not cap the combined
    # memory+swap allowance: Stage-2 spike S4's retained evidence
    # observed HostConfig.Memory=512 MiB but
    # HostConfig.MemorySwap=1024 MiB total (i.e. ~512 MiB of additional
    # swap on top of the memory limit) on both macOS/Docker Desktop and
    # native Linux Docker Engine (the specific hosts tested — see
    # spikes/s4/S4_RESULT.md; this is an observed configuration on
    # those hosts, not a claimed universal Docker default).
    # --memory-swap set equal to --memory means "no additional swap
    # beyond the memory limit," closing that gap for the combined
    # memory+swap allowance.
    "--memory-swap",
    "512m",
    "--cpus",
    "1",
    "--pids-limit",
    "128",
    "--user",
    "1000:1000",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
)


class _DockerLaunchError(Exception):
    """The `docker` executable itself couldn't be launched (OSError) —
    at *any* stage. Always maps to COMMAND_START_FAILURE, regardless of
    which docker subcommand raised it."""


class _BoundedCollector:
    """Retains at most `limit` bytes fed to it, across any number of
    `feed()` calls — used to drain a live stream continuously rather
    than capturing everything and truncating afterward."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0
        self._truncated = False

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if self._size >= self._limit:
            self._truncated = True
            return
        remaining = self._limit - self._size
        if len(data) > remaining:
            data = data[:remaining]
            self._truncated = True
        self._chunks.append(data)
        self._size += len(data)

    def text(self) -> str:
        raw = b"".join(self._chunks)
        decoded = raw.decode("utf-8", errors="replace")
        if self._truncated:
            decoded += "\n...(truncated)"
        return decoded


def _pump(stream, collector: _BoundedCollector) -> None:
    try:
        for chunk in iter(lambda: stream.read(4096), b""):
            collector.feed(chunk)
    finally:
        stream.close()


def _run_docker(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True)
    except OSError as exc:
        raise _DockerLaunchError(str(exc)) from exc


class DockerVerifier:
    def __init__(
        self,
        worktree_path,
        *,
        image: str = DEFAULT_IMAGE,
        command: tuple[str, ...] = DEFAULT_COMMAND,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        resolved = Path(worktree_path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"worktree_path must be an existing directory, got {resolved!r}")
        if not image:
            raise ValueError("image must be a nonempty string")
        if not _DIGEST_PINNED_IMAGE_RE.search(image):
            raise ValueError(
                "image must be pinned by a complete digest "
                f"('@sha256:' followed by exactly 64 hex characters), got {image!r} — "
                "a floating tag or a malformed/truncated digest is not an acceptable "
                "production identity"
            )
        if not command:
            raise ValueError("command must be a nonempty tuple")
        for i, part in enumerate(command):
            if not part:
                raise ValueError(f"command[{i}] must be a nonempty string, got {part!r}")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be finite and > 0, got {timeout_seconds!r}"
            )

        # Docker bind-mount sources must be absolute — resolved above so
        # a caller passing a relative path doesn't get a confusing
        # "failed to create the verification container" instead of a
        # clear reason.
        self._worktree_path = str(resolved)
        self._image = image
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._clock = clock or SystemClock()
        self._error_id_seq = 0

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def run_baseline(self) -> VerificationResult:
        return self._execute("baseline")

    def run(self, attempt_index: int) -> VerificationResult:
        return self._execute(f"verify-{attempt_index}")

    def _next_error_id(self, label: str) -> str:
        self._error_id_seq += 1
        return f"docker-{label}-err-{self._error_id_seq}"

    def _error(self, code: ErrorCode, label: str, message: str) -> OperationalError:
        return OperationalError(code=code, error_id=self._next_error_id(label), message=message)

    def _attempt(
        self, name: str, label: str
    ) -> tuple[
        bool, bool, events.VerificationOutcome, int | None, str, str, OperationalError | None
    ]:
        """Runs create -> start -> inspect for one container and returns
        (create_attempted, created, outcome, exit_code, stdout, stderr,
        error) — a provisional result. Never raises: every failure mode
        here is turned into a returned outcome instead of propagating,
        so the caller's cleanup step always runs regardless of what
        happened here, and never has to guess whether a container might
        exist from inside an exception handler.

        `create_attempted` is set to `True` immediately before the
        `docker create` invocation — including when that invocation
        itself raises `_DockerLaunchError` (the docker executable could
        not even be launched). This means a bare launch failure is
        still treated as "attempted": cleanup must still be confirmed
        for it (conservatively — nothing was actually created, so
        confirmation always finds it genuinely absent), never reported
        as `NOT_APPLICABLE`. `NOT_APPLICABLE` is reserved for a failure
        that occurs strictly *before* this point — none exists in this
        implementation today (nothing here validates anything between
        entering `_attempt` and issuing the create call), so it remains
        structurally reachable for a future pre-create check rather
        than produced by any path today; see
        `_cleanup_status_for`'s dedicated unit tests for how that case
        is still verified directly, and `created` — distinct from
        `create_attempted` — for whether a container object might
        actually exist afterward.
        """
        create_attempted = False
        try:
            create_attempted = True
            create_result = _run_docker(
                "create",
                "--name",
                name,
                *_SECURITY_FLAGS,
                "--mount",
                f"type=bind,source={self._worktree_path},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                self._image,
                *self._command,
            )
        except _DockerLaunchError:
            return (
                create_attempted,
                False,
                events.VerificationOutcome.COMMAND_START_FAILURE,
                None,
                "",
                "",
                self._error(
                    ErrorCode.EXECUTOR_COMMAND_START_FAILED,
                    label,
                    "docker executable could not be launched",
                ),
            )
        if create_result.returncode != 0:
            return (
                create_attempted,
                False,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                None,
                "",
                "",
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "failed to create the verification container",
                ),
            )

        try:
            exit_code, status, oom_killed, stdout_text, stderr_text, timed_out = (
                self._start_and_inspect(name)
            )
        except _DockerLaunchError:
            return (
                create_attempted,
                True,
                events.VerificationOutcome.COMMAND_START_FAILURE,
                None,
                "",
                "",
                self._error(
                    ErrorCode.EXECUTOR_COMMAND_START_FAILED,
                    label,
                    "docker executable could not be launched",
                ),
            )

        if timed_out:
            return (
                create_attempted,
                True,
                events.VerificationOutcome.TIMEOUT,
                None,
                stdout_text,
                stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_TIMEOUT,
                    label,
                    "verification container exceeded its time budget",
                ),
            )
        if status != "exited" or exit_code is None or oom_killed is None:
            return (
                create_attempted,
                True,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                None,
                stdout_text,
                stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "failed to inspect the verification container's final state",
                ),
            )
        # A confirmed OOM kill takes precedence over both PASSED and
        # TEST_FAILURE: the container was killed by the kernel before
        # any exit code it reports can be trusted as a real test
        # result (Milestone 3, following Stage-2 spike S4 — see
        # spikes/s4/S4_RESULT.md). The exit code is preserved for the
        # record, but only a fixed, sanitized message is persisted —
        # never raw Docker inspect payloads or daemon output.
        if oom_killed:
            return (
                create_attempted,
                True,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                exit_code,
                stdout_text,
                stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_OOM_KILLED,
                    label,
                    "verification container was killed for exceeding its memory limit",
                ),
            )
        if exit_code == 0:
            return (
                create_attempted,
                True,
                events.VerificationOutcome.PASSED,
                0,
                stdout_text,
                stderr_text,
                None,
            )
        return (
            create_attempted,
            True,
            events.VerificationOutcome.TEST_FAILURE,
            exit_code,
            stdout_text,
            stderr_text,
            None,
        )

    @staticmethod
    def _cleanup_status_for(
        *, create_attempted: bool, confirmed_absent: bool
    ) -> events.ContainerCleanupStatus:
        """The structural mapping from (create_attempted, cleanup
        confirmation) to a `ContainerCleanupStatus` — factored out as a
        pure function so `NOT_APPLICABLE` (a case `_attempt` cannot
        currently produce, since nothing today fails before the create
        call) can still be verified directly."""
        if not create_attempted:
            return events.ContainerCleanupStatus.NOT_APPLICABLE
        return (
            events.ContainerCleanupStatus.CONFIRMED_ABSENT
            if confirmed_absent
            else events.ContainerCleanupStatus.UNCONFIRMED
        )

    def _execute(self, label: str) -> VerificationResult:
        start = self._clock.monotonic()
        name = f"{CONTAINER_NAME_PREFIX}{label}-{uuid4().hex[:12]}"

        create_attempted, _created, outcome, exit_code, stdout_text, stderr_text, error = (
            self._attempt(name, label)
        )

        # Cleanup confirmation runs unconditionally whenever creation
        # was attempted — regardless of whether create itself failed,
        # a launch failure occurred, or the container ran to completion
        # — since any of those can leave a real container object
        # behind. Only a genuine "never attempted" case skips it.
        confirmed_absent = self._cleanup(name) if create_attempted else True
        cleanup_status = self._cleanup_status_for(
            create_attempted=create_attempted, confirmed_absent=confirmed_absent
        )

        duration = self._clock.monotonic() - start

        if cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED:
            # Overrides any provisional outcome, including a would-be
            # PASSED: a cleanup attempt is not a cleanup guarantee, and
            # this module never reports a successful run it can't also
            # confirm cleaned up after.
            return VerificationResult(
                outcome=events.VerificationOutcome.ENVIRONMENT_FAILURE,
                exit_code=None,
                duration_seconds=duration,
                stdout=stdout_text,
                stderr=stderr_text,
                cleanup_status=cleanup_status,
                error=self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "verification container could not be confirmed removed",
                ),
            )

        return VerificationResult(
            outcome=outcome,
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout_text,
            stderr=stderr_text,
            cleanup_status=cleanup_status,
            error=error,
        )

    def _start_and_inspect(
        self, name: str
    ) -> tuple[int | None, str | None, bool | None, str, str, bool]:
        stdout_collector = _BoundedCollector(_MAX_STREAM_BYTES)
        stderr_collector = _BoundedCollector(_MAX_STREAM_BYTES)
        try:
            proc = subprocess.Popen(
                ["docker", "start", "--attach", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise _DockerLaunchError(str(exc)) from exc

        stdout_thread = threading.Thread(
            target=_pump, args=(proc.stdout, stdout_collector), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_pump, args=(proc.stderr, stderr_collector), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        stdout_text = stdout_collector.text()
        stderr_text = stderr_collector.text()

        if timed_out:
            return None, None, None, stdout_text, stderr_text, True

        try:
            inspect_result = _run_docker("inspect", "--format", "{{json .State}}", name)
        except _DockerLaunchError:
            return None, None, None, stdout_text, stderr_text, False
        if inspect_result.returncode != 0:
            return None, None, None, stdout_text, stderr_text, False

        exit_code, status, oom_killed = self._parse_state(inspect_result.stdout)
        return exit_code, status, oom_killed, stdout_text, stderr_text, False

    @staticmethod
    def _parse_state(raw_state: str) -> tuple[int | None, str | None, bool | None]:
        """Parses `docker inspect --format '{{json .State}}'`'s output
        and validates it strictly, fail-closed: any parse error, wrong
        shape, missing field, or wrong field type returns
        (None, None, None) uniformly rather than a partially-trusted
        value — the caller then treats that as a malformed inspection
        (ENVIRONMENT_FAILURE), the same outcome as any other inspection
        failure. `ExitCode` must be an actual int, not a bool (`bool`
        is a subclass of `int` in Python, so `isinstance(x, int)` alone
        would silently accept `True`/`False` as an exit code)."""
        try:
            state = json.loads(raw_state)
        except (json.JSONDecodeError, ValueError):
            return None, None, None
        if not isinstance(state, dict):
            return None, None, None

        status = state.get("Status")
        exit_code = state.get("ExitCode")
        oom_killed = state.get("OOMKilled")

        if not isinstance(status, str):
            return None, None, None
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return None, None, None
        if not isinstance(oom_killed, bool):
            return None, None, None

        return exit_code, status, oom_killed

    def _cleanup(self, name: str) -> bool:
        """Best-effort removal, followed by a genuine confirmation check
        via `docker ps -a` — a cleanup *attempt* is not a cleanup
        *guarantee*. Returns True only if a successful listing
        afterward shows `name` is genuinely, exactly absent from it.

        Deliberately does not use `docker inspect name`'s exit code as
        the confirmation signal: a nonzero exit there is ambiguous
        between "confirmed gone" and "the daemon/client itself is
        broken" — both produce the same exit code, and treating both
        as confirmation risked reporting a successful run whose
        container secretly still existed. Listing everything and
        checking for the exact name removes that ambiguity: a failed
        or unparseable listing is always treated as unconfirmed, never
        as confirmed-absent.
        """
        try:
            _run_docker("rm", "--force", name)
        except _DockerLaunchError:
            pass

        try:
            listing = _run_docker("ps", "-a", "--format", "{{.Names}}")
        except _DockerLaunchError:
            return False
        if listing.returncode != 0:
            return False
        names = {line.strip() for line in listing.stdout.splitlines() if line.strip()}
        return name not in names
