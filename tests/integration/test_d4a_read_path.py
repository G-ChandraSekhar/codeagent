"""Ledger D4a: the legacy read path over the descriptor-relative toolkit,
and the controller's single terminal route for an unconfirmed
repository-tool descriptor cleanup (D4 plan revision 5 §7.4–§7.5).

R31a (real `RepoTools` with an injected close fault) and R23b (module
boundaries). No Docker.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from codeagent import domain, events, repo_tools
from codeagent._read_adapter import RepoToolsReaderAdapter
from codeagent.cancellation import CLEANUP_FAILURES_ATTRIBUTE, CancellationSource
from codeagent.controller import PlanProposal, RunConfig, RunController
from codeagent.errors import ErrorCode
from codeagent.repo_tools import RepoTools
from codeagent.tool_schema import ToolCleanupUnconfirmedError
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakeModel,
    FakePatchApplier,
    FakeVerifier,
    FakeWorkspace,
    SteppingClock,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "codeagent"
MESSAGE = "repository tool descriptor cleanup unconfirmed"
PLAN = PlanProposal(
    problem_hypothesis="h", proposed_file_paths=("worker.py",), verification_intent="python3 -B -m unittest"
)


def _controller(reader, *, cancellation=None) -> RunController:
    config = RunConfig(
        run_id="r-d4a",
        task_statement="fix",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id="a" * 32,
    )
    kwargs = {"cancellation": cancellation} if cancellation is not None else {}
    return RunController(
        config,
        FakeModel(PLAN),
        FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        FakeVerifier((events.VerificationOutcome.PASSED,), baseline_outcome=events.VerificationOutcome.TEST_FAILURE),
        FakePatchApplier(False),
        reader,
        FakeWorkspace(),
        FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
        **kwargs,
    )


class _CloseFault:
    def __init__(self) -> None:
        self.raised: list[OSError] = []

    def __call__(self, fd: int) -> None:
        import os

        os.close(fd)  # really closed: nothing leaks
        exc = OSError("SENTINEL_CLOSE_TEXT")
        self.raised.append(exc)
        raise exc


@pytest.fixture
def worktree(tmp_path):
    (tmp_path / "worker.py").write_text("def process_job(): ...\n")
    return tmp_path


def _read_completed(controller):
    return [
        e for e in controller.log.events if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.READ_FILE
    ]


def _assert_terminal_route(controller, finished):
    (completed,) = _read_completed(controller)
    assert not completed.success and completed.error.code is ErrorCode.TOOL_EXECUTION_FAILED
    assert completed.error.message == MESSAGE
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error is completed.error  # one occurrence, one error_id
    assert not any(isinstance(e, events.PlanProposed) for e in controller.log.events)
    for event in controller.log.events:
        assert "SENTINEL" not in repr(event)


def test_close_fault_takes_the_single_terminal_route(worktree, monkeypatch):
    fault = _CloseFault()
    monkeypatch.setattr(repo_tools, "_close", fault)
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)))
    calls = []
    real = RunController._terminate

    def spy(self, *a, **k):
        calls.append(sys._getframe(1).f_code.co_name)
        return real(self, *a, **k)

    monkeypatch.setattr(RunController, "_terminate", spy)
    finished = controller.run()
    _assert_terminal_route(controller, finished)
    assert calls == ["_run_after_start"]  # the run loop, never _dispatch_read_file
    (retained,) = controller.tool_cleanup_failures
    assert retained.close_failures == tuple(fault.raised) and retained.body_failure is None
    assert controller.deferred_interrupt is None


def test_body_interrupt_is_deferred_not_raised(worktree, monkeypatch):
    monkeypatch.setattr(repo_tools, "_close", _CloseFault())
    interrupt = KeyboardInterrupt()

    def read(fd, n):
        raise interrupt

    monkeypatch.setattr(repo_tools, "_read", read)
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)))
    try:
        finished = controller.run()
    except BaseException as exc:  # noqa: BLE001 - an escape must fail, not abort, the session
        pytest.fail(f"the deferred interrupt escaped run(): {type(exc).__name__}")
    _assert_terminal_route(controller, finished)
    assert controller.deferred_interrupt is interrupt
    assert controller.tool_cleanup_failures[0].body_failure is interrupt


def test_body_interrupt_with_confirmed_cleanup_propagates_exactly(worktree, monkeypatch):
    interrupt = KeyboardInterrupt()

    def read(fd, n):
        raise interrupt

    monkeypatch.setattr(repo_tools, "_read", read)
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)))
    with pytest.raises(KeyboardInterrupt) as info:
        controller.run()
    assert info.value is interrupt


def test_an_earlier_deferred_interrupt_keeps_priority(worktree, monkeypatch):
    monkeypatch.setattr(repo_tools, "_close", _CloseFault())
    monkeypatch.setattr(repo_tools, "_read", lambda fd, n: (_ for _ in ()).throw(KeyboardInterrupt()))
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)))
    earlier = SystemExit()
    controller._deferred_interrupt = earlier
    try:
        controller.run()
    except BaseException as exc:  # noqa: BLE001
        pytest.fail(f"an interrupt escaped run(): {type(exc).__name__}")
    assert controller.deferred_interrupt is earlier


def test_pending_cancellation_does_not_override_the_cleanup_failure(worktree, monkeypatch):
    source = CancellationSource()
    real_read = repo_tools._read

    def read(fd, n):
        source.request("operator")
        return real_read(fd, n)

    monkeypatch.setattr(repo_tools, "_read", read)
    monkeypatch.setattr(repo_tools, "_close", _CloseFault())
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)), cancellation=source.token)
    finished = controller.run()
    assert source.token.is_cancelled()
    _assert_terminal_route(controller, finished)


def test_duration_of_the_failed_read_is_accounted(worktree, monkeypatch):
    monkeypatch.setattr(repo_tools, "_close", _CloseFault())
    controller = _controller(RepoToolsReaderAdapter(RepoTools(worktree)))
    finished = controller.run()
    (completed,) = _read_completed(controller)
    baseline = next(e for e in controller.log.events if isinstance(e, events.BaselineRecorded))
    assert completed.duration_seconds > 0
    assert finished.total_duration_seconds == pytest.approx(baseline.duration_seconds + completed.duration_seconds)


def test_emission_failure_propagates_with_the_interrupt_attached(monkeypatch):
    controller = _controller(RepoToolsReaderAdapter(RepoTools(pathlib.Path("."))))
    interrupt = KeyboardInterrupt()
    cleanup = ToolCleanupUnconfirmedError((OSError("x"),), interrupt)
    boom = RuntimeError("log broken")

    def emit(event):
        raise boom

    monkeypatch.setattr(controller, "_emit", emit)
    with pytest.raises(RuntimeError) as info:
        controller._record_tool_cleanup(0, domain.ToolName.READ_FILE, "tc", cleanup, 0.0)
    assert info.value is boom
    assert getattr(boom, CLEANUP_FAILURES_ATTRIBUTE) == (interrupt,)
    assert controller.tool_cleanup_failures == (cleanup,)
    assert controller.deferred_interrupt is None


def test_a_raising_fake_reader_takes_the_same_route():
    cleanup = ToolCleanupUnconfirmedError((OSError("SENTINEL_CLOSE_TEXT"),), None)

    class Reader:
        def read_file(self, relative_path):
            raise cleanup

    controller = _controller(Reader())
    finished = controller.run()
    _assert_terminal_route(controller, finished)
    assert controller.tool_cleanup_failures == (cleanup,)


def test_adapter_does_not_catch_the_cleanup_error(worktree, monkeypatch):
    monkeypatch.setattr(repo_tools, "_close", _CloseFault())
    with pytest.raises(ToolCleanupUnconfirmedError):
        RepoToolsReaderAdapter(RepoTools(worktree)).read_file("worker.py")


def test_legacy_path_denies_the_git_administrative_entry(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /SENTINEL/host/path\n")
    result = RepoToolsReaderAdapter(RepoTools(tmp_path)).read_file(".git")
    assert not result.success and result.error.code is ErrorCode.TOOL_INPUT_INVALID
    assert "SENTINEL" not in result.error.message


# --- R23b: module boundaries ---------------------------------------------------


def _imports(name: str) -> set[str]:
    tree = ast.parse((SRC / name).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def test_cleanup_error_is_defined_only_in_the_neutral_module():
    for module in SRC.glob("*.py"):
        tree = ast.parse(module.read_text())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert ("ToolCleanupUnconfirmedError" in defined) == (module.name == "tool_schema.py"), module.name


def test_controller_never_imports_the_toolkit_or_adapter():
    imported = _imports("controller.py")
    assert "codeagent.tool_schema" in imported
    assert not {"codeagent.repo_tools", "codeagent._read_adapter"} & imported


def test_adapter_has_no_except_for_the_cleanup_error():
    tree = ast.parse((SRC / "_read_adapter.py").read_text())
    assert not any(isinstance(n, ast.ExceptHandler) for n in ast.walk(tree))


def test_reader_module_is_gone():
    assert not (SRC / "reader.py").exists()


def test_read_whole_file_is_used_only_by_the_transitional_adapter():
    """D4b deletes `RepoTools.read_whole_file` together with
    `_read_adapter.py`; nothing else may come to depend on it."""
    users = {m.name for m in SRC.glob("*.py") if "read_whole_file" in m.read_text()}
    assert users == {"repo_tools.py", "_read_adapter.py"}
