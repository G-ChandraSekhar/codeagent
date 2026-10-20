"""Cooperative cancellation (ADR 0005, Amendment 1; ledger D3).

A `CancellationSource` holds the run's cancellation state; components
receive only its read-only `CancellationToken`. The production signal
handler (`cli.owned_cancellation()`, the only module that calls
`signal.signal`) performs one `request()`; nothing here does I/O,
logging, locking, Docker, or Git.

**Main thread only (Amendment 1 section 2).** The source is created on,
and accepts requests only from, the main thread. A request from any other
thread raises `CancellationThreadError` and changes nothing. Thread-safe
or web cancellation is deferred to the deliverable that first adds a web
entrypoint.

**The winner is the first committed request**: the first `append` to
execute, which is not necessarily the first `request()` entered. A signal
handler can run only between an outer request's emptiness check and its
append; the handler's append then commits first and its reason wins, and
the outer request appends a second candidate that never changes `reason`.
Sequential requests never add a candidate, so the list grows only with
re-entrancy depth inside that window; no fixed numeric bound is claimed.

`requests_received` is an opt-in diagnostic (`record_requests=True`, tests
and the test-only driver only): one atomic append per `request()` call,
never consulted for state or the winning reason. Standard POSIX signals
can coalesce, so it counts delivered requests, not signals sent.
"""

from __future__ import annotations

import subprocess
import threading
import time
from enum import Enum, unique


@unique
class CancellationThreadFailure(str, Enum):
    NOT_MAIN_THREAD = "not_main_thread"
    OFF_THREAD_REQUEST = "off_thread_request"


class CancellationThreadError(Exception):
    """A source was constructed, or a request was made, off the main thread."""

    def __init__(self, reason: CancellationThreadFailure) -> None:
        super().__init__(f"cancellation is main-thread only ({reason.value})")
        self.reason = reason


class OperationCancelled(Exception):
    """Raised by a cancellable collaborator (today: an approval provider)
    that stopped waiting because its token was cancelled. The controller
    maps it to `CANCELLED` only around `approval.decide()`; anywhere else
    it is an ordinary unexpected exception."""


class CancellationToken:
    """Read-only view of a source. `NEVER_CANCELLED` is the token with no
    source. Components only ever call `is_cancelled()`."""

    __slots__ = ("_source",)

    def __init__(self, source: "CancellationSource | None") -> None:
        self._source = source

    def is_cancelled(self) -> bool:
        return self._source is not None and self._source.is_cancelled()

    @property
    def reason(self) -> str | None:
        return None if self._source is None else self._source.reason


NEVER_CANCELLED = CancellationToken(None)


class CancellationSource:
    def __init__(self, *, record_requests: bool = False) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise CancellationThreadError(CancellationThreadFailure.NOT_MAIN_THREAD)
        self._owner = threading.main_thread().ident
        self._reasons: list[str] = []
        self._received: list[None] | None = [] if record_requests else None
        self.token = CancellationToken(self)

    def request(self, reason: str) -> None:
        # Safe from a signal handler: no lock, no I/O, one C-level append
        # each. Never a read-modify-write.
        if threading.get_ident() != self._owner:
            raise CancellationThreadError(CancellationThreadFailure.OFF_THREAD_REQUEST)
        if self._received is not None:
            self._received.append(None)
        if not self._reasons:
            self._reasons.append(reason)

    def is_cancelled(self) -> bool:
        return bool(self._reasons)

    @property
    def reason(self) -> str | None:
        return self._reasons[0] if self._reasons else None

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(self._reasons)

    @property
    def requests_received(self) -> int:
        if self._received is None:
            raise AttributeError("requests_received is recorded only with record_requests=True")
        return len(self._received)


@unique
class WaitResult(str, Enum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


WAIT_SLICE_SECONDS = 0.1


def wait_cancellable(
    process: subprocess.Popen,
    *,
    timeout: float,
    token: CancellationToken,
    slice_seconds: float = WAIT_SLICE_SECONDS,
    monotonic=time.monotonic,
) -> WaitResult:
    """Wait for `process` in slices of at most `slice_seconds`, because a
    flag-only handler does not interrupt a blocking `wait(timeout)`.

    Observed-before-exit (Amendment 1 section 5): an already-exited
    process is reported `EXITED` even if the token is also set, so a
    genuine outcome stands and the caller's post-return check cancels.
    `CANCELLED` means the token was observed while the process was still
    running. Never kills or reaps beyond `wait`; the caller does that."""
    deadline = monotonic() + timeout
    while True:
        if process.poll() is not None:
            return WaitResult.EXITED
        if token.is_cancelled():
            return WaitResult.CANCELLED
        remaining = deadline - monotonic()
        if remaining <= 0:
            return WaitResult.TIMED_OUT
        try:
            process.wait(timeout=min(slice_seconds, remaining))
            return WaitResult.EXITED
        except subprocess.TimeoutExpired:
            continue


CLEANUP_FAILURES_ATTRIBUTE = "codeagent_cleanup_failures"
DEFERRED_INTERRUPT_ATTRIBUTE = "codeagent_deferred_interrupt"


def set_evidence_attribute(exc: BaseException, name: str, value) -> None:
    """Best-effort, never-raising: set one structured-evidence attribute on
    an exception that is about to surface. Any failure of the write --
    `AttributeError`, an ordinary custom exception, or a non-`Exception`
    such as `KeyboardInterrupt` raised by a custom `__setattr__` -- is
    swallowed, so evidence can never replace or alter the chosen primary.
    Never touches `__cause__`/`__context__` and never reads any message. An
    exception type that refuses the attribute simply cannot expose it."""
    try:
        setattr(exc, name, value)
    except BaseException:  # noqa: BLE001 - attachment must never replace the primary
        pass


def attach_cleanup_failures(exc: BaseException, record) -> None:
    """Make a cleanup's other retained failures observable on the surfaced
    exception, by identity and in occurrence order, through one stable
    attribute (`codeagent_cleanup_failures`), appended to, never replaced
    (ADR 0005 Amendment 1 §12). Reading and writing are both guarded against
    every `BaseException`; nothing raised here can reach the caller."""
    if not record:
        return
    try:
        existing = tuple(getattr(exc, CLEANUP_FAILURES_ATTRIBUTE, ()))
    except BaseException:  # noqa: BLE001 - a refusing getattr: start empty
        existing = ()
    try:
        combined = existing + tuple(record)
    except BaseException:  # noqa: BLE001
        return
    set_evidence_attribute(exc, CLEANUP_FAILURES_ATTRIBUTE, combined)
