"""Ledger D4b real-substrate acceptance: A20-A22 and R16 (D4 plan revision
1 §13, revisions 2-3 R16), through the internal lifecycle-aware
composition with a real fixture repository, real Git and real Docker.

The scripted model is gated on real evidence: each plan is proposed only
after a current-pass read of the real file, and only when the real
unittest output (baseline, then the failed verification) carries the
fixture's `CODEAGENT_FIXTURE_MARKER` for that stage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codeagent import _lifecycle_run as lr, domain, events
from codeagent.cancellation import CancellationSource
from codeagent.controller import EventLog, SystemClock
from codeagent.errors import ErrorCode
from codeagent.model_protocol import FeedbackKind
from codeagent.tool_schema import ProposedChange, ProposePlanArgs
from tests.support.fakes import FIXTURE_VERIFY_COMMAND, FakeApprovalProvider
from tests.support.fixture_repo import real_fixture_repo
from tests.support.scripted_model import propose_plan_call, read_file_call, turn
from tests.support.trace_assertions import assert_model_pairing, assert_tool_pairing


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if not _docker_available() and os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1":
    pytest.fail(
        "CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available — failing instead of skipping.",
        pytrace=False,
    )

pytestmark = pytest.mark.skipif(not _docker_available(), reason="requires a running local Docker daemon")

BASELINE_MARKER = "CODEAGENT_FIXTURE_MARKER retry_count_after_duplicate"
REPAIR_MARKER = "CODEAGENT_FIXTURE_MARKER retry_count_after_first_process"
INCREMENT = "    job.retry_count += 1\n"
DUPLICATE_RETURN = '        return "duplicate"\n'

# r1 deletes the increment (wrong: the first call no longer counts);
# r2 re-inserts it after the duplicate check (right).
R1 = ProposedChange("jobs/worker.py", INCREMENT, "")
R2 = ProposedChange("jobs/worker.py", DUPLICATE_RETURN, DUPLICATE_RETURN + INCREMENT)
# A21: r2 still expects the original text that r1 already removed.
STALE_R2 = ProposedChange("jobs/worker.py", INCREMENT + "    if job.idempotency_key", "    if job.idempotency_key")


def _plan(change: ProposedChange, read_id: str) -> ProposePlanArgs:
    return ProposePlanArgs(
        problem_hypothesis="retry_count is incremented for duplicate deliveries",
        evidence_refs=(read_id,),
        change=change,
        verification_intent="python3 -B -m unittest tests.test_worker",
        risk_notes="",
    )


def _current_read(context):
    for item in reversed(context.transcript):
        body = item.body
        if item.kind is FeedbackKind.TOOL_RESULT and item.pass_index == context.pass_index and body["ok"]:
            return body["tool_call_id"], "".join(text for _, text in body["payload"]["lines"])
    return None


class FixtureRepairModel:
    """Reads, then proposes r1 in pass 0 and `second` in pass 1, each gated
    on the real marker. `on_pass` runs at each pass's first request."""

    model_name = "fake-model-v1"

    def __init__(self, second: ProposedChange = R2, on_pass=None) -> None:
        self.second = second
        self.on_pass = on_pass
        self.gates: list[str] = []
        self.reads: list[str] = []

    def respond(self, context):
        found = _current_read(context)
        if found is None:
            if self.on_pass is not None:
                self.on_pass(context.pass_index)
            return turn(read_file_call("jobs/worker.py"))
        read_id, content = found
        self.reads.append(content)
        if context.pass_index == 0:
            baseline = context.baseline["stdout_tail"] + context.baseline["stderr_tail"]
            if BASELINE_MARKER not in baseline:
                raise AssertionError("baseline marker missing from the model-visible baseline tail")
            self.gates.append("baseline")
            return turn(propose_plan_call(_plan(R1, read_id)))
        failed = [i for i in context.transcript if i.kind is FeedbackKind.VERIFICATION_FAILED]
        tail = failed[-1].body["stdout_tail"] + failed[-1].body["stderr_tail"]
        if REPAIR_MARKER not in tail or BASELINE_MARKER in tail:
            raise AssertionError("verification marker missing from the model-visible failure tail")
        self.gates.append("verification")
        return turn(propose_plan_call(_plan(self.second, read_id)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state))
    with real_fixture_repo() as repo:
        yield repo, state, tmp_path / "evidence"
        for lifecycle_dir in state.glob("repos/*/runs/*"):
            for role in ("baseline", "verification"):
                subprocess.run(["docker", "rm", "-f", f"codeagent-{role}-{lifecycle_dir.name}"], capture_output=True)


def _invoke(env, model, cancellation=None):
    repo, _, evidence = env
    log = EventLog()
    kwargs = {} if cancellation is None else {"cancellation": cancellation}
    result = lr.run_lifecycle_aware(
        repo,
        run_id="r-d4b-e2e",
        task_statement="fix the retry counter for duplicate deliveries",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        evidence_root=evidence,
        model=model,
        approval=FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
        verify_command=FIXTURE_VERIFY_COMMAND,
        clock=SystemClock(),
        event_log=log,
        **kwargs,
    )
    assert_model_pairing(log.events)
    assert_tool_pairing(log.events)
    return result, log.events


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _assert_clean(env, trace) -> None:
    repo, state, _ = env
    (run_dir,) = state.glob("repos/*/runs/*")
    assert json.loads((run_dir / "lifecycle.json").read_text())["state"] == "COMPLETE"
    assert _git(repo, "for-each-ref", "refs/codeagent").strip() == ""
    worktrees = [line for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
    assert len(worktrees) == 1
    names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True).stdout
    assert run_dir.name not in names
    assert not list(state.glob(f"worktrees/*/{run_dir.name}"))
    assert all(e.cleanup_status is not events.ContainerCleanupStatus.UNCONFIRMED for e in trace if hasattr(e, "cleanup_status"))


def test_a20_r16_real_fail_then_repair_then_pass(env):
    repo, _, _ = env
    original = (repo / "jobs" / "worker.py").read_text()
    model = FixtureRepairModel()
    result, trace = _invoke(env, model)
    finished = result.finished
    assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED and finished.error is None
    assert finished.iterations_used == 2
    assert model.gates == ["baseline", "verification"]
    # Each pass read the real file as it then was.
    assert model.reads[0] == original
    assert INCREMENT not in model.reads[1]
    applied = [e for e in trace if isinstance(e, events.PatchApplied)]
    proposed = [e for e in trace if isinstance(e, events.PlanProposed)]
    assert [e.change_digest for e in applied] == [e.change_digest for e in proposed]
    outcomes = [e.outcome for e in trace if isinstance(e, events.VerificationCompleted)]
    assert outcomes == [events.VerificationOutcome.TEST_FAILURE, events.VerificationOutcome.PASSED]
    # The original checkout is untouched.
    assert (repo / "jobs" / "worker.py").read_text() == original
    assert _git(repo, "status", "--porcelain").strip() == ""
    _assert_clean(env, trace)


def test_a21_a_stale_expected_text_is_terminal(env):
    model = FixtureRepairModel(second=STALE_R2)
    result, trace = _invoke(env, model)
    finished = result.finished
    assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
    assert finished.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert len([e for e in trace if isinstance(e, events.PatchApplied)]) == 1
    _assert_clean(env, trace)


def test_a22_cancellation_during_a_pass_two_turn(env):
    source = CancellationSource()

    def on_pass(pass_index):
        if pass_index == 1:
            source.request("test")

    model = FixtureRepairModel(on_pass=on_pass)
    result, trace = _invoke(env, model, cancellation=source.token)
    finished = result.finished
    assert finished.terminal_reason is domain.TerminalReason.CANCELLED and finished.error is None
    assert model.gates == ["baseline"]
    requests = [e for e in trace if isinstance(e, events.ModelRequestStarted)]
    assert requests[-1].iteration == 1
    assert len([e for e in trace if isinstance(e, events.PatchApplied)]) == 1
    _assert_clean(env, trace)
