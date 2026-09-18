"""Milestone 1 slice C: RunReport tests.

build_report() must derive every field purely from the typed event
trace — these tests run the real RunController against deterministic
fakes (the same _build() helper used by the controller integration
tests) and check the report against the resulting real event log,
rather than hand-assembling events.

The failure-path tests below take one real, valid trace and mutate it
(via dataclasses.replace, since events are frozen) into specific
invalid shapes — mixed run_id, a sequence gap or reordering, a missing
or duplicated RunStarted/RunFinished, trailing events after
RunFinished — to prove build_report actually rejects each, not just
the trivially empty case.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from codeagent import domain, events
from codeagent.controller import PlanProposal, RunConfig, RunController
from codeagent.report import build_report, render_json, render_text
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeCheckpointSession,
    FakeEvidenceSink,
    FakeModel,
    FakePatchApplier,
    FakeRepositoryReader,
    FakeVerifier,
    FakeWorkspace,
    SteppingClock,
)

_LIFECYCLE_ID = "b" * 32

PLAN = PlanProposal(
    problem_hypothesis="idempotency key dropped on retry",
    proposed_file_paths=("jobs/worker.py",),
    verification_intent="python3 -B -m unittest tests.test_worker",
)


def _build(
    run_id: str,
    *,
    approval_decisions=(domain.ApprovalDecision.APPROVED,),
    verification_outcomes=(events.VerificationOutcome.PASSED,),
    baseline_outcome=events.VerificationOutcome.TEST_FAILURE,
    patch_should_fail: bool = False,
) -> RunController:
    config = RunConfig(
        run_id=run_id,
        task_statement="fix retry bug",
        approval_mode=domain.ApprovalMode.INTERACTIVE,
        lifecycle_id=_LIFECYCLE_ID,
        max_repair_iterations=3,
        max_plan_revisions=2,
    )
    return RunController(
        config,
        FakeModel(PLAN),
        FakeApprovalProvider(approval_decisions),
        FakeVerifier(verification_outcomes, baseline_outcome=baseline_outcome),
        FakePatchApplier(patch_should_fail),
        FakeRepositoryReader(),
        FakeWorkspace(),
        FakeCheckpointSession(),
        FakeEvidenceSink(),
        clock=SteppingClock(),
    )


def test_report_reflects_successful_run() -> None:
    controller = _build("r-success")
    controller.run()

    report = build_report(controller.log.events)

    assert report.run_id == "r-success"
    assert report.terminal_reason == domain.TerminalReason.VERIFICATION_PASSED.value
    assert report.baseline_outcome == events.VerificationOutcome.TEST_FAILURE.value
    assert report.final_verification_outcome == events.VerificationOutcome.PASSED.value
    assert report.approval_decision == domain.ApprovalDecision.APPROVED.value
    assert report.changed_paths == ("jobs/worker.py",)
    assert report.checkpoint_commit is not None
    assert report.iterations_used >= 1
    assert report.elapsed_seconds >= 0
    assert report.error is None


def test_report_reflects_rejected_approval_with_no_patch_applied() -> None:
    controller = _build(
        "r-rejected",
        approval_decisions=(domain.ApprovalDecision.REJECTED,),
    )
    controller.run()

    report = build_report(controller.log.events)

    assert report.approval_decision == domain.ApprovalDecision.REJECTED.value
    assert report.changed_paths == ()
    assert report.checkpoint_commit is None


def test_report_reflects_persistent_test_failure_as_budget_exceeded() -> None:
    controller = _build(
        "r-repair-exhausted",
        verification_outcomes=(events.VerificationOutcome.TEST_FAILURE,) * 4,
    )
    controller.run()

    report = build_report(controller.log.events)

    assert report.terminal_reason == domain.TerminalReason.BUDGET_EXCEEDED.value
    assert report.final_verification_outcome == events.VerificationOutcome.TEST_FAILURE.value


def test_build_report_rejects_incomplete_trace() -> None:
    with pytest.raises(ValueError):
        build_report([])


def _valid_trace() -> list[events.Event]:
    controller = _build("r-valid-trace")
    controller.run()
    return list(controller.log.events)


def test_build_report_rejects_mixed_run_ids() -> None:
    trace = _valid_trace()
    trace[-1] = dataclasses.replace(trace[-1], run_id="a-different-run-id")
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_sequence_gap() -> None:
    trace = _valid_trace()
    trace[-1] = dataclasses.replace(trace[-1], sequence=trace[-1].sequence + 1)
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_reordered_sequence() -> None:
    trace = _valid_trace()
    trace[0], trace[1] = trace[1], trace[0]
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_missing_run_started() -> None:
    trace = _valid_trace()
    trace = [e for e in trace if not isinstance(e, events.RunStarted)]
    # Re-sequence so this failure is isolated to the missing event,
    # not incidentally also a sequence-gap failure.
    trace = [dataclasses.replace(e, sequence=i) for i, e in enumerate(trace)]
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_run_started_not_first() -> None:
    """RunStarted present exactly once is not enough — it must actually
    be the first event. Moves it to the middle of an otherwise-valid
    trace and re-sequences everything else by list order, so this
    failure is isolated to position, not incidentally a sequence-gap
    or count failure."""
    trace = _valid_trace()
    started = next(e for e in trace if isinstance(e, events.RunStarted))
    rest = [e for e in trace if not isinstance(e, events.RunStarted)]
    midpoint = len(rest) // 2
    reordered = rest[:midpoint] + [started] + rest[midpoint:]
    reordered = [dataclasses.replace(e, sequence=i) for i, e in enumerate(reordered)]
    with pytest.raises(ValueError):
        build_report(reordered)


def test_build_report_rejects_missing_run_finished() -> None:
    trace = _valid_trace()
    trace = trace[:-1]
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_duplicate_run_finished() -> None:
    trace = _valid_trace()
    duplicate = dataclasses.replace(trace[-1], sequence=trace[-1].sequence + 1)
    trace = trace + [duplicate]
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_rejects_events_after_run_finished() -> None:
    trace = _valid_trace()
    finished = trace[-1]
    # Duplicate a non-RunStarted event so this failure is isolated to
    # "something follows RunFinished," not incidentally also a
    # duplicate-RunStarted failure.
    source = next(e for e in trace if isinstance(e, events.ModelRequestStarted))
    extra = dataclasses.replace(source, sequence=finished.sequence + 1, state=finished.state)
    trace = trace + [extra]
    with pytest.raises(ValueError):
        build_report(trace)


def test_build_report_aggregates_multiple_patch_applied_events() -> None:
    """Two PatchApplied events (a repair loop touching different files
    across attempts): changed_paths must be the deterministic,
    duplicate-free union in first-seen order, and checkpoint_commit
    must be the latest one, not the first."""
    controller = _build(
        "r-multi-patch",
        verification_outcomes=(
            events.VerificationOutcome.TEST_FAILURE,
            events.VerificationOutcome.PASSED,
        ),
    )
    controller.run()
    trace = list(controller.log.events)

    patch_events = [e for e in trace if isinstance(e, events.PatchApplied)]
    assert len(patch_events) == 2

    report = build_report(trace)

    assert report.changed_paths == ("jobs/worker.py",)
    assert report.checkpoint_commit == patch_events[-1].checkpoint_id
    assert report.checkpoint_commit != patch_events[0].checkpoint_id


def test_render_text_contains_key_fields() -> None:
    controller = _build("r-text")
    controller.run()
    report = build_report(controller.log.events)

    text = render_text(report)

    assert "r-text" in text
    assert report.terminal_reason in text
    assert "jobs/worker.py" in text


def test_render_json_round_trips_all_fields() -> None:
    controller = _build("r-json")
    controller.run()
    report = build_report(controller.log.events)

    payload = json.loads(render_json(report))

    assert payload["run_id"] == "r-json"
    assert payload["changed_paths"] == ["jobs/worker.py"]
    assert payload["terminal_reason"] == report.terminal_reason
