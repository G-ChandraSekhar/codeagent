"""Milestone 1 slice B: the real filesystem flow, end to end, through
RunController.

    real temporary repository
    -> real detached worktree
    -> controlled patch in worktree
    -> real Git checkpoint and diff
    -> original checkout unchanged
    -> worktree cleanup

The model, approval, and verifier collaborators are still fake (slice C
covers Docker verification and real model integration) — only the
patch application and the workspace it happens in are real.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from codeagent import domain, events
from codeagent.checkpoint_ref import CheckpointRef, new_lifecycle_id
from codeagent.checkpoint_session import CheckpointSession
from codeagent.controller import RunConfig, RunController, PlanProposal
from codeagent.evidence import FilesystemEvidenceSink, parse_artifact
from codeagent.patch import GitPatchApplier, PatchOperation
from codeagent.workspace import GitWorktree
from tests.support.fakes import (
    FakeApprovalProvider,
    FakeModel,
    FakeRepositoryReader,
    FakeVerifier,
    SteppingClock,
)
from tests.support.fixture_repo import real_fixture_repo

ORIGINAL_SNIPPET = (
    'if job.idempotency_key in already_processed:\n        return "duplicate"'
)
FIXED_SNIPPET = (
    "if job.idempotency_key in already_processed:\n"
    '        return "duplicate"\n'
    "    # (fixture) fix applied by codeagent"
)

PLAN = PlanProposal(
    problem_hypothesis="idempotency key dropped on retry",
    proposed_file_paths=("jobs/worker.py",),
    verification_intent="python3 -B -m unittest tests.test_worker",
)


def _status(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True
    ).stdout


def _head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_real_repo_to_worktree_to_patch_to_checkpoint_to_cleanup() -> None:
    with real_fixture_repo() as repo, tempfile.TemporaryDirectory(prefix="codeagent-evidence-") as evidence_root:
        before_status = _status(repo)
        before_head = _head(repo)
        before_content = (repo / "jobs" / "worker.py").read_text()

        worktree_lifecycle = GitWorktree(repo, run_id="r-slice-b")
        source_snapshot = worktree_lifecycle.snapshot_source()
        lifecycle_id = new_lifecycle_id()
        session = CheckpointSession(CheckpointRef(repo, lifecycle_id))

        with worktree_lifecycle as worktree_path:
            applier = GitPatchApplier(
                worktree_path,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, FIXED_SNIPPET),),
            )
            config = RunConfig(
                run_id="r-slice-b",
                task_statement="fix retry bug",
                approval_mode=domain.ApprovalMode.INTERACTIVE,
                repository_path=str(worktree_path),
                lifecycle_id=lifecycle_id,
            )
            controller = RunController(
                config,
                FakeModel(PLAN),
                FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
                FakeVerifier((events.VerificationOutcome.PASSED,)),
                applier,
                FakeRepositoryReader(),
                worktree_lifecycle,
                session,
                FilesystemEvidenceSink(Path(evidence_root)),
                clock=SteppingClock(),
            )

            finished = controller.run()

            # --- controlled patch in worktree ---
            assert finished.terminal_reason is domain.TerminalReason.VERIFICATION_PASSED
            assert finished.error is None
            sequences = [e.sequence for e in controller.log.events]
            assert sequences == list(range(len(sequences)))

            patch_applied = next(
                e for e in controller.log.events if isinstance(e, events.PatchApplied)
            )
            checkpoint_created = next(
                e for e in controller.log.events if isinstance(e, events.CheckpointCreated)
            )

            # --- real Git checkpoint and diff ---
            assert patch_applied.operation_count == 1
            assert patch_applied.files_changed == ("jobs/worker.py",)
            assert patch_applied.diff_bytes > 0
            assert checkpoint_created.checkpoint_id == patch_applied.checkpoint_id
            assert checkpoint_created.parent_checkpoint_id == source_snapshot.head
            assert checkpoint_created.commit_hash == patch_applied.checkpoint_id

            # By the time controller.run() returns, _terminate has
            # already disposed the worktree (a successful run is
            # cleaned up exactly like a failed one) — the patch's real
            # effect is verified through the durable evidence artifact
            # instead of reading the now-gone worktree path directly.
            evidence_captured = next(
                e for e in controller.log.events if isinstance(e, events.EvidenceCaptured)
            )
            assert evidence_captured.success is True
            artifact_path = Path(evidence_root) / f"{lifecycle_id}.evidence"
            header, payload = parse_artifact(artifact_path.read_bytes())
            assert header["status"] == "complete"
            assert b"fixture) fix applied" in payload

        # --- worktree and checkpoint-ref cleanup (real, via _terminate) ---
        assert worktree_lifecycle.path is None
        assert not worktree_path.exists()
        ref_listing = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", f"refs/codeagent/runs/{lifecycle_id}"],
            capture_output=True,
            text=True,
        ).stdout
        assert ref_listing.strip() == ""
        assert list(Path(evidence_root).iterdir()) == [artifact_path]

        # --- original checkout unchanged ---
        assert _status(repo) == before_status
        assert _head(repo) == before_head
        assert (repo / "jobs" / "worker.py").read_text() == before_content


def test_patch_validation_failure_leaves_worktree_and_original_unchanged_via_controller() -> None:
    """The controller-level error path (UNRECOVERABLE_ERROR) exercised
    with a real, failing GitPatchApplier instead of the fake one."""
    with real_fixture_repo() as repo, tempfile.TemporaryDirectory(prefix="codeagent-evidence-") as evidence_root:
        before_content = (repo / "jobs" / "worker.py").read_text()
        before_status = _status(repo)

        worktree_lifecycle = GitWorktree(repo, run_id="r-slice-b-fail")
        lifecycle_id = new_lifecycle_id()
        session = CheckpointSession(CheckpointRef(repo, lifecycle_id))

        with worktree_lifecycle as worktree_path:
            applier = GitPatchApplier(
                worktree_path,
                (PatchOperation("jobs/worker.py", "TEXT_NOT_PRESENT_ANYWHERE", "y"),),
            )
            config = RunConfig(
                run_id="r-slice-b-fail",
                task_statement="fix retry bug",
                approval_mode=domain.ApprovalMode.INTERACTIVE,
                repository_path=str(worktree_path),
                lifecycle_id=lifecycle_id,
            )
            controller = RunController(
                config,
                FakeModel(PLAN),
                FakeApprovalProvider((domain.ApprovalDecision.APPROVED,)),
                FakeVerifier((events.VerificationOutcome.PASSED,)),
                applier,
                FakeRepositoryReader(),
                worktree_lifecycle,
                session,
                FilesystemEvidenceSink(Path(evidence_root)),
                clock=SteppingClock(),
            )

            finished = controller.run()

            assert finished.terminal_reason is domain.TerminalReason.UNRECOVERABLE_ERROR
            assert finished.error is not None
            assert not any(isinstance(e, events.PatchApplied) for e in controller.log.events)
            assert not any(isinstance(e, events.CheckpointCreated) for e in controller.log.events)
            # _terminate has already disposed the worktree by the time
            # run() returns, exactly like the successful case — the
            # invariant this proves now is that the *original checkout*
            # (never the disposable worktree, which is gone either way)
            # is left untouched.
            assert worktree_lifecycle.path is None
            assert not worktree_path.exists()

        assert (repo / "jobs" / "worker.py").read_text() == before_content
        assert _status(repo) == before_status
        # No apply_patch was ever accepted, so no checkpoint ref was ever
        # created (ADR 0003 Amendment 2 section 1) — terminal teardown's
        # ref deletion is therefore a no-op, not a failure.
        ref_listing = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", f"refs/codeagent/runs/{lifecycle_id}"],
            capture_output=True,
            text=True,
        ).stdout
        assert ref_listing.strip() == ""
