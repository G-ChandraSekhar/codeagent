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

Milestone 3 Slice 3B-4 adds bounded, timeout-controlled execution for
every non-streaming Docker control-plane command (`create`, `inspect`,
`rm`, `ps -a`) via the shared `codeagent._bounded_subprocess` runner —
these previously had no timeout at all and captured output with no
size bound — plus strict capture and validation of `docker create`'s
full container ID. Once a valid ID exists, `start`/`inspect`/`rm` all
target that immutable ID rather than the mutable generated name, and
final cleanup confirmation checks a single fresh, unfiltered,
`--no-trunc` listing for the absence of *both* the exact generated
name and (when one was captured) the exact ID — closing the ambiguity
where a container's ID reappears live under a different name. This
slice introduces no lifecycle publisher, no lifecycle-store adapter,
no deterministic lifecycle-derived name, and no ownership label —
container naming remains the legacy UUID-suffixed scheme, and no
lifecycle projection is written anywhere in this module.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from uuid import uuid4

from codeagent import container_lifecycle, events
from codeagent import _docker_ownership as docker_ownership
from codeagent._bounded_subprocess import BoundedProcessError, BoundedProcessFailure, run_bounded_stdout
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

# Slice 3B-4: fixed, documented, per-command bounds and timeouts for
# every non-streaming Docker control-plane command — never one
# unexplained arbitrary cap shared across all of them.
#
# `docker create`'s own control-plane call is quick (it does not run
# the container), unlike `docker start --attach`, whose own timeout is
# the caller-configured verification budget (`self._timeout_seconds`)
# and stays entirely separate from this fixed control-plane timeout.
_DOCKER_CONTROL_PLANE_TIMEOUT_SECONDS = 30.0
# A successful `docker create`'s entire stdout is exactly the created
# container's full ID (64 lowercase hex characters) plus one trailing
# LF — 65 bytes, never more.
_CREATE_ID_MAX_BYTES = 65
# `docker inspect --format '{{json .State}}'`'s State object is small
# (Status/ExitCode/OOMKilled plus a handful of timestamps and an
# optional Health block) but is not itself size-pinned by Docker;
# bounded generously above any observed real size while still being a
# hard, documented, non-arbitrary bound, matching this module's own
# existing `_MAX_STREAM_BYTES` bound for streamed output.
_INSPECT_STATE_MAX_BYTES = 64 * 1024
# `docker rm --force`'s own stdout is never read or trusted as proof of
# removal (see `_cleanup`) — it is normally just the removed
# name/ID echoed back on one line. Bounded to a small, fixed value
# purely so an unexpected torrent of output cannot be captured
# unboundedly; its content is discarded either way.
_RM_OUTPUT_MAX_BYTES = 4096
# A full, unfiltered `docker ps -a --no-trunc` listing on a busy host
# can be large; matches `codeagent.reconciliation`'s own
# `_DOCKER_OUTPUT_MAX_BYTES` bound for the identical command shape.
_CLEANUP_LISTING_MAX_BYTES = 1024 * 1024

# A Docker container ID is exactly 64 lowercase hexadecimal ASCII
# characters — never uppercase, never abbreviated, never anything else.
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Docker's own container-name grammar: an ASCII alphanumeric first
# character, then any run of ASCII alphanumeric, underscore, period, or
# hyphen -- never whitespace, CR, NUL, or any non-ASCII character. A
# cleanup-listing row whose name doesn't match this exactly is refused,
# never partially trusted as "nonempty is good enough."
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class _DockerLaunchError(Exception):
    """The `docker` executable itself couldn't be launched (OSError) —
    at *any* stage. Always maps to COMMAND_START_FAILURE, regardless of
    which docker subcommand raised it."""


class _DockerControlPlaneFailure(Exception):
    """A bounded, non-streaming Docker control-plane command
    (`create`/`inspect`/`rm`/`ps -a`) launched successfully but then
    failed in some categorical way after launch — a timeout, an
    output-bound overflow, a monitoring failure, an unconfirmed
    termination, or an unconfirmed descriptor cleanup — distinct from
    `_DockerLaunchError` (the executable itself never started). Always
    classified as ENVIRONMENT_FAILURE by every caller, the same
    treatment this module already gave an inspect/listing failure
    before this slice."""


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


def _run_docker(*args: str, limit: int, timeout: float = _DOCKER_CONTROL_PLANE_TIMEOUT_SECONDS):
    """The single seam for every non-streaming Docker control-plane
    command (`create`/`inspect`/`rm`/`ps -a`): runs through the shared
    bounded subprocess runner (`codeagent._bounded_subprocess.
    run_bounded_stdout`), never a shell. That runner's own monotonic
    deadline governs monitoring, reading, and waiting only *after* the
    `docker` process is launched — it cannot and does not bound the
    synchronous launch call itself. Returns a `BoundedProcessResult`
    (raw stdout bytes, confirmed exit code) for any exit code,
    including nonzero — never raises for a nonzero exit. Raises
    `_DockerLaunchError` for a bare launch failure (unchanged mapping
    from before this slice); every other categorical failure the
    shared runner can raise (a monitoring failure, a timeout, an
    output-bound overflow, an unconfirmed termination, or an
    unconfirmed descriptor cleanup) becomes `_DockerControlPlaneFailure`
    uniformly."""
    try:
        return run_bounded_stdout(["docker", *args], timeout_seconds=timeout, stdout_limit=limit)
    except BoundedProcessError as exc:
        if exc.reason is BoundedProcessFailure.LAUNCH_FAILED:
            raise _DockerLaunchError(str(exc)) from exc
        raise _DockerControlPlaneFailure(str(exc)) from exc


def _parse_create_id(raw: bytes) -> str | None:
    """Strict full-container-ID parser for a successful (zero-exit)
    `docker create`'s stdout. Accepts exactly 64 lowercase hexadecimal
    ASCII characters followed by exactly one trailing LF (65 bytes
    total) — nothing else. Rejects empty output, a missing trailing
    newline, multiple lines, CRLF, uppercase, an abbreviated ID,
    non-hex characters, embedded NUL, leading/trailing whitespace, and
    trailing data after the newline. Returns `None` for anything else;
    the caller must never truncate, guess at, or otherwise trust a
    partial value. Oversized output never reaches this function at all
    — `_run_docker`'s own `_CREATE_ID_MAX_BYTES` bound rejects it
    first, as `_DockerControlPlaneFailure`."""
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        return None
    candidate_bytes = raw[:-1]
    try:
        candidate = candidate_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _CONTAINER_ID_RE.fullmatch(candidate):
        return None
    return candidate


def _parse_cleanup_listing(raw: bytes) -> tuple[frozenset[str], frozenset[str]] | None:
    """Strictly parse `docker ps -a --no-trunc --format
    '{{.ID}}\\t{{.Names}}'`'s stdout: one ID/name record per nonempty
    line, every ID exactly 64 lowercase hexadecimal characters, every
    name matching Docker's own container-name grammar exactly
    (`_CONTAINER_NAME_RE`) -- not merely nonempty. Returns `None` for a
    decode failure, a malformed row (not exactly two tab-separated
    fields, a malformed ID, or a name containing whitespace, a carriage
    return, NUL, non-ASCII characters, or leading punctuation), or a
    duplicate ID/name (an ambiguous listing is never partially trusted)
    — the caller treats that identically to a listing failure: cleanup
    unconfirmed. Returns `(ids, names)` on a fully well-formed listing,
    empty sets included."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    ids: set[str] = set()
    names: set[str] = set()
    for line in text.split("\n"):
        if line == "":
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            return None
        id_field, name_field = parts
        if not _CONTAINER_ID_RE.fullmatch(id_field):
            return None
        if not _CONTAINER_NAME_RE.fullmatch(name_field):
            return None
        if id_field in ids or name_field in names:
            return None
        ids.add(id_field)
        names.add(name_field)
    return frozenset(ids), frozenset(names)


@dataclass(frozen=True)
class DockerVerifierLifecycleContext:
    """Opt-in lifecycle-aware context (Milestone 3 Slice 3B-6, ADR 0004
    Amendment 6). When supplied to `DockerVerifier`, container naming
    becomes deterministic per role (`codeagent-baseline-<lifecycle_id>`/
    `codeagent-verification-<lifecycle_id>`), the four ADR 0004 section 7
    labels are set at create time, and every container-attribution
    transition is durably published through `publisher` *before* the
    corresponding Docker mutation — never after.

    Deliberately depends on nothing from `codeagent.lifecycle_store` or
    any other persistence-stack module: `publisher` is typed only as the
    dependency-light `container_lifecycle.ContainerTransitionPublisher`
    Protocol. Matching the existing `checkpoint_session.
    CheckpointTransitionPublisher` precedent, `publisher`'s shape is not
    runtime-checked here (no `@runtime_checkable`/`isinstance`) — only
    Python's own type-checker-level `Protocol` conformance at call
    sites, plus ordinary fail-on-first-use duck typing.

    `lifecycle_id`/`state_root_id` (Slice 3B-6 correction pass) are
    *not* independently supplied constructor fields — accepting them
    that way alongside `publisher` let the two disagree (the publisher
    durably recording lifecycle A's transitions while Docker names/
    labels used lifecycle/state-root B), a real, dangerous mismatch a
    crash could leave unattributable to any reconciler. They are
    instead read-only properties derived from `publisher.lifecycle_id`/
    `publisher.state_root_id` — the *single* source of identity — and
    validated eagerly against the canonical 32-lowercase-hex grammar
    (not merely "nonempty") at construction time, before this context
    can ever be handed to a `DockerVerifier`. The grammar is the same
    one `checkpoint_ref.LIFECYCLE_ID_RE`/`_lifecycle_fs.validate_hex32`
    already enforce for these exact values elsewhere, redefined
    independently in `container_lifecycle.py` so this module never has
    to import that persistence-stack grammar."""

    publisher: container_lifecycle.ContainerTransitionPublisher

    def __post_init__(self) -> None:
        lifecycle_id = self.publisher.lifecycle_id
        state_root_id = self.publisher.state_root_id
        if not container_lifecycle.is_valid_hex32(lifecycle_id):
            raise ValueError(
                f"publisher.lifecycle_id must be exactly 32 lowercase hex characters, got {lifecycle_id!r}"
            )
        if not container_lifecycle.is_valid_hex32(state_root_id):
            raise ValueError(
                f"publisher.state_root_id must be exactly 32 lowercase hex characters, got {state_root_id!r}"
            )

    @property
    def lifecycle_id(self) -> str:
        return self.publisher.lifecycle_id

    @property
    def state_root_id(self) -> str:
        return self.publisher.state_root_id


@dataclass(frozen=True)
class _CreateOutcome:
    """The outcome of `DockerVerifier._docker_create` alone — strictly
    the `docker create` call, never `start`/`inspect`. `container_id` is
    set only on a fully validated success; every failure shape (launch
    failure, control-plane failure, nonzero exit, or a malformed/
    untrustworthy id on an otherwise-zero exit) is reported via
    `outcome`/`error` instead, with `container_id` left `None`."""

    container_id: str | None
    outcome: events.VerificationOutcome | None
    error: OperationalError | None


@dataclass(frozen=True)
class _RecoveryResult:
    """The outcome of recovering one already-existing, proven-owned
    occupant container: `disposition` is always one of
    `_CleanupDisposition.CONFIRMED_ABSENT`/`UNCONFIRMED_DEFERRED` —
    never `NOT_APPLICABLE`/`REQUIRES_ID_CLEANUP`, since this function is
    only ever invoked once a real, owned candidate container's id is
    already in hand. `error is None` is the caller's sole "safe to
    proceed" signal (a fully successful recovery); any non-`None` error
    means the caller must not proceed to create at this name this
    attempt, even when `disposition` also happens to be
    `CONFIRMED_ABSENT` (the "physical absence confirmed, but the ABSENT
    publish itself then failed" row)."""

    disposition: "_CleanupDisposition"
    error: OperationalError | None


@unique
class _CleanupDisposition(str, Enum):
    """Slice 3B-6's unambiguous internal cleanup-disposition carrier,
    replacing the legacy path's own `(create_attempted, confirmed_absent)`
    pair for the lifecycle-aware path only (the legacy `_attempt`/
    `_cleanup_status_for` pairing below is completely unchanged).
    `create_attempted` alone was insufficient here: an occupied-name
    recovery attempt can require deferred cleanup (`UNCONFIRMED_DEFERRED`)
    for *this* invocation's own container even though *this*
    invocation's own `docker create` was never reached
    (`create_attempted=False`) — see the final correction plan's §1/§3."""

    NOT_APPLICABLE = "not_applicable"
    REQUIRES_ID_CLEANUP = "requires_id_cleanup"
    CONFIRMED_ABSENT = "confirmed_absent"
    UNCONFIRMED_DEFERRED = "unconfirmed_deferred"


@dataclass(frozen=True)
class _AttemptResult:
    """Replaces the legacy path's own 8-tuple return shape for the
    lifecycle-aware path only. `create_attempted` is retained purely as
    diagnostic evidence (never drives any decision — `disposition`
    alone does); `container_id` is set only when one genuinely,
    currently exists and is this invocation's own to act on."""

    create_attempted: bool
    container_id: str | None
    disposition: _CleanupDisposition
    outcome: events.VerificationOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    error: OperationalError | None


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
    # `--user` is deliberately absent: it is the effective host identity,
    # computed per verifier by `_effective_container_user()` (ADR 0004
    # Amendment 15) and passed beside these flags at every `docker create`.
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
)


def _effective_container_user() -> str:
    """The `--user` value for every verification container: the effective
    host UID:GID (ADR 0004 Amendment 15). The effective identity is the one
    that creates and owns the deterministic 0700 worktree leaf, so it is the
    only identity guaranteed to read the read-only bind mount; the previous
    fixed `1000:1000` could not read it on a host whose user is not uid 1000.

    Fails closed rather than falling back to any fixed identity: a platform
    without POSIX effective IDs is refused. Effective uid 0 is refused because
    it would make the formerly non-root container process root. Effective gid
    0 is refused too: with a nonzero uid it does not make the process root,
    but it would give the process the root-group identity the fixed
    `1000:1000` never had.
    """
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    if geteuid is None or getegid is None:
        raise ValueError(
            "verification containers require POSIX effective user and group IDs; "
            "this platform is unsupported"
        )
    uid, gid = geteuid(), getegid()
    if uid == 0:
        raise ValueError("verification containers refuse effective uid 0")
    if gid == 0:
        raise ValueError(
            "verification containers refuse effective gid 0 (the root group)"
        )
    return f"{uid}:{gid}"


class DockerVerifier:
    def __init__(
        self,
        worktree_path,
        *,
        image: str = DEFAULT_IMAGE,
        command: tuple[str, ...] = DEFAULT_COMMAND,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Clock | None = None,
        lifecycle_context: DockerVerifierLifecycleContext | None = None,
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
        self._container_user = _effective_container_user()
        self._image = image
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._clock = clock or SystemClock()
        self._error_id_seq = 0
        # Slice 3B-6: opt-in only. `None` (the default) means every
        # existing behavior below is completely unchanged — the legacy
        # `_attempt`/`_execute`/`_cleanup` methods, untouched by this
        # slice, remain the sole code path.
        self._lifecycle_context = lifecycle_context

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def run_baseline(self) -> VerificationResult:
        return self._execute("baseline", role=container_lifecycle.ContainerRole.BASELINE)

    def run(self, attempt_index: int) -> VerificationResult:
        return self._execute(f"verify-{attempt_index}", role=container_lifecycle.ContainerRole.VERIFICATION)

    def _next_error_id(self, label: str) -> str:
        self._error_id_seq += 1
        return f"docker-{label}-err-{self._error_id_seq}"

    def _error(self, code: ErrorCode, label: str, message: str) -> OperationalError:
        return OperationalError(code=code, error_id=self._next_error_id(label), message=message)

    def _attempt(
        self, name: str, label: str
    ) -> tuple[
        bool,
        bool,
        events.VerificationOutcome,
        int | None,
        str,
        str,
        OperationalError | None,
        str | None,
    ]:
        """Runs create -> start -> inspect for one container and returns
        (create_attempted, created, outcome, exit_code, stdout, stderr,
        error, container_id) — a provisional result. Never raises: every
        failure mode here is turned into a returned outcome instead of
        propagating, so the caller's cleanup step always runs regardless
        of what happened here, and never has to guess whether a
        container might exist from inside an exception handler.

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

        `container_id` is populated only once `docker create` both
        returns zero *and* its stdout strictly parses as a full,
        validated container ID (Slice 3B-4, `_parse_create_id`) — never
        trusted from a nonzero create result, and never a truncated or
        best-effort value. Every operation after a successful create
        (`start`/`inspect`) targets that immutable ID, never the
        mutable generated name.
        """
        create_attempted = False
        try:
            create_attempted = True
            create_result = _run_docker(
                "create",
                "--name",
                name,
                *_SECURITY_FLAGS,
                "--user",
                self._container_user,
                "--mount",
                f"type=bind,source={self._worktree_path},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                self._image,
                *self._command,
                limit=_CREATE_ID_MAX_BYTES,
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
                None,
            )
        except _DockerControlPlaneFailure:
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
                None,
            )
        if create_result.returncode != 0:
            # Never trust or act on stdout from a nonzero create — even
            # if it happens to look ID-shaped.
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
                None,
            )

        container_id = _parse_create_id(create_result.stdout)
        if container_id is None:
            # Docker committed to creating a container (exit 0), but its
            # own reported identity cannot be trusted — the container is
            # never started. Cleanup must still be attempted by the
            # exact generated name, since a real container may exist.
            return (
                create_attempted,
                True,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                None,
                "",
                "",
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "the verification container's created ID could not be validated",
                ),
                None,
            )

        try:
            exit_code, status, oom_killed, stdout_text, stderr_text, timed_out = (
                self._start_and_inspect(container_id)
            )
        except _DockerLaunchError:
            # `_start_and_inspect` only ever raises this for `docker
            # start --attach`'s own launch (its inspect step folds a
            # `_DockerControlPlaneFailure` into a "malformed
            # inspection" return instead of raising — see its own
            # docstring), so this is the one exception it can propagate.
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
                container_id,
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
                container_id,
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
                container_id,
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
                container_id,
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
                container_id,
            )
        return (
            create_attempted,
            True,
            events.VerificationOutcome.TEST_FAILURE,
            exit_code,
            stdout_text,
            stderr_text,
            None,
            container_id,
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

    def _execute(
        self, label: str, *, role: container_lifecycle.ContainerRole | None = None
    ) -> VerificationResult:
        start = self._clock.monotonic()

        if self._lifecycle_context is None:
            # Legacy path: completely unchanged from before Slice 3B-6.
            # `role` is accepted but ignored here — it is meaningful
            # only once a lifecycle context is actually supplied.
            name = f"{CONTAINER_NAME_PREFIX}{label}-{uuid4().hex[:12]}"

            create_attempted, _created, outcome, exit_code, stdout_text, stderr_text, error, container_id = (
                self._attempt(name, label)
            )

            # Cleanup confirmation runs unconditionally whenever creation
            # was attempted — regardless of whether create itself failed,
            # a launch failure occurred, or the container ran to completion
            # — since any of those can leave a real container object
            # behind. Only a genuine "never attempted" case skips it.
            confirmed_absent = self._cleanup(name, container_id) if create_attempted else True
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

        return self._execute_lifecycle_aware(label, role, start)

    def _execute_lifecycle_aware(
        self, label: str, role: container_lifecycle.ContainerRole, start: float
    ) -> VerificationResult:
        """Milestone 3 Slice 3B-6: the lifecycle-aware counterpart to the
        legacy branch above. Never calls the legacy `_cleanup()` method
        and never issues `docker rm` by name — every mutation this
        branch performs targets an immutable, freshly validated
        container id, and every attribution transition is durably
        published *before* the Docker mutation it authorizes."""
        assert role is not None
        ctx = self._lifecycle_context
        assert ctx is not None
        name = container_lifecycle.deterministic_container_name(role=role, lifecycle_id=ctx.lifecycle_id)

        attempt = self._attempt_lifecycle_aware(name, label, role, ctx)

        disposition = attempt.disposition
        final_error = attempt.error
        absent_publish_failed = False

        if disposition is _CleanupDisposition.REQUIRES_ID_CLEANUP:
            assert attempt.container_id is not None
            try:
                ctx.publisher.publish(
                    role=role, intent=container_lifecycle.ContainerIntent.REMOVING, id=attempt.container_id
                )
            except container_lifecycle.ContainerPublicationError as exc:
                # The container is not removed: no mutation is
                # authorized without a confirmed write-ahead publish.
                disposition = _CleanupDisposition.UNCONFIRMED_DEFERRED
                final_error = self._publication_error(label, exc)
            else:
                confirmed_absent = self._cleanup_by_id(attempt.container_id, name)
                if confirmed_absent:
                    try:
                        ctx.publisher.publish(
                            role=role, intent=container_lifecycle.ContainerIntent.ABSENT, id=None
                        )
                    except container_lifecycle.ContainerPublicationError as exc:
                        disposition = _CleanupDisposition.CONFIRMED_ABSENT
                        absent_publish_failed = True
                        final_error = self._publication_error(label, exc)
                    else:
                        disposition = _CleanupDisposition.CONFIRMED_ABSENT
                else:
                    disposition = _CleanupDisposition.UNCONFIRMED_DEFERRED
                    final_error = self._error(
                        ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                        label,
                        "verification container could not be confirmed removed",
                    )

        cleanup_status = {
            _CleanupDisposition.NOT_APPLICABLE: events.ContainerCleanupStatus.NOT_APPLICABLE,
            _CleanupDisposition.UNCONFIRMED_DEFERRED: events.ContainerCleanupStatus.UNCONFIRMED,
            _CleanupDisposition.CONFIRMED_ABSENT: events.ContainerCleanupStatus.CONFIRMED_ABSENT,
        }[disposition]

        duration = self._clock.monotonic() - start

        if cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED:
            # Identical dominance rule to the legacy path: overrides any
            # provisional outcome. The underlying cause (a publication
            # failure or an unconfirmed removal) is never placed in the
            # public error — only this fixed, sanitized message.
            return VerificationResult(
                outcome=events.VerificationOutcome.ENVIRONMENT_FAILURE,
                exit_code=None,
                duration_seconds=duration,
                stdout=attempt.stdout,
                stderr=attempt.stderr,
                cleanup_status=cleanup_status,
                error=self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "verification container could not be confirmed removed",
                ),
            )

        if absent_publish_failed:
            # The one new dominance check (Slice 3B-6): physical absence
            # was already confirmed, but the ABSENT publish itself then
            # failed — never report a would-be PASSED/TEST_FAILURE here.
            return VerificationResult(
                outcome=events.VerificationOutcome.ENVIRONMENT_FAILURE,
                exit_code=None,
                duration_seconds=duration,
                stdout=attempt.stdout,
                stderr=attempt.stderr,
                cleanup_status=events.ContainerCleanupStatus.CONFIRMED_ABSENT,
                error=final_error,
            )

        return VerificationResult(
            outcome=attempt.outcome,
            exit_code=attempt.exit_code,
            duration_seconds=duration,
            stdout=attempt.stdout,
            stderr=attempt.stderr,
            cleanup_status=cleanup_status,
            error=attempt.error,
        )

    def _publication_error(
        self, label: str, exc: container_lifecycle.ContainerPublicationError
    ) -> OperationalError:
        """Never places `exc.reason`/`exc.message` (persistence-layer
        detail) in the public error — only this fixed, sanitized
        message, matching every other Docker-failure branch in this
        module."""
        return self._error(
            ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
            label,
            "a lifecycle projection transition could not be published",
        )

    def _attempt_lifecycle_aware(
        self,
        name: str,
        label: str,
        role: container_lifecycle.ContainerRole,
        ctx: "DockerVerifierLifecycleContext",
    ) -> _AttemptResult:
        """Milestone 3 Slice 3B-6's full ordered algorithm: publish
        `CREATING` write-ahead, prove and recover any occupied name,
        create, publish `PRESENT(id)`, start/inspect. Every branch that
        returns before a container is known to exist reports
        `container_id=None`; every branch reached after `docker create`
        both succeeds and is confirmed by `_parse_create_id` reports the
        real, validated id."""
        try:
            ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.CREATING, id=None)
        except container_lifecycle.ContainerPublicationError as exc:
            return _AttemptResult(
                False, None, _CleanupDisposition.NOT_APPLICABLE,
                events.VerificationOutcome.ENVIRONMENT_FAILURE, None, "", "",
                self._publication_error(label, exc),
            )

        try:
            name_to_id, _id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
        except docker_ownership.DockerListingError:
            # Binding correction: a pre-create listing failure is
            # `NOT_APPLICABLE`, not `UNCONFIRMED` — nothing this
            # invocation created could possibly exist yet (its own
            # `docker create` has not been reached), so there is
            # nothing of this invocation's own to report as unconfirmed
            # cleanup, even though occupancy itself could not be
            # determined.
            return _AttemptResult(
                False, None, _CleanupDisposition.NOT_APPLICABLE,
                events.VerificationOutcome.ENVIRONMENT_FAILURE, None, "", "",
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "failed to inspect for an occupied verification container name",
                ),
            )

        candidate_id = name_to_id.get(name)
        if candidate_id is not None:
            recovery = self._resolve_occupied_candidate(candidate_id, name, label, role, ctx)
            if recovery.error is not None:
                return _AttemptResult(
                    False, None, recovery.disposition,
                    events.VerificationOutcome.ENVIRONMENT_FAILURE, None, "", "",
                    recovery.error,
                )
            # The name is now genuinely, durably free -- but the
            # projection's own attribution just collapsed all the way
            # back to ABSENT (the recovered occupant's own
            # REMOVING->ABSENT edge). There is no direct ABSENT->PRESENT
            # edge -- the live-owner table requires ABSENT->CREATING
            # first -- so this invocation must re-publish its own
            # CREATING before it can ever publish PRESENT for its own
            # container below. A failure here is classified identically
            # to the very first CREATING-publish failure above: nothing
            # of this invocation's own doing exists yet (the prior
            # occupant is already confirmed removed).
            try:
                ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.CREATING, id=None)
            except container_lifecycle.ContainerPublicationError as exc:
                return _AttemptResult(
                    False, None, _CleanupDisposition.NOT_APPLICABLE,
                    events.VerificationOutcome.ENVIRONMENT_FAILURE, None, "", "",
                    self._publication_error(label, exc),
                )

        create_attempted = True
        create_outcome = self._docker_create_for_role(name, label, role, ctx)
        if create_outcome.container_id is None:
            return self._recover_after_uncertain_create(
                name, label, role, ctx, create_outcome.outcome, create_outcome.error
            )

        container_id = create_outcome.container_id

        try:
            ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.PRESENT, id=container_id)
        except container_lifecycle.ContainerPublicationError as exc:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.UNCONFIRMED_DEFERRED,
                events.VerificationOutcome.ENVIRONMENT_FAILURE, None, "", "",
                self._publication_error(label, exc),
            )

        try:
            exit_code, status, oom_killed, stdout_text, stderr_text, timed_out = self._start_and_inspect(
                container_id
            )
        except _DockerLaunchError:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
                events.VerificationOutcome.COMMAND_START_FAILURE, None, "", "",
                self._error(
                    ErrorCode.EXECUTOR_COMMAND_START_FAILED, label, "docker executable could not be launched"
                ),
            )

        if timed_out:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
                events.VerificationOutcome.TIMEOUT, None, stdout_text, stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_TIMEOUT, label, "verification container exceeded its time budget"
                ),
            )
        if status != "exited" or exit_code is None or oom_killed is None:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
                events.VerificationOutcome.ENVIRONMENT_FAILURE, None, stdout_text, stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "failed to inspect the verification container's final state",
                ),
            )
        if oom_killed:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
                events.VerificationOutcome.ENVIRONMENT_FAILURE, exit_code, stdout_text, stderr_text,
                self._error(
                    ErrorCode.EXECUTOR_OOM_KILLED,
                    label,
                    "verification container was killed for exceeding its memory limit",
                ),
            )
        if exit_code == 0:
            return _AttemptResult(
                create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
                events.VerificationOutcome.PASSED, 0, stdout_text, stderr_text, None,
            )
        return _AttemptResult(
            create_attempted, container_id, _CleanupDisposition.REQUIRES_ID_CLEANUP,
            events.VerificationOutcome.TEST_FAILURE, exit_code, stdout_text, stderr_text, None,
        )

    def _docker_create_for_role(
        self,
        name: str,
        label: str,
        role: container_lifecycle.ContainerRole,
        ctx: "DockerVerifierLifecycleContext",
    ) -> _CreateOutcome:
        """`docker create` with the four ADR 0004 section 7 labels set
        (and no `attempt` label) — the lifecycle-aware counterpart to
        `_docker_create`, which the legacy path does not use."""
        label_flags: list[str] = []
        for key, value in container_lifecycle.required_labels(
            state_root_id=ctx.state_root_id, lifecycle_id=ctx.lifecycle_id, role=role
        ).items():
            label_flags.extend(["--label", f"{key}={value}"])
        try:
            create_result = _run_docker(
                "create",
                "--name",
                name,
                *_SECURITY_FLAGS,
                "--user",
                self._container_user,
                *label_flags,
                "--mount",
                f"type=bind,source={self._worktree_path},target=/workspace,readonly",
                "--workdir",
                "/workspace",
                self._image,
                *self._command,
                limit=_CREATE_ID_MAX_BYTES,
            )
        except _DockerLaunchError:
            return _CreateOutcome(
                None,
                events.VerificationOutcome.COMMAND_START_FAILURE,
                self._error(
                    ErrorCode.EXECUTOR_COMMAND_START_FAILED, label, "docker executable could not be launched"
                ),
            )
        except _DockerControlPlaneFailure:
            return _CreateOutcome(
                None,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE, label, "failed to create the verification container"
                ),
            )
        if create_result.returncode != 0:
            return _CreateOutcome(
                None,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE, label, "failed to create the verification container"
                ),
            )
        container_id = _parse_create_id(create_result.stdout)
        if container_id is None:
            return _CreateOutcome(
                None,
                events.VerificationOutcome.ENVIRONMENT_FAILURE,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "the verification container's created ID could not be validated",
                ),
            )
        return _CreateOutcome(container_id, None, None)

    def _resolve_occupied_candidate(
        self,
        candidate_id: str,
        name: str,
        label: str,
        role: container_lifecycle.ContainerRole,
        ctx: "DockerVerifierLifecycleContext",
    ) -> _RecoveryResult:
        """Prove ownership of whatever currently occupies the
        deterministic name before ever touching it. A foreign or
        unproven candidate is refused outright (`NOT_APPLICABLE` — no
        mutation of any kind is authorized); only a proven-owned
        candidate is ever passed to `_recover_owned_occupant`."""
        try:
            proof = docker_ownership.docker_inspect_ownership(candidate_id)
        except docker_ownership.DockerInspectError:
            # Binding correction: a pre-create inspection failure is
            # also `NOT_APPLICABLE` — this invocation still has not
            # touched anything of its own (no mutation is authorized on
            # an unproven candidate either way).
            return _RecoveryResult(
                _CleanupDisposition.NOT_APPLICABLE,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "the occupying verification container could not be inspected",
                ),
            )
        if not (
            proof.id == candidate_id
            and proof.name == name
            and container_lifecycle.labels_match(
                proof.labels, state_root_id=ctx.state_root_id, lifecycle_id=ctx.lifecycle_id, role=role
            )
        ):
            return _RecoveryResult(
                _CleanupDisposition.NOT_APPLICABLE,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "a foreign container occupies the verification container name",
                ),
            )
        return self._recover_owned_occupant(candidate_id, name, label, role, ctx)

    def _recover_owned_occupant(
        self,
        candidate_id: str,
        name: str,
        label: str,
        role: container_lifecycle.ContainerRole,
        ctx: "DockerVerifierLifecycleContext",
    ) -> _RecoveryResult:
        """`CREATING -> PRESENT(id) -> REMOVING(id) -> rm(id) ->
        confirmed absence -> ABSENT` for one already-existing, proven-
        owned occupant. Every publish happens strictly before its
        corresponding Docker mutation; any publication failure stops
        this recovery before the next mutation, leaving the candidate
        untouched (still present) at that point."""
        try:
            ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.PRESENT, id=candidate_id)
        except container_lifecycle.ContainerPublicationError as exc:
            return _RecoveryResult(_CleanupDisposition.UNCONFIRMED_DEFERRED, self._publication_error(label, exc))

        try:
            ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.REMOVING, id=candidate_id)
        except container_lifecycle.ContainerPublicationError as exc:
            return _RecoveryResult(_CleanupDisposition.UNCONFIRMED_DEFERRED, self._publication_error(label, exc))

        confirmed_absent = self._cleanup_by_id(candidate_id, name)
        if not confirmed_absent:
            return _RecoveryResult(
                _CleanupDisposition.UNCONFIRMED_DEFERRED,
                self._error(
                    ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    label,
                    "the occupying verification container could not be confirmed removed",
                ),
            )

        try:
            ctx.publisher.publish(role=role, intent=container_lifecycle.ContainerIntent.ABSENT, id=None)
        except container_lifecycle.ContainerPublicationError as exc:
            return _RecoveryResult(_CleanupDisposition.CONFIRMED_ABSENT, self._publication_error(label, exc))

        return _RecoveryResult(_CleanupDisposition.CONFIRMED_ABSENT, None)

    def _recover_after_uncertain_create(
        self,
        name: str,
        label: str,
        role: container_lifecycle.ContainerRole,
        ctx: "DockerVerifierLifecycleContext",
        failure_outcome: events.VerificationOutcome | None,
        failure_error: OperationalError | None,
    ) -> _AttemptResult:
        """Fresh, independent, read-only observation only — never calls
        `docker create` or removes anything by name; only ever removes
        a freshly-proven candidate by its immutable id. Even a fully
        successful recovery does not retroactively succeed *this*
        attempt: the original `docker create` call that led here still
        failed to produce a trustworthy container for this invocation —
        `failure_outcome`/`failure_error` (this invocation's own
        create-failure) are always what is ultimately reported unless
        the recovery itself also failed, in which case the recovery's
        own error dominates."""
        try:
            name_to_id, _id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
        except docker_ownership.DockerListingError:
            return _AttemptResult(
                True, None, _CleanupDisposition.UNCONFIRMED_DEFERRED,
                failure_outcome, None, "", "", failure_error,
            )

        candidate_id = name_to_id.get(name)
        if candidate_id is None:
            # Binding correction: a fresh strict observation confirming
            # absence is `CONFIRMED_ABSENT` — even though the lifecycle
            # projection conservatively remains `CREATING` (this
            # observation is a point-in-time claim, not the ADR's
            # stronger "nothing was ever created for this attempt"
            # precondition the live-owner's own `CREATING->ABSENT` edge
            # requires, so no publish is ever attempted here). Publicly,
            # though, there is genuinely nothing left to clean up.
            return _AttemptResult(
                True, None, _CleanupDisposition.CONFIRMED_ABSENT,
                failure_outcome, None, "", "", failure_error,
            )

        try:
            proof = docker_ownership.docker_inspect_ownership(candidate_id)
        except docker_ownership.DockerInspectError:
            return _AttemptResult(
                True, None, _CleanupDisposition.UNCONFIRMED_DEFERRED,
                failure_outcome, None, "", "", failure_error,
            )

        if not (
            proof.id == candidate_id
            and proof.name == name
            and container_lifecycle.labels_match(
                proof.labels, state_root_id=ctx.state_root_id, lifecycle_id=ctx.lifecycle_id, role=role
            )
        ):
            # Binding correction: a foreign/conflicting candidate here
            # is `UNCONFIRMED`, not `NOT_APPLICABLE` — this invocation's
            # own `docker create` genuinely was entered (even though it
            # failed), so "nothing to clean up" is no longer a safe
            # claim once a real occupant is present at the name.
            return _AttemptResult(
                True, None, _CleanupDisposition.UNCONFIRMED_DEFERRED,
                failure_outcome, None, "", "", failure_error,
            )

        recovery = self._recover_owned_occupant(candidate_id, name, label, role, ctx)
        return _AttemptResult(
            True, None, recovery.disposition, failure_outcome, None, "", "",
            recovery.error if recovery.error is not None else failure_error,
        )

    def _cleanup_by_id(self, container_id: str, name: str) -> bool:
        """id-only removal (Slice 3B-6): `docker rm --force <id>`, never
        a name, then independent re-observation of both that id and the
        deterministic `name` it was expected to occupy. Never called
        from the legacy path — `_cleanup` (name-fallback-capable) is
        that path's own, completely unchanged, method."""
        try:
            _run_docker("rm", "--force", container_id, limit=_RM_OUTPUT_MAX_BYTES)
        except (_DockerLaunchError, _DockerControlPlaneFailure):
            # rm's own outcome is never authoritative for removal — the
            # independent listing below is.
            pass

        try:
            name_to_id, id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
        except docker_ownership.DockerListingError:
            return False
        if container_id in id_to_name:
            return False
        if name in name_to_id:
            return False
        return True

    def _start_and_inspect(
        self, container_id: str
    ) -> tuple[int | None, str | None, bool | None, str, str, bool]:
        """`docker start --attach` and the final-state `docker inspect`
        both target `container_id` (Slice 3B-4) — the immutable
        identity Docker itself confirmed at create time, never the
        mutable generated name. Only `docker start`'s own launch
        failure (`_DockerLaunchError`) ever propagates out of this
        method; a `_DockerLaunchError` or `_DockerControlPlaneFailure`
        from the inspect step is folded into a malformed-inspection
        return (`None, None, None, ...`) instead, the same treatment a
        malformed or unparseable inspect payload already gets."""
        stdout_collector = _BoundedCollector(_MAX_STREAM_BYTES)
        stderr_collector = _BoundedCollector(_MAX_STREAM_BYTES)
        try:
            proc = subprocess.Popen(
                ["docker", "start", "--attach", container_id],
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
            inspect_result = _run_docker(
                "inspect", "--format", "{{json .State}}", container_id, limit=_INSPECT_STATE_MAX_BYTES
            )
        except (_DockerLaunchError, _DockerControlPlaneFailure):
            return None, None, None, stdout_text, stderr_text, False
        if inspect_result.returncode != 0:
            return None, None, None, stdout_text, stderr_text, False

        try:
            inspect_text = inspect_result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None, None, None, stdout_text, stderr_text, False

        exit_code, status, oom_killed = self._parse_state(inspect_text)
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

    def _cleanup(self, name: str, container_id: str | None) -> bool:
        """Best-effort removal, followed by a genuine confirmation check
        via one fresh, unfiltered, `--no-trunc` `docker ps -a` listing —
        a cleanup *attempt* is not a cleanup *guarantee*. Returns True
        only if that listing strictly parses and shows both the exact
        generated `name` absent and, when `container_id` was validated,
        that exact ID also absent.

        Removal itself targets `container_id` when one is available
        (Slice 3B-4: the immutable identity Docker itself confirmed at
        create time) rather than the mutable `name`, so a rename or
        name-reuse race cannot redirect this mutation to a different
        container. Before a valid ID exists — a launch failure, a
        control-plane failure, a nonzero create, or a malformed create
        ID — removal falls back to the exact generated name, matching
        this module's existing conservative behavior.

        Deliberately does not use `docker inspect <target>`'s exit code
        as the confirmation signal: a nonzero exit there is ambiguous
        between "confirmed gone" and "the daemon/client itself is
        broken" — both produce the same exit code, and treating both
        as confirmation risked reporting a successful run whose
        container secretly still existed. A strictly parsed dual-
        identity listing removes that ambiguity: a failed, malformed,
        or unparseable listing is always treated as unconfirmed, never
        as confirmed-absent, and so is a listing in which either the
        name or the ID (when one was validated) still appears —
        including the ID appearing live under a different name.
        """
        rm_target = container_id if container_id is not None else name
        try:
            _run_docker("rm", "--force", rm_target, limit=_RM_OUTPUT_MAX_BYTES)
        except (_DockerLaunchError, _DockerControlPlaneFailure):
            # rm's own outcome is never authoritative for removal — the
            # independent listing below is — so a failure here still
            # proceeds to that listing rather than short-circuiting.
            pass

        try:
            listing = _run_docker(
                "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}", limit=_CLEANUP_LISTING_MAX_BYTES
            )
        except (_DockerLaunchError, _DockerControlPlaneFailure):
            return False
        if listing.returncode != 0:
            return False

        parsed = _parse_cleanup_listing(listing.stdout)
        if parsed is None:
            return False
        ids, names = parsed
        if name in names:
            return False
        if container_id is not None and container_id in ids:
            return False
        return True
