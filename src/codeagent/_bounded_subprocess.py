"""Milestone 3 Slice 3B-4: shared bounded, non-shell subprocess
execution for control-plane commands whose complete stdout must be
captured within a fixed size and time bound, with confirmed
termination and confirmed descriptor cleanup on every failure path.

Owns the complete child lifecycle (launch, monitor, read, wait, kill,
confirm) behind one function, `run_bounded_stdout`, rather than
exposing separate drain/kill primitives a caller could combine
incorrectly or inconsistently -- the same discipline
`codeagent._git_safety`'s own `_read_bounded` already established for
Git subprocesses, generalized here for any structured-argv command
with no stdin (Docker's control-plane commands today; potentially
others later). `codeagent.reconciliation` is this module's first
caller, replacing its own former private copy of this same logic.

Never uses a shell. Never trusts truncated output as complete: an
oversized read is a categorical failure (`OUTPUT_LIMIT_EXCEEDED`), not
a silent truncation, and no single `os.read()` call ever requests more
than the caller's remaining allowance (`limit + 1 - len(already-read)`)
-- a small `stdout_limit` genuinely bounds how much a hostile or
buggy child can cause this process to read into memory in one
syscall, not merely how much is *retained* afterward. A monitoring
failure, a timeout, or an oversize read always kills the child and
confirms it was reaped -- using a deadline computed fresh at the
moment of the kill attempt, not derived from the original command
deadline, so an early failure on a long-timeout command cannot extend
the effective wait anywhere near that original budget. Every owned
descriptor (the selector, the child's stdout pipe) is also positively
confirmed closed, never silently swallowed: a close failure is its own
categorical `CLEANUP_UNCONFIRMED` outcome, attempted on every path
including an otherwise-successful read. `TERMINATION_UNCONFIRMED`
(process reap) and `CLEANUP_UNCONFIRMED` (descriptor close) are
distinct signals -- the former means the child might still be running,
the latter means it is very likely gone but a description of *that*
could not be confirmed -- and either dominates and is raised explicitly
`from` a fully-formed `BoundedProcessError` representing whatever
failure preceded it, never left to incidental `__context__` and never
the internal-only `_BoundedFailure` signal type. A launch failure (the
executable itself could not be started) is categorically distinct from
every post-launch failure, since there is no process to clean up. A
cancellation-style `BaseException` that is not an ordinary `Exception`
(`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`) is never
translated into a categorical `BoundedProcessError`: cleanup is still
attempted, but the original exception instance propagates unchanged if
cleanup succeeds; if cleanup instead fails, the result is a categorical
`BoundedProcessError` (`TERMINATION_UNCONFIRMED` if the process itself
could not be confirmed reaped, or `CLEANUP_UNCONFIRMED` if reap was
confirmed but an owned descriptor was not) chained from that original
exception. A nonzero exit code is not raised as a failure by
this module -- it is returned as an ordinary `BoundedProcessResult` for
the caller to classify. Failure messages are fixed, sanitized,
categorical text only -- never raw argv, stdout, or filesystem paths.

The deadline this module computes governs monitoring, reading, and
waiting *after* a child process object is returned by `Popen` -- it
does not and cannot interrupt the synchronous `Popen()` call itself
(a slow `fork`/`exec` is not bounded by anything here).
"""

from __future__ import annotations

import math
import os
import selectors
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique

_BOUNDED_READ_CHUNK = 65_536
_KILL_CONFIRM_GRACE_SECONDS = 2.0

# No embedded NUL anywhere in an argv element -- a NUL byte can never
# appear in a real POSIX argv element, so its presence is only ever a
# smuggling attempt or a caller bug, never legitimate content.
_NUL = "\x00"


@dataclass(frozen=True)
class BoundedProcessResult:
    """A confirmed exit code plus the complete raw stdout bytes (never
    text-decoded here -- callers decode and parse with their own
    strict rules). A nonzero `returncode` is not raised as a failure by
    this module; classifying it is the caller's job."""

    returncode: int
    stdout: bytes


@unique
class BoundedProcessFailure(str, Enum):
    LAUNCH_FAILED = "launch_failed"
    MONITORING_FAILED = "monitoring_failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    TERMINATION_UNCONFIRMED = "termination_unconfirmed"
    # A close failure on an owned descriptor (the selector, the
    # child's stdout pipe) -- distinct from TERMINATION_UNCONFIRMED
    # (which means the *process* might still be running): this means
    # the process's own fate is otherwise settled, but a descriptor
    # this module owns could not be confirmed released.
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


class BoundedProcessError(Exception):
    """`reason` is the stable, matchable identifier. `message` is
    fixed, sanitized categorical text only -- never raw argv, stdout,
    or filesystem paths."""

    def __init__(self, reason: BoundedProcessFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class _BoundedFailure(Exception):
    """Internal-only signal from the read/wait helpers to
    `run_bounded_stdout`'s single cleanup-and-raise site. Never escapes
    this module."""

    def __init__(self, reason: BoundedProcessFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


_FAILURE_MESSAGES: dict[BoundedProcessFailure, str] = {
    BoundedProcessFailure.LAUNCH_FAILED: "the executable could not be launched",
    BoundedProcessFailure.MONITORING_FAILED: "the subprocess output could not be monitored",
    BoundedProcessFailure.TIMED_OUT: "the subprocess did not finish within its time limit",
    BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED: (
        "the subprocess produced more output than its fixed safety bound allows"
    ),
    BoundedProcessFailure.TERMINATION_UNCONFIRMED: "the subprocess could not be confirmed terminated",
    BoundedProcessFailure.CLEANUP_UNCONFIRMED: "a subprocess descriptor could not be confirmed closed",
}


def _to_error(reason: BoundedProcessFailure) -> BoundedProcessError:
    return BoundedProcessError(reason, _FAILURE_MESSAGES[reason])


def _validate_call_arguments(
    argv: Sequence[str], *, timeout_seconds: float, stdout_limit: int
) -> list[str]:
    """Materializes and validates every call argument before a deadline
    is computed or `Popen` is ever invoked. Every rejection is a fixed,
    sanitized `ValueError` -- never the caller's actual argv or value,
    and never an incidental `TypeError`/`AttributeError` escaping from
    an unguarded comparison against a wrong-typed value."""
    if isinstance(argv, (str, bytes)):
        raise ValueError("argv must not be a bare str or bytes")
    try:
        materialized = list(argv)
    except TypeError as exc:
        raise ValueError("argv must be an iterable of strings") from exc
    if not materialized:
        raise ValueError("argv must contain at least one element")
    for element in materialized:
        if not isinstance(element, str) or not element:
            raise ValueError("every argv element must be a nonempty string")
        if _NUL in element:
            raise ValueError("no argv element may contain a NUL character")

    if isinstance(stdout_limit, bool) or not isinstance(stdout_limit, int) or stdout_limit <= 0:
        raise ValueError("stdout_limit must be a positive integer")

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")

    return materialized


def _drain(process: subprocess.Popen, *, deadline: float, limit: int) -> bytes:
    """Read at most `limit + 1` bytes of `process.stdout` in total,
    non-blocking, under `deadline` -- each individual `os.read()` call
    is itself capped to the caller's *remaining* allowance
    (`min(_BOUNDED_READ_CHUNK, limit + 1 - len(already-read))`), never
    a flat chunk size regardless of how close to the limit the buffer
    already is. Raises `_BoundedFailure` on a monitoring setup failure,
    a genuine read error, a timeout, or an oversize read -- the caller
    discards the buffer and kills the child in every case; a truncated
    read is never silently trusted as complete. A cancellation-style
    `BaseException` (not a `_BoundedFailure`) reaching this function's
    own read loop is never converted into one -- it still triggers the
    descriptor cleanup below, then propagates unchanged. Every owned
    descriptor (the selector, the child's stdout pipe) is closed on
    every path, attempted regardless of the read outcome; a close
    failure is its own `CLEANUP_UNCONFIRMED` failure, chained from the
    read failure (if any) rather than silently discarding it."""
    try:
        selector = selectors.DefaultSelector()
    except Exception as exc:  # noqa: BLE001 - any setup failure is categorical
        raise _BoundedFailure(BoundedProcessFailure.MONITORING_FAILED) from exc

    buf = bytearray()
    read_failure: BaseException | None = None
    try:
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
        except Exception as exc:  # noqa: BLE001 - any setup failure is categorical
            raise _BoundedFailure(BoundedProcessFailure.MONITORING_FAILED) from exc

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoundedFailure(BoundedProcessFailure.TIMED_OUT)
            if not selector.select(timeout=remaining):
                continue
            # Never request more than the caller's remaining allowance
            # in one syscall: `len(buf) <= limit` is the loop's own
            # invariant on every iteration reached here (the very next
            # statement below raises before looping again once that
            # stops holding), so this is always >= 1.
            to_read = min(_BOUNDED_READ_CHUNK, limit + 1 - len(buf))
            try:
                chunk = os.read(process.stdout.fileno(), to_read)
            except BlockingIOError:
                continue
            except OSError as exc:
                # A genuine I/O error on the pipe is a monitoring
                # failure, not a timeout: the two are observably
                # different (one is a raised OSError, the other is
                # simply no readiness before the deadline) and must
                # not be conflated.
                raise _BoundedFailure(BoundedProcessFailure.MONITORING_FAILED) from exc
            if not chunk:
                break  # EOF: the child closed stdout normally.
            buf += chunk
            if len(buf) > limit:
                raise _BoundedFailure(BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED)
    except BaseException as exc:  # noqa: BLE001 - descriptor cleanup below must still run,
        # including for a cancellation-style BaseException (e.g.
        # KeyboardInterrupt) that is not a _BoundedFailure at all --
        # such an exception is re-raised unchanged below, never
        # converted into a categorical failure by this function.
        read_failure = exc

    close_exc: BaseException | None = None
    try:
        selector.close()
    except Exception as exc:  # noqa: BLE001 - still attempt the other close below
        close_exc = exc
    try:
        process.stdout.close()
    except Exception as exc:  # noqa: BLE001
        close_exc = close_exc if close_exc is not None else exc

    if close_exc is not None:
        raise _BoundedFailure(BoundedProcessFailure.CLEANUP_UNCONFIRMED) from (
            read_failure if read_failure is not None else close_exc
        )
    if read_failure is not None:
        raise read_failure
    return bytes(buf)


def _confirm_exit(process: subprocess.Popen, *, deadline: float) -> None:
    """Confirm the child has already exited (EOF was observed) within
    `deadline`. A child that closes stdout but does not promptly exit
    is classified as a timeout, not silently accepted. Any other
    exception from `wait()` (not a timeout) is still a categorical
    monitoring failure, never a raw exception escaping this function --
    the caller performs the same kill/confirm cleanup regardless of
    which categorical failure this raises."""
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise _BoundedFailure(BoundedProcessFailure.TIMED_OUT) from exc
    except Exception as exc:  # noqa: BLE001 - any other wait failure is still categorical
        raise _BoundedFailure(BoundedProcessFailure.MONITORING_FAILED) from exc


def _terminate_and_confirm(process: subprocess.Popen) -> None:
    """The single abort path for every post-launch failure: kill the
    child, close its stdout, and confirm it was reaped -- within a
    grace window computed fresh from the moment of the kill attempt
    (`time.monotonic() + _KILL_CONFIRM_GRACE_SECONDS`), never derived
    from or extended by the original command deadline, so an early
    failure on a long-timeout command cannot grant a kill-confirmation
    wait anywhere near that original budget.

    Raises `_BoundedFailure(TERMINATION_UNCONFIRMED)` if the process
    itself cannot be confirmed reaped -- this takes precedence over a
    stdout-close failure, since the process's own fate is the more
    consequential unknown. If the process is confirmed reaped but the
    stdout close failed, raises `_BoundedFailure(CLEANUP_UNCONFIRMED)`
    instead. Never returns having silently left the child running or a
    close failure unreported."""
    try:
        process.kill()
    except Exception:  # noqa: BLE001 - still attempt to confirm exit below
        pass

    close_exc: BaseException | None = None
    try:
        if process.stdout is not None:
            process.stdout.close()
    except Exception as exc:  # noqa: BLE001
        close_exc = exc

    confirm_deadline = time.monotonic() + _KILL_CONFIRM_GRACE_SECONDS
    try:
        process.wait(timeout=max(0.0, confirm_deadline - time.monotonic()))
    except Exception as exc:  # noqa: BLE001 - any inability to confirm reap is unconfirmed
        raise _BoundedFailure(BoundedProcessFailure.TERMINATION_UNCONFIRMED) from exc

    if close_exc is not None:
        raise _BoundedFailure(BoundedProcessFailure.CLEANUP_UNCONFIRMED) from close_exc


def run_bounded_stdout(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdout_limit: int,
) -> BoundedProcessResult:
    """Run `argv` (never through a shell, never with stdin) capturing
    at most `stdout_limit + 1` bytes of stdout in total -- never more
    than that in any single underlying read -- under one monotonic
    deadline of `timeout_seconds` covering monitoring, reading, and
    waiting *after* the child process object is returned by `Popen`
    (this deadline cannot and does not bound the synchronous `Popen()`
    call itself). stderr is discarded (`DEVNULL`): nothing that calls
    this function consumes or persists it today, and an unread stderr
    pipe can itself deadlock a child that writes enough to it.

    Every call argument is validated before any deadline is computed or
    any process is launched (see `_validate_call_arguments`); invalid
    input raises `ValueError` with fixed, sanitized text.

    Returns normally for any exit code, including nonzero -- the
    caller classifies that; this function never raises for a nonzero
    exit. Raises `BoundedProcessError` for a launch failure, a
    monitoring setup or read failure, a timeout, an oversized read, or
    an unclosable owned descriptor. Every one of those except a bare
    launch failure (which has no process to clean up) kills the child
    and confirms it was reaped, and confirms its stdout pipe was
    closed, before this function raises. If termination itself cannot
    be confirmed, `BoundedProcessError(TERMINATION_UNCONFIRMED)`
    dominates; if termination is confirmed but a descriptor close
    fails, `BoundedProcessError(CLEANUP_UNCONFIRMED)` dominates instead
    -- either way, raised explicitly `from` a fully-formed
    `BoundedProcessError` representing the original failure, never the
    internal-only `_BoundedFailure` signal type.

    A cancellation-style `BaseException` that is not an ordinary
    `Exception` (`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`) is
    never translated into a categorical `BoundedProcessError`: cleanup
    is still attempted, but if it succeeds, the original exception
    instance propagates completely unchanged. If cleanup instead fails,
    the result is a categorical `BoundedProcessError` chained from that
    original exception -- `TERMINATION_UNCONFIRMED` if the process
    itself could not be confirmed reaped, or `CLEANUP_UNCONFIRMED` if
    reap was confirmed but an owned descriptor could not be confirmed
    closed (see `_terminate_and_confirm`).
    """
    materialized = _validate_call_arguments(
        argv, timeout_seconds=timeout_seconds, stdout_limit=stdout_limit
    )

    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            materialized, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        raise _to_error(BoundedProcessFailure.LAUNCH_FAILED) from exc

    try:
        buf = _drain(process, deadline=deadline, limit=stdout_limit)
        _confirm_exit(process, deadline=deadline)
    except _BoundedFailure as failure:
        original = _to_error(failure.reason)
        # `_drain` itself may already have chained a descriptor-close
        # failure (`CLEANUP_UNCONFIRMED`) `from` a deeper read failure
        # it caught internally (see its own docstring) -- when that
        # inner cause is itself a `_BoundedFailure`, surface it as a
        # fully-formed public `BoundedProcessError` too, so the
        # original read failure is never lost from the public chain.
        inner_cause = (
            _to_error(failure.__cause__.reason)
            if isinstance(failure.__cause__, _BoundedFailure)
            else None
        )
        # Attach the deeper cause to `original` itself, before it is
        # ever used as a `from` target below -- otherwise, if
        # `_terminate_and_confirm` also fails, the further raise
        # (`from original`) would carry an `original` with no
        # `__cause__` of its own, silently losing this inner failure
        # from the complete public chain.
        original.__cause__ = inner_cause
        try:
            _terminate_and_confirm(process)
        except _BoundedFailure as cleanup_failure:
            raise _to_error(cleanup_failure.reason) from original
        raise original from inner_cause
    except Exception as exc:  # noqa: BLE001 - any other ordinary failure is still categorical
        original = _to_error(BoundedProcessFailure.MONITORING_FAILED)
        try:
            _terminate_and_confirm(process)
        except _BoundedFailure as cleanup_failure:
            raise _to_error(cleanup_failure.reason) from original
        raise original from exc
    except BaseException as exc:  # noqa: BLE001 - cancellation-style: never wrapped
        try:
            _terminate_and_confirm(process)
        except _BoundedFailure as cleanup_failure:
            raise _to_error(cleanup_failure.reason) from exc
        raise

    return BoundedProcessResult(returncode=process.returncode, stdout=bytes(buf))
