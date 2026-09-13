"""Milestone 1 slice C: a small, deterministic report built only from
the typed event trace — never from anything the controller tracked
separately. `build_report` replays `EventLog.events`; if a field can't
be derived from the events, it isn't in the report.

`build_report` validates the trace's own shape before trusting it (see
`_validate_trace`): a report is only ever built from a trace that looks
like one real, complete, single run's log, never from a mixed,
reordered, or truncated one.

No frontend, no persistence beyond returning strings — `render_text`
and `render_json` are pure functions over one `RunReport` value.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from codeagent import events


@dataclass(frozen=True)
class RunReport:
    run_id: str
    terminal_reason: str
    baseline_outcome: str | None
    final_verification_outcome: str | None
    approval_decision: str | None
    changed_paths: tuple[str, ...]
    checkpoint_commit: str | None
    iterations_used: int
    elapsed_seconds: float
    error: dict[str, str] | None


def _validate_trace(event_list: list[events.Event]) -> None:
    if not event_list:
        raise ValueError("event trace must be nonempty")

    run_ids = {e.run_id for e in event_list}
    if len(run_ids) != 1:
        raise ValueError(f"event trace must have exactly one run_id, got {sorted(run_ids)!r}")

    for i in range(1, len(event_list)):
        prev_seq = event_list[i - 1].sequence
        cur_seq = event_list[i].sequence
        if cur_seq != prev_seq + 1:
            raise ValueError(
                "event trace sequence numbers must be contiguous in list order "
                f"(index {i - 1} has sequence {prev_seq!r}, index {i} has {cur_seq!r})"
            )

    started_indices = [i for i, e in enumerate(event_list) if isinstance(e, events.RunStarted)]
    if len(started_indices) != 1:
        raise ValueError(
            f"event trace must have exactly one RunStarted, got {len(started_indices)}"
        )
    if started_indices[0] != 0:
        raise ValueError("RunStarted must be the first event in the trace")

    finished_indices = [i for i, e in enumerate(event_list) if isinstance(e, events.RunFinished)]
    if len(finished_indices) != 1:
        raise ValueError(
            f"event trace must have exactly one RunFinished, got {len(finished_indices)}"
        )
    if finished_indices[0] != len(event_list) - 1:
        raise ValueError("RunFinished must be the final event in the trace")


def build_report(event_list: list[events.Event]) -> RunReport:
    """Derive a RunReport purely by scanning the typed event trace.
    Raises ValueError if the trace isn't a well-formed, complete,
    single run's log — see `_validate_trace`."""
    _validate_trace(event_list)

    baseline_outcome: str | None = None
    final_verification_outcome: str | None = None
    approval_decision: str | None = None
    changed_paths: list[str] = []
    seen_paths: set[str] = set()
    checkpoint_commit: str | None = None

    for event in event_list:
        if isinstance(event, events.BaselineRecorded):
            baseline_outcome = event.outcome.value
        elif isinstance(event, events.VerificationCompleted):
            final_verification_outcome = event.outcome.value
        elif isinstance(event, events.ApprovalRecorded):
            approval_decision = event.decision.value
        elif isinstance(event, events.PatchApplied):
            for path in event.files_changed:
                if path not in seen_paths:
                    seen_paths.add(path)
                    changed_paths.append(path)
            # The latest PatchApplied's checkpoint wins — later patches
            # supersede earlier checkpoints as the run's current state.
            checkpoint_commit = event.checkpoint_id

    finished = next(e for e in event_list if isinstance(e, events.RunFinished))

    error = None
    if finished.error is not None:
        error = {
            "code": finished.error.code.value,
            "error_id": finished.error.error_id,
            "message": finished.error.message,
        }

    return RunReport(
        run_id=finished.run_id,
        terminal_reason=finished.terminal_reason.value,
        baseline_outcome=baseline_outcome,
        final_verification_outcome=final_verification_outcome,
        approval_decision=approval_decision,
        changed_paths=tuple(changed_paths),
        checkpoint_commit=checkpoint_commit,
        iterations_used=finished.iterations_used,
        elapsed_seconds=finished.total_duration_seconds,
        error=error,
    )


def render_text(report: RunReport) -> str:
    lines = [
        f"Run:                {report.run_id}",
        f"Result:             {report.terminal_reason}",
        f"Baseline:           {report.baseline_outcome or '-'}",
        f"Final verification: {report.final_verification_outcome or '-'}",
        f"Approval:           {report.approval_decision or '-'}",
        f"Changed files:      {', '.join(report.changed_paths) or '-'}",
        f"Checkpoint commit:  {report.checkpoint_commit or '-'}",
        f"Iterations used:    {report.iterations_used}",
        f"Elapsed seconds:    {report.elapsed_seconds:.3f}",
    ]
    if report.error is not None:
        lines.append(f"Error:              {report.error['code']}: {report.error['message']}")
    return "\n".join(lines)


def render_json(report: RunReport) -> str:
    return json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))
