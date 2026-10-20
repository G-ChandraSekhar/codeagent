"""ADR 0005 Amendment 1 (ledger D3): `codeagent.cancellation` — the
main-thread-only, append-based source (plan section 5.3) and the sliced
cancellable wait (plan section 6.1)."""

from __future__ import annotations

import ast
import inspect
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from codeagent import cancellation as cn
from codeagent.cancellation import (
    NEVER_CANCELLED,
    CancellationSource,
    CancellationThreadError,
    CancellationThreadFailure,
    WaitResult,
    wait_cancellable,
)


def _wait_until(predicate, *, deadline_seconds: float = 5.0) -> None:
    """A bounded wait whose deadline fails the test (never a correctness sleep)."""
    deadline = time.monotonic() + deadline_seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before the deadline")
        time.sleep(0.001)


@pytest.fixture
def sigint_to(request):
    """Install a test SIGINT handler that requests on a given source;
    restore the previous handler afterwards."""
    previous = signal.getsignal(signal.SIGINT)
    holder = {}

    def handler(signum, frame):
        holder["source"].request(holder.get("reason", "signal:SIGINT"))

    signal.signal(signal.SIGINT, handler)
    yield holder
    signal.signal(signal.SIGINT, previous)


# --- serialized requests ---------------------------------------------------


def test_serialized_direct_calls_keep_one_candidate_and_count_each():
    source = CancellationSource(record_requests=True)
    for i in range(10_000):
        source.request(f"r{i}")
    assert source.candidates == ("r0",)
    assert source.reason == "r0"
    assert source.is_cancelled() and source.token.is_cancelled()
    assert source.requests_received == 10_000


def test_serialized_real_os_signals_each_delivered_once(sigint_to):
    source = CancellationSource(record_requests=True)
    sigint_to["source"] = source
    for k in range(1, 51):
        os.kill(os.getpid(), signal.SIGINT)
        _wait_until(lambda: source.requests_received == k)
    assert source.candidates == ("signal:SIGINT",)
    assert source.requests_received == 50


# --- deterministic re-entrancy (first committed request wins) --------------


class _ReentrantList(list):
    """On its first truth evaluation only, runs `hook` (as a signal handler
    interrupting the outer request's check-then-append window would)."""

    def __init__(self, hooks):
        super().__init__()
        self._hooks = list(hooks)

    def __bool__(self):
        # The emptiness result is fixed first: the handler runs *after* the
        # outer request's check and before its append.
        result = len(self) > 0
        if self._hooks:
            self._hooks.pop(0)()
        return result


def test_reentrant_request_inside_the_window_commits_first():
    source = CancellationSource(record_requests=True)
    source._reasons = _ReentrantList([lambda: source.request("inner")])
    source.request("outer")
    assert list(source._reasons) == ["inner", "outer"]
    assert source.reason == "inner"
    assert source.is_cancelled()
    # One count per request() call, never lost under re-entrancy (M33).
    assert source.requests_received == 2


def test_reentrant_real_signal_inside_the_window(sigint_to):
    source = CancellationSource(record_requests=True)
    sigint_to["source"] = source
    sigint_to["reason"] = "inner"

    def deliver():
        os.kill(os.getpid(), signal.SIGINT)
        # len() never calls __bool__, so this cannot re-enter the hook.
        _wait_until(lambda: len(source._reasons) == 1)

    source._reasons = _ReentrantList([deliver])
    source.request("outer")
    assert list(source._reasons) == ["inner", "outer"]
    assert source.reason == "inner"
    assert source.requests_received == 2


def test_two_level_nesting_has_no_fixed_bound():
    source = CancellationSource()

    def middle():
        source._reasons._hooks.append(lambda: source.request("innermost"))
        source.request("middle")

    source._reasons = _ReentrantList([middle])
    source.request("outer")
    assert list(source._reasons) == ["innermost", "middle", "outer"]
    assert source.reason == "innermost"


# --- recording off / diagnostic separation ---------------------------------


def test_recording_off_has_no_count_and_never_grows():
    source = CancellationSource()
    assert not hasattr(source, "requests_received")
    with pytest.raises(AttributeError):
        source.requests_received
    for i in range(10_000):
        source.request(f"r{i}")
    assert source.candidates == ("r0",)
    assert source._received is None


def test_state_never_reads_the_diagnostic():
    """M34: `is_cancelled()`/`reason` come from the candidates only."""
    source = CancellationSource(record_requests=True)
    source._received.extend([None] * 5)
    assert not source.is_cancelled() and source.reason is None
    source._received = None
    source.request("x")
    assert source.is_cancelled() and source.reason == "x"


# --- main thread only ------------------------------------------------------


def _in_thread(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    assert not t.is_alive()
    return box


def test_off_thread_request_is_refused_and_changes_nothing():
    source = CancellationSource(record_requests=True)
    box = _in_thread(lambda: source.request("off"))
    assert isinstance(box["error"], CancellationThreadError)
    assert box["error"].reason is CancellationThreadFailure.OFF_THREAD_REQUEST
    assert not source.is_cancelled()
    assert source.requests_received == 0


def test_off_thread_construction_is_refused():
    box = _in_thread(CancellationSource)
    assert isinstance(box["error"], CancellationThreadError)
    assert box["error"].reason is CancellationThreadFailure.NOT_MAIN_THREAD


def test_request_from_a_handler_while_main_thread_holds_a_lock(sigint_to):
    source = CancellationSource(record_requests=True)
    sigint_to["source"] = source
    lock = threading.Lock()
    with lock:
        os.kill(os.getpid(), signal.SIGINT)
        _wait_until(lambda: source.requests_received == 1)
    assert source.reason == "signal:SIGINT"


def test_request_path_uses_no_lock_event_io_or_logging():
    """M20: AST pin over the whole module (the request path included)."""
    tree = ast.parse(Path(inspect.getfile(cn)).read_text())
    forbidden_attrs = {"Lock", "RLock", "Event", "Condition", "Semaphore", "BoundedSemaphore", "Barrier"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attrs, node.attr
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names} | {getattr(node, "module", None) or ""}
            assert not names & {"logging", "queue", "asyncio"}
    request = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "request")
    for node in ast.walk(request):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print"}


def test_never_cancelled():
    assert not NEVER_CANCELLED.is_cancelled()
    assert NEVER_CANCELLED.reason is None


# --- sliced wait -----------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _ScriptedProc:
    """`exits_after` slices of waiting, then exits. Every wait advances the
    fake clock by its full timeout and records it."""

    def __init__(self, clock, *, exits_after=None, already_exited=False):
        self._clock = clock
        self._left = exits_after
        self.returncode = 0 if already_exited else None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self._clock.now += timeout
        if self._left is not None:
            self._left -= 1
            if self._left <= 0:
                self.returncode = 0
                return 0
        raise subprocess.TimeoutExpired("x", timeout)


class _FlipToken:
    def __init__(self, after):
        self._after = after
        self.calls = 0

    def is_cancelled(self):
        self.calls += 1
        return self.calls > self._after


def test_wait_is_sliced_and_times_out_at_the_deadline():
    """M5: never a single blocking wait for the whole budget."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock)
    assert wait_cancellable(proc, timeout=1.0, token=NEVER_CANCELLED, monotonic=clock) is WaitResult.TIMED_OUT
    assert proc.waits and max(proc.waits) <= 0.1 + 1e-9
    assert sum(proc.waits) == pytest.approx(1.0)


def test_wait_observes_the_token_between_slices():
    clock = _FakeClock()
    proc = _ScriptedProc(clock)
    result = wait_cancellable(proc, timeout=60.0, token=_FlipToken(after=3), monotonic=clock)
    assert result is WaitResult.CANCELLED
    assert len(proc.waits) == 3 and proc.returncode is None


def test_wait_exits_normally():
    clock = _FakeClock()
    proc = _ScriptedProc(clock, exits_after=2)
    assert wait_cancellable(proc, timeout=60.0, token=NEVER_CANCELLED, monotonic=clock) is WaitResult.EXITED


def test_an_already_exited_process_is_never_reported_cancelled():
    """M30 (observed-before-exit): the genuine outcome stands."""
    clock = _FakeClock()
    proc = _ScriptedProc(clock, already_exited=True)
    assert wait_cancellable(proc, timeout=60.0, token=_FlipToken(after=0), monotonic=clock) is WaitResult.EXITED
    assert proc.waits == []


# --- D3 driver boundary pins (plan section 10.4) ---------------------------


def test_driver_exists_and_no_production_module_imports_tests_or_the_driver():
    """M28: the driver lives under tests/ only; production never imports
    `tests`, the driver, or `_lifecycle_run` (T41 pins the latter too)."""
    src = Path(inspect.getfile(cn)).parent
    root = src.parent.parent
    assert (root / "tests" / "support" / "cancellation_driver.py").is_file()
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            for name in names:
                assert not name.startswith("tests"), path.name
                assert "cancellation_driver" not in name, path.name
                if path.name != "_lifecycle_run.py":
                    assert name not in ("_lifecycle_run", "codeagent._lifecycle_run"), path.name


# --- best-effort, never-raising evidence attachment (sixth correction) -----

from codeagent.cancellation import attach_cleanup_failures, set_evidence_attribute  # noqa: E402
from tests.support.refusing import REFUSALS, refusing  # noqa: E402


@pytest.mark.parametrize("make_refusal", REFUSALS)
@pytest.mark.parametrize("on_read, on_write", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("base", [RuntimeError, KeyboardInterrupt])
def test_attachment_never_raises_and_never_touches_the_chain(make_refusal, on_read, on_write, base):
    exc = refusing(base, make_refusal(), on_read=on_read, on_write=on_write)
    cause, context = RuntimeError("c"), RuntimeError("x")
    exc.__cause__, exc.__context__ = cause, context
    try:
        attach_cleanup_failures(exc, (RuntimeError("later"),))
        set_evidence_attribute(exc, "codeagent_deferred_interrupt", KeyboardInterrupt())
    except BaseException as escaped:  # noqa: BLE001 - would otherwise abort the session
        pytest.fail(f"{type(escaped).__name__} escaped the attachment helper")
    assert exc.__cause__ is cause and exc.__context__ is context


def test_attachment_appends_in_order_by_identity():
    exc, a, b = KeyboardInterrupt(), RuntimeError("a"), KeyboardInterrupt("b")
    attach_cleanup_failures(exc, (a,))
    attach_cleanup_failures(exc, (b, a))
    assert exc.codeagent_cleanup_failures == (a, b, a)
