"""ADR 0005 Amendment 1 (ledger D3): the test-only cancellation driver.

Runs `run_lifecycle_aware()` inside `cli.owned_cancellation()` in its own
process, so the real-signal tests (E1-E12) can deliver real SIGINT to its
process group and real SIGTERM to its exact PID. It lives under `tests/`
only; no production module imports it (pinned by its tests).

Usage: `python -m tests.support.cancellation_driver <config.json>`.

Synchronization uses file gates only: the driver writes `ready-*` markers
and blocks on `release-*` or acknowledgement-request files. Every wait has
a deadline that fails the run (never a correctness sleep). The signal
handler itself does no I/O: every file write happens in ordinary
main-thread gate code.

The only long-lived production child, the `docker start --attach` client,
has just its PID recorded (an attribute of the returned `Popen`, so no
syscall race) to `attach-<n>.json`; the parent inspects it while the
container is known to be running.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from codeagent import _lifecycle_run, cli, domain, events, executor
from codeagent import lifecycle_store as ls
from codeagent import reconciliation
from codeagent.controller import EventLog, PlanProposal, RunController, SystemClock
from codeagent.patch import GitPatchApplier, PatchOperation
from tests.support.fakes import BlockingCancellableApprovalProvider, FakeApprovalProvider, MarkerGatedFakeModel

BUGGY = (
    "    job.retry_count += 1\n"
    "    if job.idempotency_key in already_processed:\n"
    '        return "duplicate"\n'
    "    already_processed.add(job.idempotency_key)"
)
FIXED = (
    "    if job.idempotency_key in already_processed:\n"
    '        return "duplicate"\n'
    "    job.retry_count += 1\n"
    "    already_processed.add(job.idempotency_key)"
)
PLAN = PlanProposal(
    problem_hypothesis="idempotency key dropped on retry",
    proposed_file_paths=("jobs/worker.py",),
    verification_intent="python3 -B -m unittest tests.test_worker",
)
_DEADLINE_SECONDS = 300.0


class Gates:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, name: str, content: str = "") -> None:
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(content)
        os.replace(tmp, self.root / name)

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()

    def wait(self, predicate, what: str) -> None:
        deadline = time.monotonic() + _DEADLINE_SECONDS
        while not predicate():
            if time.monotonic() > deadline:
                self.write("driver-deadline", what)
                raise RuntimeError(f"driver gate deadline: {what}")
            time.sleep(0.01)


def main(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text())
    gates = Gates(Path(config["gates"]))
    scenario = config["scenario"]
    record: dict = {"driver_pid": os.getpid(), "driver_sid": os.getsid(0), "scenario": scenario}
    published: list = []
    attach_count = [0]
    current_container = [None]
    controller_box: list = []
    source_box: list = []

    # --- instrumentation (recording only, except the scenario wrappers) ---
    real_start = executor.DockerVerifier._start_and_inspect

    def start_and_inspect(self, container_id):
        current_container[0] = container_id
        return real_start(self, container_id)

    real_wait = executor.wait_cancellable

    def wait_cancellable(process, **kwargs):
        attach_count[0] += 1
        gates.write(
            f"attach-{attach_count[0]}.json",
            json.dumps({"pid": process.pid, "container_id": current_container[0]}),
        )
        return real_wait(process, **kwargs)

    executor.DockerVerifier._start_and_inspect = start_and_inspect
    executor.wait_cancellable = wait_cancellable

    for cls, method, kind in (
        (ls.LifecycleWorktreePublisher, "publish", "worktree"),
        (ls.LifecycleCheckpointRefPublisher, "publish", "checkpoint_ref"),
    ):
        real = getattr(cls, method)

        def wrapped(self, transition, _real=real, _kind=kind):
            published.append([_kind, transition.intent.value])
            return _real(self, transition)

        setattr(cls, method, wrapped)

    real_container_publish = ls.LifecycleContainerPublisher.publish

    def container_publish(self, *, role, intent, id):
        published.append(["container:" + role.value, intent.value])
        return real_container_publish(self, role=role, intent=intent, id=id)

    ls.LifecycleContainerPublisher.publish = container_publish
    for method in ("activate", "begin_cleanup", "complete"):
        real = getattr(ls.LifecycleOwnerStatePublisher, method)

        def owner(self, _real=real, _method=method):
            published.append(["owner", _method])
            return _real(self)

        setattr(ls.LifecycleOwnerStatePublisher, method, owner)

    class RecordingController(RunController):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            controller_box.append(self)

    _lifecycle_run.RunController = RecordingController

    def requests_received() -> int:
        return source_box[0].requests_received

    if scenario == "cleanup_gate":  # E7
        real_cleanup = executor.DockerVerifier._cleanup_by_id

        def cleanup_gate(self, container_id, name):
            base = requests_received()
            gates.write("ready-cleanup", str(base))
            acked = 0
            deadline = time.monotonic() + _DEADLINE_SECONDS
            while not gates.exists("release-cleanup"):
                if time.monotonic() > deadline:
                    gates.write("driver-deadline", "cleanup gate")
                    raise RuntimeError("cleanup gate deadline")
                # Serialized repeats (plan section 10.3): acknowledge signal k
                # only once ack-request-k exists and exactly base + k
                # requests were received; a jump past that is an error.
                count = requests_received()
                if count > base + acked + 1:
                    gates.write("ack-error", f"{count} {base} {acked}")
                elif gates.exists(f"ack-request-{acked + 1}") and count == base + acked + 1:
                    acked += 1
                    gates.write(f"ack-{acked}")
                time.sleep(0.005)
            record["cleanup_gate_acks"] = acked
            return real_cleanup(self, container_id, name)

        executor.DockerVerifier._cleanup_by_id = cleanup_gate
    elif scenario == "cleanup_skip":  # E8
        real_cleanup = executor.DockerVerifier._cleanup_by_id

        def cleanup_skip(self, container_id, name):
            # Only the cancelled owner's own removal is suppressed: no
            # `docker rm`, and no listing or identity observation, so the
            # real container remains. Earlier (baseline) cleanup is real.
            if source_box and source_box[0].is_cancelled():
                return False
            return real_cleanup(self, container_id, name)

        executor.DockerVerifier._cleanup_by_id = cleanup_skip
    elif scenario == "patch_signal":  # E9
        real_apply = GitPatchApplier.apply

        def apply(self, run_id, iteration, approved_paths):
            os.kill(os.getpid(), signal.SIGINT)
            return real_apply(self, run_id, iteration, approved_paths)

        GitPatchApplier.apply = apply
    elif scenario == "admission":  # E10
        real_reconcile = reconciliation.reconcile_repository

        def reconcile_gate(*args, **kwargs):
            gates.write("ready-reconcile")
            gates.wait(lambda: requests_received() >= 1, "admission signal")
            gates.write("ack-signal")
            gates.wait(lambda: gates.exists("release-reconcile"), "release-reconcile")
            return real_reconcile(*args, **kwargs)

        reconciliation.reconcile_repository = reconcile_gate

    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    log = EventLog()
    try:
        with cli.owned_cancellation(record_requests=True) as source:
            source_box.append(source)
            if config["approval"] == "blocking":
                approval = BlockingCancellableApprovalProvider(
                    source.token, on_wait=lambda: gates.write("ready-approval"), deadline_seconds=_DEADLINE_SECONDS
                )
            else:
                approval = FakeApprovalProvider((domain.ApprovalDecision.APPROVED,))
            result = _lifecycle_run.run_lifecycle_aware(
                config["repo"],
                run_id=config["run_id"],
                task_statement="fix retry bug",
                approval_mode=domain.ApprovalMode.INTERACTIVE,
                evidence_root=config["evidence"],
                patch_operations=(PatchOperation("jobs/worker.py", BUGGY, FIXED),),
                model=MarkerGatedFakeModel(read_path="jobs/worker.py", marker="# BUG:", plan=PLAN),
                approval=approval,
                verify_command=tuple(config["verify_command"]),
                clock=SystemClock(),
                event_log=log,
                cancellation=source.token,
            )
        finished = result.finished
        record.update(
            terminal_reason=finished.terminal_reason.value,
            error_code=None if finished.error is None else finished.error.code.value,
            iterations_used=finished.iterations_used,
            unconfirmed_stages=[s.value for s in result.unconfirmed_stages],
        )
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, which fails the test
        record["exception"] = "".join(traceback.format_exception(exc))
    record["handlers_restored"] = all(
        signal.getsignal(sig) is handler for sig, handler in original_handlers.items()
    )
    if source_box:
        record["candidates"] = list(source_box[0].candidates)
        record["requests_received"] = source_box[0].requests_received
    if controller_box:
        controller = controller_box[0]
        record["lifecycle_id"] = controller._c.lifecycle_id
        record["sites"] = [site for site, _ in controller._cancellation_sites]
    record["published"] = published
    record["events"] = [_summarize(e) for e in log.events]
    gates.write("result.json", json.dumps(record))


def _summarize(event: events.Event) -> dict:
    out = {"type": type(event).__name__}
    for field in ("outcome", "exit_code", "cleanup_status", "tool", "trigger", "decision"):
        value = getattr(event, field, None)
        if value is not None:
            out[field] = getattr(value, "value", value)
    return out


if __name__ == "__main__":
    main(sys.argv[1])
