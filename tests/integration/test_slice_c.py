"""Milestone 1 slice C: the real end-to-end flow, through RunController,
with real Docker verification.

    real temporary repository
    -> real detached worktree (read-only bind-mounted into containers)
    -> real failing baseline verification in Docker
    -> real READ_FILE against the real worktree, evidence passed to the
       deterministic fake model, which proposes its plan only after
       seeing the real bug marker
    -> fake approval
    -> real controlled patch and Git checkpoint
    -> real passing verification in Docker
    -> typed events -> report

This is the implementation guide's exact Milestone 1 requirement:
"Using a deterministic fake model and a real fixture repository,
perform a full read-plan-approve-patch-verify-report flow." Only the
model-decision and approval collaborators are fake — the read, the
worktree, the patch, the checkpoint, and the verification are all real.
Requires a working local Docker daemon; skipped otherwise so the suite
still runs on machines without Docker (no security or correctness
claim is weakened by skipping — there's simply no infrastructure to
test against). On CI (CODEAGENT_REQUIRE_DOCKER=1, see
.github/workflows/ci.yml), a missing daemon is instead a hard failure
— see `_docker_required` below — since Docker there is a verified
precondition, not an optional local convenience.

Uses `codeagent.controller.SystemClock` (the real clock), not
`SteppingClock`, for the Docker verifier and the controller driving it
— this is a real demonstration of real elapsed time, and reporting a
fabricated duration for a real Docker execution would defeat the
point. Durations are asserted finite and non-negative, never pinned to
an exact value.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from codeagent import domain, events
from codeagent.checkpoint_ref import CheckpointRef, new_lifecycle_id
from codeagent.checkpoint_session import CheckpointSession
from codeagent.controller import PlanProposal, RunConfig, RunController, SystemClock
from codeagent.evidence import FilesystemEvidenceSink, parse_artifact
from codeagent.executor import CONTAINER_NAME_PREFIX, DockerVerifier
from codeagent.patch import GitPatchApplier, PatchOperation
from codeagent.reader import WorktreeFileReader
from codeagent.report import build_report, render_json, render_text
from codeagent.workspace import GitWorktree
from tests.support.fakes import FIXTURE_VERIFY_COMMAND, FakeApprovalProvider, MarkerGatedFakeModel
from tests.support.fixture_repo import real_fixture_repo

# The real bug marker actually present in the fixture's committed
# jobs/worker.py (see tests/fixtures/retry_worker/jobs/worker.py) — the
# model's plan is genuinely gated on seeing this in the read evidence,
# not merely on being handed *some* content.
BUG_MARKER = "# BUG:"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_required() -> bool:
    """CI-only escape hatch (CODEAGENT_REQUIRE_DOCKER=1): on a runner
    where Docker is a mandatory, already-verified precondition (see the
    workflow's own `docker info` preflight step), a missing daemon here
    means the environment is broken, not merely "no infrastructure to
    test against" — these tests must fail loudly instead of silently
    skipping. Unset or any other value keeps the default local-dev
    behavior of skipping cleanly."""
    return os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1"


if not _docker_available() and _docker_required():
    pytest.fail(
        "CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available — "
        "this environment is expected to guarantee Docker; failing "
        "instead of skipping.",
        pytrace=False,
    )

pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="requires a running local Docker daemon"
)

BUGGY = (
    '    job.retry_count += 1\n'
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


def _assert_real_duration(seconds: float) -> None:
    assert math.isfinite(seconds)
    assert seconds >= 0


def _no_stray_containers() -> None:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    # Empty stdout from a *failed* `docker ps` (daemon down, permission
    # error, etc.) would look identical to "confirmed no containers" —
    # require the listing itself to have actually succeeded before
    # trusting its emptiness as evidence of cleanliness.
    assert result.returncode == 0, f"docker ps itself failed: {result.stderr!r}"
    assert result.stdout.strip() == "", f"leftover containers: {result.stdout!r}"


def test_real_docker_e2e_failing_baseline_then_passing_verification() -> None:
    with real_fixture_repo() as repo, tempfile.TemporaryDirectory(
        prefix="codeagent-evidence-"
    ) as evidence_root:
        before_status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
        ).stdout
        before_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        before_content = (repo / "jobs" / "worker.py").read_text()

        worktree_lifecycle = GitWorktree(repo, run_id="r-slice-c")
        lifecycle_id = new_lifecycle_id()
        session = CheckpointSession(CheckpointRef(repo, lifecycle_id))
        evidence_sink = FilesystemEvidenceSink(Path(evidence_root))

        with worktree_lifecycle as worktree_path:
            verifier = DockerVerifier(worktree_path, command=FIXTURE_VERIFY_COMMAND, clock=SystemClock())
            applier = GitPatchApplier(
                worktree_path,
                (PatchOperation("jobs/worker.py", BUGGY, FIXED),),
            )
            reader = WorktreeFileReader(worktree_path)
            model = MarkerGatedFakeModel(
                read_path="jobs/worker.py", marker=BUG_MARKER, plan=PLAN
            )
            config = RunConfig(
                run_id="r-slice-c",
                task_statement="fix retry bug",
                approval_mode=domain.ApprovalMode.INTERACTIVE,
                repository_path=str(worktree_path),
                lifecycle_id=lifecycle_id,
            )
            controller = RunController(
                config,
                model,
                FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
                verifier,
                applier,
                reader,
                worktree_lifecycle,
                session,
                evidence_sink,
                clock=SystemClock(),
            )

            finished = controller.run()

            assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
            assert finished.error is None
            _assert_real_duration(finished.total_duration_seconds)

            # --- read: real evidence actually reached the model ---
            assert model.last_read_result is not None
            assert model.last_read_result.success
            assert BUG_MARKER in (model.last_read_result.content or "")
            # And it's the real file's real (pre-patch) content, not a
            # stand-in — compare directly against the original
            # checkout's content, captured before the worktree existed.
            # (The read happens during EXPLORE, before EXECUTE applies
            # the patch, so it must match the *original* file, not the
            # patched one the worktree holds by the time run() returns.)
            assert model.last_read_result.content == before_content

            read_requested = next(
                e
                for e in controller.log.events
                if isinstance(e, events.ToolRequested) and e.tool is domain.ToolName.READ_FILE
            )
            read_completed = next(
                e
                for e in controller.log.events
                if isinstance(e, events.ToolCompleted) and e.tool is domain.ToolName.READ_FILE
            )
            plan_requested = next(
                e
                for e in controller.log.events
                if isinstance(e, events.ToolRequested) and e.tool is domain.ToolName.PROPOSE_PLAN
            )
            plan_proposed = next(
                e for e in controller.log.events if isinstance(e, events.PlanProposed)
            )
            assert read_completed.success
            # No raw content anywhere in the persisted event trace.
            assert "BUG" not in read_completed.result_summary
            assert plan_proposed.evidence_refs == (read_requested.tool_call_id,)
            # read → plan, in that order, by sequence number.
            assert read_requested.sequence < read_completed.sequence < plan_requested.sequence

            run_started = next(
                e for e in controller.log.events if isinstance(e, events.RunStarted)
            )
            baseline = next(
                e for e in controller.log.events if isinstance(e, events.BaselineRecorded)
            )
            final_verification = next(
                e for e in controller.log.events if isinstance(e, events.VerificationCompleted)
            )
            # The event trace must always record the command the
            # Verifier itself actually exposes and executed — not a
            # separately configured, possibly-disagreeing value.
            assert run_started.verify_command == verifier.command
            assert baseline.command == verifier.command
            assert final_verification.command == verifier.command

            assert baseline.outcome == events.VerificationOutcome.TEST_FAILURE
            assert baseline.exit_code == 1
            _assert_real_duration(baseline.duration_seconds)
            assert final_verification.outcome == events.VerificationOutcome.PASSED
            assert final_verification.exit_code == 0
            _assert_real_duration(final_verification.duration_seconds)

            patch_applied = next(
                e for e in controller.log.events if isinstance(e, events.PatchApplied)
            )

            report = build_report(controller.log.events)
            assert report.baseline_outcome == "test_failure"
            assert report.final_verification_outcome == "passed"
            assert report.changed_paths == ("jobs/worker.py",)
            assert report.checkpoint_commit == patch_applied.checkpoint_id
            assert "r-slice-c" in render_text(report)
            assert render_json(report)  # must not raise

            # --- durable evidence artifact (ADR 0003 Amendment 2) ---
            evidence_captured = next(
                e for e in controller.log.events if isinstance(e, events.EvidenceCaptured)
            )
            assert evidence_captured.success is True
            assert evidence_captured.complete is True
            artifact_path = Path(evidence_root) / f"{lifecycle_id}.evidence"
            assert artifact_path.exists()
            assert (artifact_path.stat().st_mode & 0o777) == 0o600
            header, payload = parse_artifact(artifact_path.read_bytes())
            assert header["schema_version"] == 1
            assert header["lifecycle_id"] == lifecycle_id
            assert header["status"] == "complete"
            assert header["complete"] is True
            assert header["bytes_total"] == len(payload)
            assert header["sha256_payload"] == hashlib.sha256(payload).hexdigest()
            assert b"job.retry_count += 1" in payload
            # --- worktree already disposed by _terminate, before run() returns ---
            assert worktree_lifecycle.path is None

        # --- cleanup after success: worktree, checkpoint ref, containers ---
        assert not worktree_path.exists()
        ref_listing = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", f"refs/codeagent/runs/{lifecycle_id}"],
            capture_output=True,
            text=True,
        ).stdout
        assert ref_listing.strip() == ""
        _no_stray_containers()

        # --- original checkout unchanged ---
        assert (repo / "jobs" / "worker.py").read_text() == before_content
        after_status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True
        ).stdout
        after_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        assert after_status == before_status
        assert after_head == before_head


def test_real_docker_baseline_outcome_matches_current_buggy_fixture() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-slice-c-baseline") as worktree_path:
            verifier = DockerVerifier(worktree_path, command=FIXTURE_VERIFY_COMMAND, clock=SystemClock())
            result = verifier.run_baseline()
            assert result.outcome == events.VerificationOutcome.TEST_FAILURE
            assert result.exit_code == 1
            assert result.error is None
            assert result.cleanup_status == events.ContainerCleanupStatus.CONFIRMED_ABSENT
            _assert_real_duration(result.duration_seconds)
        _no_stray_containers()


def test_real_docker_cleanup_after_container_level_test_failure() -> None:
    """Even when the container's own command genuinely fails (as
    opposed to a Docker/environment failure), the container itself
    must still be confirmed removed afterward."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-slice-c-cleanup-fail") as worktree_path:
            verifier = DockerVerifier(worktree_path, command=FIXTURE_VERIFY_COMMAND, clock=SystemClock())
            result = verifier.run_baseline()
            assert result.outcome == events.VerificationOutcome.TEST_FAILURE
        _no_stray_containers()
