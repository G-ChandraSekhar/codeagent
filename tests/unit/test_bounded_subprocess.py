"""Tests for `codeagent._bounded_subprocess` (Milestone 3 Slice 3B-4).

Real subprocesses (never a real `docker` daemon) are used for every
scenario that depends on genuine process timing (clean exit, overflow,
timeout, kill/reap confirmation) — matching this project's established
pattern for testing bounded-subprocess primitives (see the equivalent
tests this module's own logic was extracted from in
`tests/unit/test_reconciliation.py`'s prior history, and
`codeagent._git_safety`'s own bounded-subprocess tests). Launch and
monitoring-setup failures are exercised via monkeypatching, since those
don't need a real process at all.
"""

from __future__ import annotations

import subprocess

import pytest

from codeagent import _bounded_subprocess as bs


def test_clean_zero_exit() -> None:
    result = bs.run_bounded_stdout(
        ["python3", "-c", "print('hello')"], timeout_seconds=5.0, stdout_limit=1024
    )
    assert result.returncode == 0
    assert result.stdout == b"hello\n"


def test_clean_nonzero_exit_is_not_raised() -> None:
    result = bs.run_bounded_stdout(
        ["python3", "-c", "import sys; sys.exit(3)"], timeout_seconds=5.0, stdout_limit=1024
    )
    assert result.returncode == 3


def test_exact_output_boundary_succeeds() -> None:
    """Exactly `stdout_limit` bytes (no overflow) must succeed."""
    result = bs.run_bounded_stdout(
        ["python3", "-c", "import sys; sys.stdout.write('x' * 10)"],
        timeout_seconds=5.0,
        stdout_limit=10,
    )
    assert result.returncode == 0
    assert result.stdout == b"x" * 10


def test_one_byte_overflow_is_output_limit_exceeded() -> None:
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(
            ["python3", "-c", "import sys; sys.stdout.write('x' * 11)"],
            timeout_seconds=5.0,
            stdout_limit=10,
        )
    assert excinfo.value.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED


def test_overflow_confirms_child_terminated(monkeypatch) -> None:
    process_holder: dict[str, subprocess.Popen] = {}
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(*a, **k)
        process_holder["proc"] = proc
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError):
        bs.run_bounded_stdout(
            [
                "python3",
                "-c",
                "import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(5)",
            ],
            timeout_seconds=5.0,
            stdout_limit=10,
        )

    assert process_holder["proc"].poll() is not None


def test_timeout_is_raised_and_confirms_termination(monkeypatch) -> None:
    process_holder: dict[str, subprocess.Popen] = {}
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(*a, **k)
        process_holder["proc"] = proc
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(
            ["python3", "-c", "import time; time.sleep(5)"],
            timeout_seconds=0.2,
            stdout_limit=1_000_000,
        )

    assert excinfo.value.reason is bs.BoundedProcessFailure.TIMED_OUT
    assert process_holder["proc"].poll() is not None


def test_launch_failure_is_categorically_distinct(monkeypatch) -> None:
    def raise_oserror(*a, **k):
        raise OSError("no such executable")

    monkeypatch.setattr(bs.subprocess, "Popen", raise_oserror)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["does-not-exist"], timeout_seconds=1.0, stdout_limit=1024)
    assert excinfo.value.reason is bs.BoundedProcessFailure.LAUNCH_FAILED


def test_monitoring_setup_failure(monkeypatch) -> None:
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: (_ for _ in ()).throw(OSError("no selector")))
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=5.0, stdout_limit=1024)
    assert excinfo.value.reason is bs.BoundedProcessFailure.MONITORING_FAILED


def test_kill_failure_still_confirms_reap_via_natural_exit(monkeypatch) -> None:
    """A `process.kill()` failure must not prevent confirming
    termination if the child exits on its own within the grace
    window."""
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import time; time.sleep(0.05)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        proc.kill = lambda: (_ for _ in ()).throw(OSError("kill failed"))
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=0.01, stdout_limit=10)
    # The original timeout, not TERMINATION_UNCONFIRMED: cleanup itself
    # succeeded (the child exited on its own within the grace window)
    # despite kill() raising.
    assert excinfo.value.reason is bs.BoundedProcessFailure.TIMED_OUT


def test_wait_failure_after_eof_is_timed_out(monkeypatch) -> None:
    """A child that closes stdout (EOF) but does not promptly exit is
    classified as a timeout, not silently accepted."""
    real_popen = subprocess.Popen

    def factory(*a, **k):
        # A child whose stdout closes almost immediately (small write,
        # then it keeps running past the deadline before actually
        # exiting) — EOF is observed, but the subsequent wait() must
        # still confirm exit within the same deadline and fail to.
        return real_popen(
            [
                "python3",
                "-c",
                "import sys,time; sys.stdout.write('x'); sys.stdout.close(); time.sleep(2)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=0.2, stdout_limit=1024)
    assert excinfo.value.reason is bs.BoundedProcessFailure.TIMED_OUT


def test_termination_unconfirmed_dominates_with_explicit_cause(monkeypatch) -> None:
    """If termination cannot be confirmed after a timeout, that failure
    dominates and is raised explicitly `from` a fully-formed
    `BoundedProcessError` representing the original failure — never
    left to incidental `__context__`, and never the internal-only
    `_BoundedFailure` signal type."""
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import time; time.sleep(0.05)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def always_timeout(timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        proc.wait = always_timeout
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=0.01, stdout_limit=10)

    assert excinfo.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, bs.BoundedProcessError)
    assert excinfo.value.__cause__.reason is bs.BoundedProcessFailure.TIMED_OUT


def test_descriptor_and_stream_cleanup_on_success(monkeypatch) -> None:
    """stdout is closed by the time this returns -- reading from it
    again must fail, proving the descriptor was not leaked open."""
    real_popen = subprocess.Popen
    holder: dict[str, subprocess.Popen] = {}

    def factory(*a, **k):
        proc = real_popen(*a, **k)
        holder["proc"] = proc
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    bs.run_bounded_stdout(["python3", "-c", "print('x')"], timeout_seconds=5.0, stdout_limit=1024)

    assert holder["proc"].stdout.closed


def test_no_shell_structured_argv(monkeypatch) -> None:
    captured: list = []
    real_popen = subprocess.Popen

    def factory(*a, **k):
        captured.append((a, k))
        return real_popen(*a, **k)

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=5.0, stdout_limit=1024)

    args, kwargs = captured[0]
    assert args[0] == ["python3", "-c", "print(1)"]
    assert kwargs.get("shell", False) is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_rejects_empty_argv() -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout([], timeout_seconds=1.0, stdout_limit=1024)


def test_rejects_argv_with_empty_element() -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", ""], timeout_seconds=1.0, stdout_limit=1024)


def test_rejects_nonpositive_stdout_limit() -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=1.0, stdout_limit=0)


# --------------------------------------------------------------------
# Correction pass, finding 1: each os.read() request is bounded to the
# caller's remaining allowance, never a flat chunk size
# --------------------------------------------------------------------


def test_drain_never_requests_more_than_remaining_allowance(monkeypatch) -> None:
    """A small `stdout_limit` must genuinely bound how much a single
    `os.read()` call can request -- not merely how much is retained
    afterward. Instruments the real requested sizes for the *target
    subprocess's own fd only* (patching `os.read` process-wide also
    intercepts unrelated reads, e.g. pytest's own output-capture
    machinery, which must not be mistaken for this module's own
    behavior); the flat `_BOUNDED_READ_CHUNK` (64 KiB) must never
    appear for that fd when the limit is far smaller than that."""
    requested_sizes: list[int] = []
    real_read = bs.os.read
    real_popen = subprocess.Popen
    target_fd_holder: dict[str, int] = {}

    def factory(*a, **k):
        proc = real_popen(*a, **k)
        target_fd_holder["fd"] = proc.stdout.fileno()
        return proc

    def spy_read(fd, n):
        if fd == target_fd_holder.get("fd"):
            requested_sizes.append(n)
        return real_read(fd, n)

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.os, "read", spy_read)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(
            ["python3", "-c", "import sys; sys.stdout.write('x' * 100)"],
            timeout_seconds=5.0,
            stdout_limit=10,
        )

    assert excinfo.value.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED
    assert requested_sizes  # at least one read was attempted on the target fd
    assert all(n <= 11 for n in requested_sizes)  # limit + 1, never the flat 64 KiB chunk
    assert bs._BOUNDED_READ_CHUNK not in requested_sizes


# --------------------------------------------------------------------
# Correction pass, finding 2: kill-confirmation uses a fresh grace
# deadline, never derived from the original command deadline
# --------------------------------------------------------------------


def test_kill_confirmation_uses_fresh_grace_deadline(monkeypatch) -> None:
    """An early overflow on a long-timeout (30s) command must not grant
    the kill-confirmation wait() anywhere near that remaining budget --
    only the fixed, fresh grace period."""
    wait_timeouts: list[float] = []
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            [
                "python3",
                "-c",
                "import sys,time; sys.stdout.write('x'*100); sys.stdout.flush(); time.sleep(5)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        real_wait = proc.wait

        def spy_wait(timeout=None):
            wait_timeouts.append(timeout)
            return real_wait(timeout=timeout)

        proc.wait = spy_wait
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=30.0, stdout_limit=10)

    assert excinfo.value.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED
    assert wait_timeouts  # the kill-confirmation wait() was reached
    # Bounded by the fixed grace period plus a small scheduling
    # tolerance -- nowhere near the ~29 remaining seconds of the
    # original 30-second command deadline.
    assert wait_timeouts[-1] <= bs._KILL_CONFIRM_GRACE_SECONDS + 1.0


# --------------------------------------------------------------------
# Correction pass, finding 3: cancellation-style BaseException is never
# translated into a categorical BoundedProcessError
# --------------------------------------------------------------------


class _AlwaysReadySelector:
    """A selector stand-in that always reports readiness immediately --
    lets a test reach `os.read()` deterministically without waiting on
    real pipe timing."""

    def register(self, *a, **k) -> None:
        pass

    def select(self, timeout=None):
        return [1]

    def close(self) -> None:
        pass


def test_keyboard_interrupt_propagates_unchanged_after_successful_cleanup(monkeypatch) -> None:
    """A cancellation-style BaseException injected at the internal read
    boundary (never a real OS signal) must propagate as the exact same
    instance once cleanup (kill + confirmed reap) succeeds -- never
    wrapped into a categorical BoundedProcessError. `os.read` is only
    made to raise for the target subprocess's own stdout fd: patching
    it unconditionally also intercepts `subprocess.Popen`'s own
    internal errpipe read during process launch on some code paths,
    which is not what this test means to exercise."""
    injected = KeyboardInterrupt("simulated interrupt")
    real_popen = subprocess.Popen
    real_read = bs.os.read
    target_fd_holder: dict[str, int] = {}

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        target_fd_holder["fd"] = proc.stdout.fileno()
        return proc

    def raising_read(fd, n):
        if fd == target_fd_holder.get("fd"):
            raise injected
        return real_read(fd, n)

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _AlwaysReadySelector())
    monkeypatch.setattr(bs.os, "read", raising_read)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value is injected


def test_keyboard_interrupt_with_failed_cleanup_chains_termination_unconfirmed(monkeypatch) -> None:
    """If termination cannot be confirmed while handling a cancellation-
    style BaseException, the result must be `BoundedProcessError(
    TERMINATION_UNCONFIRMED)` explicitly chained `from` the exact
    original exception instance -- never left to incidental
    `__context__`, and never silently discarding the interrupt."""
    injected = KeyboardInterrupt("simulated interrupt")
    real_popen = subprocess.Popen
    real_read = bs.os.read
    target_fd_holder: dict[str, int] = {}

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import time; time.sleep(0.05)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        target_fd_holder["fd"] = proc.stdout.fileno()

        def always_timeout(timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        proc.wait = always_timeout
        return proc

    def raising_read(fd, n):
        if fd == target_fd_holder.get("fd"):
            raise injected
        return real_read(fd, n)

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _AlwaysReadySelector())
    monkeypatch.setattr(bs.os, "read", raising_read)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert excinfo.value.__cause__ is injected


# --------------------------------------------------------------------
# Correction pass, finding 4: complete, sanitized argument validation
# --------------------------------------------------------------------


@pytest.mark.parametrize("argv", ["python3 -c print(1)", b"python3"])
def test_rejects_bare_string_or_bytes_argv(argv) -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(argv, timeout_seconds=1.0, stdout_limit=1024)


def test_rejects_non_string_argv_element() -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", 3], timeout_seconds=1.0, stdout_limit=1024)


def test_rejects_nul_containing_argv_element() -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "a\x00b"], timeout_seconds=1.0, stdout_limit=1024)


@pytest.mark.parametrize("limit", ["10", 1.5, True, False, 0, -1])
def test_rejects_malformed_stdout_limit(limit) -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=1.0, stdout_limit=limit)


@pytest.mark.parametrize(
    "timeout", ["10", True, False, 0, -1, float("nan"), float("inf"), float("-inf")]
)
def test_rejects_malformed_timeout(timeout) -> None:
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=timeout, stdout_limit=1024)


def test_validation_error_never_includes_caller_argv_or_values() -> None:
    """Rejection messages must be fixed, sanitized categorical text --
    never the caller's actual argv or value."""
    secret_marker = "super-secret-argv-value-xyz"
    with pytest.raises(ValueError) as excinfo:
        bs.run_bounded_stdout(["python3", secret_marker, ""], timeout_seconds=1.0, stdout_limit=1024)
    assert secret_marker not in str(excinfo.value)


def test_validation_never_leaks_incidental_type_error() -> None:
    """A wrong-typed `stdout_limit`/`timeout_seconds` must raise a clean
    `ValueError`, never an incidental `TypeError` from an unguarded
    comparison against the wrong type."""
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds=1.0, stdout_limit="not-a-number")
    with pytest.raises(ValueError):
        bs.run_bounded_stdout(["python3", "-c", "print(1)"], timeout_seconds="not-a-number", stdout_limit=1024)


# --------------------------------------------------------------------
# Correction pass, finding 5: os.read() I/O error is MONITORING_FAILED,
# not TIMED_OUT; any non-timeout wait() failure is still categorical
# --------------------------------------------------------------------


def test_read_oserror_is_monitoring_failed_not_timed_out(monkeypatch) -> None:
    """`os.read` is only made to raise for the target subprocess's own
    stdout fd -- patching it unconditionally also intercepts
    `subprocess.Popen`'s own internal errpipe read during launch on
    some code paths, which is not what this test means to exercise."""
    real_popen = subprocess.Popen
    real_read = bs.os.read
    target_fd_holder: dict[str, int] = {}

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        target_fd_holder["fd"] = proc.stdout.fileno()
        return proc

    def raising_read(fd, n):
        if fd == target_fd_holder.get("fd"):
            raise OSError("simulated pipe I/O error")
        return real_read(fd, n)

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _AlwaysReadySelector())
    monkeypatch.setattr(bs.os, "read", raising_read)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.MONITORING_FAILED


def test_non_timeout_wait_error_during_normal_completion_is_categorical(monkeypatch) -> None:
    """A `wait()` failure other than `TimeoutExpired`, reached during
    ordinary completion confirmation (`_confirm_exit`, not termination
    cleanup), must still become a categorical `BoundedProcessError`
    (`MONITORING_FAILED`) and still trigger termination cleanup --
    never a raw exception escaping. `wait()` fails only on its first
    call (the `_confirm_exit` one); the subsequent kill-confirmation
    `wait()` inside `_terminate_and_confirm` succeeds for real, so the
    final result is exactly `MONITORING_FAILED`, not dominated by a
    (here, nonexistent) termination failure."""
    real_popen = subprocess.Popen
    call_count = {"n": 0}

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "print('x')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        real_wait = proc.wait

        def flaky_once_wait(timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated wait() failure")
            return real_wait(timeout=timeout)

        proc.wait = flaky_once_wait
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.MONITORING_FAILED
    assert call_count["n"] >= 2  # both _confirm_exit's and the termination-confirming wait() ran


def test_non_timeout_wait_error_during_termination_is_unconfirmed(monkeypatch) -> None:
    """A `wait()` failure other than `TimeoutExpired`, reached during
    kill-confirmation itself, must become `TERMINATION_UNCONFIRMED` --
    not every inability to confirm reap is a `TimeoutExpired`. Uses a
    real, fast-completing process and a real read loop (no `os.read`
    interception needed): only `process.wait()` is faked, so `_drain`
    succeeds normally, `_confirm_exit`'s own `wait()` call fails first
    (-> MONITORING_FAILED), and `_terminate_and_confirm`'s `wait()`
    call also fails (-> TERMINATION_UNCONFIRMED)."""
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "print('x')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def flaky_wait(timeout=None):
            raise OSError("simulated wait() failure")

        proc.wait = flaky_wait
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED


# --------------------------------------------------------------------
# Correction pass, finding 6: descriptor-close failures are never
# silently swallowed
# --------------------------------------------------------------------


def test_selector_close_failure_is_cleanup_unconfirmed_on_success_path(monkeypatch) -> None:
    """Even an otherwise fully successful drain must not silently
    discard a selector-close failure."""

    class _FailingCloseSelector(_AlwaysReadySelector):
        def close(self) -> None:
            raise OSError("simulated selector close failure")

    real_popen = subprocess.Popen

    def factory(*a, **k):
        return real_popen(
            ["python3", "-c", "print('x')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _FailingCloseSelector())

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED


def test_stdout_close_failure_during_drain_is_cleanup_unconfirmed(monkeypatch) -> None:
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "print('x')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        real_close = proc.stdout.close

        def failing_close():
            raise OSError("simulated stdout close failure")

        proc.stdout.close = failing_close
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED


def test_both_selector_and_stdout_close_fail_is_still_categorical(monkeypatch) -> None:
    """Both close failures happening together must still be attempted
    and result in one clean categorical outcome, not a raw exception."""

    class _FailingCloseSelector(_AlwaysReadySelector):
        def close(self) -> None:
            raise OSError("simulated selector close failure")

    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "print('x')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def failing_close():
            raise OSError("simulated stdout close failure")

        proc.stdout.close = failing_close
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _FailingCloseSelector())

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=1024)

    assert excinfo.value.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED


def test_cleanup_unconfirmed_chains_from_earlier_read_failure(monkeypatch) -> None:
    """When a read failure (here, OUTPUT_LIMIT_EXCEEDED) and a
    descriptor-close failure both occur, the raised
    `CLEANUP_UNCONFIRMED` must dominate but its `__cause__` must be the
    earlier read failure's own `BoundedProcessError`, not the raw close
    exception and not discarded."""

    class _FailingCloseSelector(_AlwaysReadySelector):
        def close(self) -> None:
            raise OSError("simulated selector close failure")

    real_popen = subprocess.Popen

    def factory(*a, **k):
        return real_popen(
            ["python3", "-c", "import sys; sys.stdout.write('x' * 100)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _FailingCloseSelector())

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=10)

    assert excinfo.value.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__cause__, bs.BoundedProcessError)
    assert excinfo.value.__cause__.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED


# --------------------------------------------------------------------
# Second correction pass, finding 1: the complete public failure chain
# must survive a repeated cleanup failure, three levels deep
# --------------------------------------------------------------------


def test_repeated_cleanup_failure_preserves_complete_three_level_chain(monkeypatch) -> None:
    """Forces all three levels: a real OUTPUT_LIMIT_EXCEEDED read
    failure, a descriptor-close failure inside `_drain()` (producing
    the first CLEANUP_UNCONFIRMED, chained from the read failure), and
    another close failure during `_terminate_and_confirm()` (producing
    the final dominant CLEANUP_UNCONFIRMED). Every node in the raised
    chain must be a public `BoundedProcessError` -- the internal
    `_BoundedFailure` signal type must never appear anywhere in it."""
    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import sys; sys.stdout.write('x' * 100)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        # Fails every close attempt on this process's stdout, whether
        # made by _drain() (first) or _terminate_and_confirm() (second).
        def failing_close():
            raise OSError("simulated stdout close failure")

        proc.stdout.close = failing_close
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=10)

    final = excinfo.value
    middle = final.__cause__
    deepest = middle.__cause__ if middle is not None else None

    assert final.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED
    assert isinstance(middle, bs.BoundedProcessError)
    assert middle.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED
    assert isinstance(deepest, bs.BoundedProcessError)
    assert deepest.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED
    assert deepest.__cause__ is None  # the chain terminates here, nothing deeper

    for node in (final, middle, deepest):
        assert isinstance(node, bs.BoundedProcessError)
        assert not isinstance(node, bs._BoundedFailure)


def test_repeated_termination_unconfirmed_also_preserves_complete_chain(monkeypatch) -> None:
    """The same three-level property, but with the second (dominant)
    cleanup failure being an unconfirmed *termination* (a `wait()`
    failure) rather than a second descriptor-close failure -- proving
    the chain-preservation fix is general, not specific to
    `CLEANUP_UNCONFIRMED` recurring twice."""

    class _FailingCloseSelector(_AlwaysReadySelector):
        def close(self) -> None:
            raise OSError("simulated selector close failure")

    real_popen = subprocess.Popen

    def factory(*a, **k):
        proc = real_popen(
            ["python3", "-c", "import sys,time; sys.stdout.write('x'*100); sys.stdout.flush(); time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def always_timeout(timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        proc.wait = always_timeout
        return proc

    monkeypatch.setattr("codeagent._bounded_subprocess.subprocess.Popen", factory)
    monkeypatch.setattr(bs.selectors, "DefaultSelector", lambda: _FailingCloseSelector())

    with pytest.raises(bs.BoundedProcessError) as excinfo:
        bs.run_bounded_stdout(["ignored"], timeout_seconds=5.0, stdout_limit=10)

    final = excinfo.value
    middle = final.__cause__
    deepest = middle.__cause__ if middle is not None else None

    assert final.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert isinstance(middle, bs.BoundedProcessError)
    assert middle.reason is bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED
    assert isinstance(deepest, bs.BoundedProcessError)
    assert deepest.reason is bs.BoundedProcessFailure.OUTPUT_LIMIT_EXCEEDED

    for node in (final, middle, deepest):
        assert isinstance(node, bs.BoundedProcessError)
        assert not isinstance(node, bs._BoundedFailure)


# ADR 0005 Amendment 1 (ledger D3): L1 real-process session tests.
from tests.support import process_shims as _shims  # noqa: E402


def test_l1_launch_leads_its_own_session(tmp_path, monkeypatch):
    _shims.check_session("L1", tmp_path, monkeypatch)


def test_l1_group_sigint_never_reaches_the_child(tmp_path):
    _shims.check_group_sigint_spared("L1", tmp_path)


def test_l1_abort_kills_the_whole_process_group(tmp_path, monkeypatch):
    _shims.check_abort_group_kill("L1", tmp_path, monkeypatch)


# ADR 0005 Amendment 1 (D3 joint-review correction): L1's group kill is
# fail-closed. ProcessLookupError means the group is confirmed absent; any
# other group-kill failure is retained, every later cleanup step still runs
# exactly once, and the result is TERMINATION_UNCONFIRMED.

import sys  # noqa: E402

_GROUP_SENTINEL_TEXT = "SENTINEL-group-kill-/secret/path"


class _AbortProc:
    """A fake child for the abort path. Never a real pid: every test using
    it patches `os.killpg`."""

    def __init__(self, *, wait_error=None, close_error=None):
        self.pid = 4_000_321
        self.kills = 0
        self.waits = 0
        self.closes = 0
        self._wait_error = wait_error
        self._close_error = close_error
        outer = self

        class _Stdout:
            def close(self):
                outer.closes += 1
                if outer._close_error is not None:
                    raise outer._close_error

        self.stdout = _Stdout()

    def kill(self):
        self.kills += 1

    def wait(self, timeout=None):
        self.waits += 1
        if self._wait_error is not None:
            raise self._wait_error
        return -9


def _killpg_raising(monkeypatch, exc, probe_exc=None):
    """`exc` for the SIGKILL group kill; `probe_exc` (default: `exc`) for
    the post-reap signal-0 probe."""
    calls = []

    def killpg(pid, sig):
        calls.append((pid, sig))
        raise exc if sig == bs.signal.SIGKILL else (probe_exc if probe_exc is not None else exc)

    monkeypatch.setattr(bs.os, "killpg", killpg)
    return calls


def test_l1_group_absent_is_not_uncertain(monkeypatch):
    calls = _killpg_raising(monkeypatch, ProcessLookupError())
    proc = _AbortProc()
    bs._terminate_and_confirm(proc)  # confirmed: no raise
    assert calls == [(proc.pid, bs.signal.SIGKILL)]
    assert (proc.kills, proc.closes, proc.waits) == (1, 1, 1)


@pytest.mark.parametrize("group_exc", [PermissionError(_GROUP_SENTINEL_TEXT), RuntimeError(_GROUP_SENTINEL_TEXT)])
def test_l1_group_kill_failure_dominates_a_confirmed_reap(monkeypatch, group_exc):
    calls = _killpg_raising(monkeypatch, group_exc)
    proc = _AbortProc()
    with pytest.raises(bs._BoundedFailure) as info:
        bs._terminate_and_confirm(proc)
    assert info.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert info.value.__cause__ is group_exc
    # Every later step still ran, exactly once; no second group/direct kill
    # (the only further group call is the signal-0 probe after the reap).
    assert [sig for _, sig in calls] == [bs.signal.SIGKILL, 0]
    assert (proc.kills, proc.closes, proc.waits) == (1, 1, 1)


def test_l1_post_reap_probe_confirms_an_absent_group(monkeypatch):
    """macOS reports PermissionError for a group holding only its exited
    leader; a ProcessLookupError from the post-reap signal-0 probe confirms
    no member remains."""
    calls = _killpg_raising(monkeypatch, PermissionError(_GROUP_SENTINEL_TEXT), probe_exc=ProcessLookupError())
    proc = _AbortProc()
    bs._terminate_and_confirm(proc)  # confirmed: no raise
    assert [sig for _, sig in calls] == [bs.signal.SIGKILL, 0]
    assert (proc.kills, proc.closes, proc.waits) == (1, 1, 1)


def test_l1_group_kill_failure_dominates_a_close_failure(monkeypatch):
    group_exc = PermissionError(_GROUP_SENTINEL_TEXT)
    _killpg_raising(monkeypatch, group_exc)
    proc = _AbortProc(close_error=OSError("close"))
    with pytest.raises(bs._BoundedFailure) as info:
        bs._terminate_and_confirm(proc)
    assert info.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert info.value.__cause__ is group_exc


def test_l1_unconfirmed_reap_dominates_a_group_kill_failure(monkeypatch):
    _killpg_raising(monkeypatch, PermissionError(_GROUP_SENTINEL_TEXT))
    reap_exc = subprocess.TimeoutExpired("x", 1)
    proc = _AbortProc(wait_error=reap_exc)
    with pytest.raises(bs._BoundedFailure) as info:
        bs._terminate_and_confirm(proc)
    assert info.value.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert info.value.__cause__ is reap_exc
    assert (proc.kills, proc.waits) == (1, 1)


def test_l1_end_to_end_group_kill_failure_is_sanitized_and_kills_once(monkeypatch):
    """A real timed-out child: the direct kill and reap still happen exactly
    once, but the public result is TERMINATION_UNCONFIRMED with fixed text."""
    group_exc = PermissionError(_GROUP_SENTINEL_TEXT)
    calls = _killpg_raising(monkeypatch, group_exc)
    kills = []
    real_kill = subprocess.Popen.kill
    monkeypatch.setattr(subprocess.Popen, "kill", lambda self: (kills.append(self.pid), real_kill(self))[1])
    with pytest.raises(bs.BoundedProcessError) as info:
        bs.run_bounded_stdout(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.3, stdout_limit=64
        )
    err = info.value
    assert err.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert isinstance(err.__cause__, bs.BoundedProcessError)
    assert err.__cause__.reason is bs.BoundedProcessFailure.TIMED_OUT
    assert [sig for _, sig in calls] == [bs.signal.SIGKILL, 0] and len(kills) == 1
    chain, node = [], err
    while node is not None:
        chain.append(node)
        node = node.__cause__
    assert all(isinstance(n, bs.BoundedProcessError) for n in chain)
    assert all(_GROUP_SENTINEL_TEXT not in str(n) for n in chain)


# ADR 0005 Amendment 1 §12 (fourth correction): L1 BaseException model, and
# a pin of the outer body-interrupt path (cleanup completes, then the exact
# body interrupt is re-raised).


class _L1Base(BaseException):
    pass


def _without_escape(fn):
    """Call `fn`; any escaping `BaseException` (including KeyboardInterrupt,
    which would otherwise abort the pytest session) becomes a test failure."""
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"{type(exc).__name__} escaped instead of being retained")


def _l1_steps(monkeypatch, *, group=None, probe=ProcessLookupError()):
    calls = []

    def killpg(pid, sig):
        calls.append(sig)
        action = group if sig == bs.signal.SIGKILL else probe
        if action is not None:
            raise action

    monkeypatch.setattr(bs.os, "killpg", killpg)
    return calls


@pytest.mark.parametrize("site", ["group_kill", "direct_kill"])
def test_l1_confirmed_cleanup_returns_the_interrupt(monkeypatch, site):
    inst = KeyboardInterrupt(f"SENTINEL-{site}")
    calls = _l1_steps(monkeypatch, group=inst if site == "group_kill" else None)
    proc = _AbortProc()
    if site == "direct_kill":
        proc.kill = lambda: (_ for _ in ()).throw(inst)
    # Fifth correction: a confirmed cleanup returns its whole chronological
    # record (the caller's body failure stays primary).
    assert _without_escape(lambda: bs._terminate_and_confirm(proc)) == (inst,)
    assert (proc.closes, proc.waits) == (1, 1)
    assert calls == ([bs.signal.SIGKILL, 0] if site == "group_kill" else [bs.signal.SIGKILL])


@pytest.mark.parametrize("site", ["reap", "probe", "close"])
def test_l1_unconfirmed_cleanup_dominates_an_interrupt(monkeypatch, site):
    inst = _L1Base(f"SENTINEL-{site}")
    if site == "reap":
        _l1_steps(monkeypatch)
        proc, reason = _AbortProc(wait_error=inst), bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    elif site == "probe":
        _l1_steps(monkeypatch, group=PermissionError("SENTINEL-g"), probe=inst)
        proc, reason = _AbortProc(), bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    else:
        _l1_steps(monkeypatch)
        proc, reason = _AbortProc(close_error=inst), bs.BoundedProcessFailure.CLEANUP_UNCONFIRMED
    with pytest.raises(bs._BoundedFailure) as info:
        bs._terminate_and_confirm(proc)
    assert info.value.reason is reason
    assert info.value.deferred_interrupt is inst and inst in info.value.failures_in_order
    assert (proc.kills, proc.closes, proc.waits) == (1, 1, 1)


def test_l1_path_level_confirmed_cleanup_reraises_and_dominance_is_public(monkeypatch):
    inst = KeyboardInterrupt("SENTINEL-ki")
    _l1_steps(monkeypatch, group=inst)  # probe confirms absence
    kills = []
    real_kill = subprocess.Popen.kill
    monkeypatch.setattr(subprocess.Popen, "kill", lambda self: (kills.append(self.pid), real_kill(self))[1])
    # Fifth correction: the body failure (the timeout) stays primary; the
    # cleanup interrupt is attached, never raised in its place.
    err = _without_escape(lambda: _expect_bounded_error(
        lambda: bs.run_bounded_stdout([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.3, stdout_limit=64)
    ))
    assert err.reason is bs.BoundedProcessFailure.TIMED_OUT
    assert err.codeagent_cleanup_failures == (inst,) and len(kills) == 1

    inst2 = _L1Base("SENTINEL-base")
    _l1_steps(monkeypatch, group=inst2, probe=PermissionError("SENTINEL-p"))
    with pytest.raises(bs.BoundedProcessError) as info2:
        bs.run_bounded_stdout([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.3, stdout_limit=64)
    err = info2.value
    assert err.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert err.deferred_interrupt is inst2 and "SENTINEL" not in str(err)


def test_l1_body_interrupt_completes_cleanup_before_reraise(monkeypatch):
    """Pins the outer path: a body interrupt (in the drain) still runs the
    complete abort (group kill, kill, close, reap) and then re-raises the
    exact instance."""
    inst = KeyboardInterrupt("SENTINEL-body")
    calls = _l1_steps(monkeypatch, group=None)
    waits = []
    real_wait = subprocess.Popen.wait
    monkeypatch.setattr(subprocess.Popen, "wait", lambda self, timeout=None: (waits.append(timeout), real_wait(self, timeout))[1])

    def drain(process, *, deadline, limit):
        raise inst

    monkeypatch.setattr(bs, "_drain", drain)
    with pytest.raises(KeyboardInterrupt) as info:
        bs.run_bounded_stdout([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=30, stdout_limit=64)
    assert info.value is inst
    assert calls == [bs.signal.SIGKILL] and len(waits) == 1



def _expect_bounded_error(fn):
    try:
        fn()
    except bs.BoundedProcessError as err:
        return err
    raise AssertionError("expected a BoundedProcessError")


# ADR 0005 Amendment 1 §12 (fifth correction): one precedence model. The body
# failure stays primary after a confirmed cleanup; later cleanup failures are
# attached in order by identity; unconfirmed cleanup dominates.


def _l1_body(monkeypatch, body):
    def drain(process, *, deadline, limit):
        raise body

    monkeypatch.setattr(bs, "_drain", drain)


def _sleeper():
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def _count_kills(monkeypatch):
    kills = []
    real_kill = subprocess.Popen.kill
    monkeypatch.setattr(subprocess.Popen, "kill", lambda self: (kills.append(self.pid), real_kill(self))[1])
    return kills


def _with_preset_chain(exc):
    cause, context = RuntimeError("preset-cause"), RuntimeError("preset-context")
    exc.__cause__, exc.__context__ = cause, context
    return exc, cause, context


def test_l1_ordinary_body_failure_then_cleanup_interrupt(monkeypatch):
    kills = _count_kills(monkeypatch)
    body = RuntimeError("SENTINEL-body")
    _l1_body(monkeypatch, body)
    ki = KeyboardInterrupt("SENTINEL-ki")
    _l1_steps(monkeypatch, group=ki)  # probe confirms absence
    err = _without_escape(lambda: _expect_bounded_error(lambda: bs.run_bounded_stdout(_sleeper(), timeout_seconds=30, stdout_limit=64)))
    assert err.reason is bs.BoundedProcessFailure.MONITORING_FAILED and err.__cause__ is body
    assert err.codeagent_cleanup_failures == (ki,)
    assert "SENTINEL" not in str(err) and len(kills) == 1


def test_l1_body_interrupt_then_second_cleanup_interrupt(monkeypatch):
    kills = _count_kills(monkeypatch)
    ki1, cause, context = _with_preset_chain(KeyboardInterrupt("SENTINEL-1"))
    ki2 = _L1Base("SENTINEL-2")
    _l1_body(monkeypatch, ki1)
    _l1_steps(monkeypatch, group=ki2)
    with pytest.raises(KeyboardInterrupt) as info:
        bs.run_bounded_stdout(_sleeper(), timeout_seconds=30, stdout_limit=64)
    assert info.value is ki1
    assert ki1.codeagent_cleanup_failures == (ki2,)
    assert ki1.__cause__ is cause and ki1.__context__ is context
    assert len(kills) == 1  # every later cleanup step still ran


def test_l1_unconfirmed_cleanup_dominates_a_body_interrupt(monkeypatch):
    kills = _count_kills(monkeypatch)
    ki1, cause, context = _with_preset_chain(KeyboardInterrupt("SENTINEL-1"))
    ki2, probe = _L1Base("SENTINEL-2"), PermissionError("SENTINEL-p")
    _l1_body(monkeypatch, ki1)
    _l1_steps(monkeypatch, group=ki2, probe=probe)
    err = _without_escape(lambda: _expect_bounded_error(lambda: bs.run_bounded_stdout(_sleeper(), timeout_seconds=30, stdout_limit=64)))
    assert err.reason is bs.BoundedProcessFailure.TERMINATION_UNCONFIRMED
    assert err.__cause__ is ki1 and err.body_failure is ki1
    assert err.failures_in_order == (ki2, probe) and err.deferred_interrupt is ki2
    assert ki1.__cause__ is cause and ki1.__context__ is context
    assert "SENTINEL" not in str(err) and len(kills) == 1


# Sixth correction: an attribute-refusing body still surfaces, unchanged.
from tests.support.refusing import REFUSALS, refusing  # noqa: E402


@pytest.mark.parametrize("make_refusal", REFUSALS)
def test_l1_refusing_body_still_surfaces_exactly(monkeypatch, make_refusal):
    body = refusing(KeyboardInterrupt, make_refusal())
    _l1_body(monkeypatch, body)
    _l1_steps(monkeypatch, group=_L1Base("SENTINEL-2"))  # a cleanup failure to attach
    kills = _count_kills(monkeypatch)
    with pytest.raises(KeyboardInterrupt) as info:
        bs.run_bounded_stdout(_sleeper(), timeout_seconds=30, stdout_limit=64)
    assert info.value is body and len(kills) == 1
