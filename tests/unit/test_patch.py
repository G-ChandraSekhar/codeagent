"""Milestone 1 slice B: GitPatchApplier tests.

Real filesystem, real git — a controlled narrow patch operation against
a real worktree. Not the Milestone 2 patch engine; see patch.py's
module docstring for the intentional scope limits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeagent.errors import ErrorCode
from codeagent.patch import GitPatchApplier, PatchOperation
from codeagent.workspace import GitWorktree
from tests.support.fixture_repo import real_fixture_repo

ORIGINAL_SNIPPET = (
    'if job.idempotency_key in already_processed:\n        return "duplicate"'
)
REPLACEMENT_SNIPPET = (
    'if job.idempotency_key in already_processed:\n        return "duplicate"  # patched'
)


def _status(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True
    ).stdout


def test_successful_patch_produces_real_diff_and_commit() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-patch-ok") as worktree:
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-patch-ok", 0, frozenset({"jobs/worker.py"}))

            assert result.success
            assert result.error is None
            assert result.operation_count == 1
            assert result.changed_paths == ("jobs/worker.py",)
            assert result.diff_bytes > 0
            assert result.commit_hash is not None
            assert result.commit_hash != before_head

            content = (worktree / "jobs" / "worker.py").read_text()
            assert "# patched" in content

            log = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            assert log == result.commit_hash

            diff = subprocess.run(
                ["git", "-C", str(worktree), "show", "--stat", result.commit_hash],
                capture_output=True,
                text=True,
            ).stdout
            assert "worker.py" in diff


@pytest.mark.parametrize(
    ("relative_path", "expected_text", "reason_fragment"),
    [
        ("/etc/passwd", "x", "absolute"),
        ("../outside.py", "x", "'..'"),
        ("jobs/does_not_exist.py", "x", "does not exist"),
        ("jobs/worker.py", "return", "matches 2 times"),
        ("jobs/worker.py", "THIS_TEXT_IS_NOT_PRESENT", "not found"),
    ],
)
def test_validation_failures_leave_worktree_and_original_unchanged(
    relative_path: str, expected_text: str, reason_fragment: str
) -> None:
    with real_fixture_repo() as repo:
        original_before = (repo / "jobs" / "worker.py").read_text()
        original_status_before = _status(repo)

        with GitWorktree(repo, run_id="r-patch-fail") as worktree:
            worktree_before = (worktree / "jobs" / "worker.py").read_text()

            result = GitPatchApplier(
                worktree, (PatchOperation(relative_path, expected_text, "y"),)
            ).apply("r-patch-fail", 0, frozenset({relative_path}))

            assert not result.success
            assert result.error is not None
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert reason_fragment in result.error.message

            worktree_after = (worktree / "jobs" / "worker.py").read_text()
            assert worktree_after == worktree_before

        original_after = (repo / "jobs" / "worker.py").read_text()
        original_status_after = _status(repo)
        assert original_after == original_before
        assert original_status_after == original_status_before


def test_rejects_symlink_escape() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-symlink") as worktree:
            outside_target = Path(worktree).parent / "outside_target.py"
            outside_target.write_text("outside content\n")
            link = Path(worktree) / "jobs" / "evil_link.py"
            os.symlink(outside_target, link)

            result = GitPatchApplier(
                worktree, (PatchOperation("jobs/evil_link.py", "outside", "y"),)
            ).apply("r-symlink", 0, frozenset({"jobs/evil_link.py"}))

            # Caught either by the symlink-component check or by the
            # resolved-path-outside-worktree check (both run; whichever
            # fires first is an implementation detail) — what matters is
            # rejection, and that the outside file is never touched.
            assert not result.success
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert "worktree" in result.error.message or "symlink" in result.error.message
            assert outside_target.read_text() == "outside content\n"


def test_rejects_unapproved_target_before_any_write_or_commit() -> None:
    """The primary enforcement (not the controller's postcondition):
    GitPatchApplier must refuse to touch anything, and must never
    commit, when its configured operation's path isn't in
    approved_paths — checked before validation, before any read, before
    any write."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-unapproved") as worktree:
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            before_content = (worktree / "jobs" / "worker.py").read_bytes()

            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            ).apply("r-unapproved", 0, frozenset({"jobs/other_file.py"}))

            assert not result.success
            assert result.error is not None
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert "not in the approved plan" in result.error.message

            after_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            after_content = (worktree / "jobs" / "worker.py").read_bytes()

            assert after_content == before_content  # byte-identical, not just "equal text"
            assert after_head == before_head  # no commit was created
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1"],
                capture_output=True,
                text=True,
            ).stdout
            assert status == ""  # nothing staged, nothing modified


def test_rejects_empty_operations() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-empty-ops") as worktree:
            with pytest.raises(ValueError):
                GitPatchApplier(worktree, ())


def test_rejects_more_than_one_operation() -> None:
    """Author decision: exactly one operation. Multiple operations
    against the same file would each start from the original content
    and silently overwrite one another — deferred to Milestone 2's
    atomic multi-file patch engine, not partially built here."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-multi-ops") as worktree:
            with pytest.raises(ValueError):
                GitPatchApplier(
                    worktree,
                    (
                        PatchOperation("jobs/worker.py", "a", "b"),
                        PatchOperation("jobs/worker.py", "c", "d"),
                    ),
                )


def test_rejects_empty_expected_text() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-empty-expected") as worktree:
            result = GitPatchApplier(
                worktree, (PatchOperation("jobs/worker.py", "", "y"),)
            ).apply("r-empty-expected", 0, frozenset({"jobs/worker.py"}))
            assert not result.success
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert "nonempty" in result.error.message


def test_rejects_noop_replacement() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-noop") as worktree:
            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, ORIGINAL_SNIPPET),),
            ).apply("r-noop", 0, frozenset({"jobs/worker.py"}))
            assert not result.success
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert "no-op" in result.error.message or "differ" in result.error.message


def test_rejects_non_utf8_file_as_structured_failure_not_an_exception() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-non-utf8") as worktree:
            binary_path = Path(worktree) / "jobs" / "binary.py"
            binary_path.write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xfe")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "-A"], check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git", "-C", str(worktree), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "add binary",
                ],
                check=True,
                capture_output=True,
            )

            # Must not raise UnicodeDecodeError — must come back as a
            # structured PatchResult failure.
            result = GitPatchApplier(
                worktree, (PatchOperation("jobs/binary.py", "anything", "y"),)
            ).apply("r-non-utf8", 0, frozenset({"jobs/binary.py"}))
            assert not result.success
            assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
            assert "UTF-8" in result.error.message


def test_error_messages_never_contain_absolute_worktree_path_or_raw_git_stderr(
    monkeypatch,
) -> None:
    """A git-command failure during apply() must not leak the worktree's
    absolute filesystem path or raw git stderr into the persisted
    message — both are excluded categorically, not merely truncated."""
    import codeagent.patch as patch_module

    real_run_git = patch_module._run_git
    call_count = {"n": 0}

    def failing_run_git(worktree, *args):
        # Let the initial validation/read pass; fail on the first git
        # subprocess call (staging).
        if args and args[0] == "add":
            call_count["n"] += 1
            raise subprocess.CalledProcessError(
                1, ["git", "add"], output="", stderr=f"fatal: could not open {worktree}/.git/index"
            )
        return real_run_git(worktree, *args)

    monkeypatch.setattr(patch_module, "_run_git", failing_run_git)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-sanitized-error") as worktree:
            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            ).apply("r-sanitized-error", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
    assert str(worktree) not in result.error.message
    assert "fatal:" not in result.error.message
    assert ".git/index" not in result.error.message
    assert result.error.message == "failed to stage the patched file"
    assert call_count["n"] == 1


def test_patch_result_rejects_successful_construction_missing_commit_hash() -> None:
    from codeagent.controller import PatchResult

    with pytest.raises(ValueError):
        PatchResult(
            success=True,
            operation_count=1,
            changed_paths=("a.py",),
            diff_bytes=10,
            commit_hash=None,
        )


def test_patch_result_rejects_successful_construction_with_zero_operation_count() -> None:
    from codeagent.controller import PatchResult

    with pytest.raises(ValueError):
        PatchResult(
            success=True,
            operation_count=0,
            changed_paths=("a.py",),
            diff_bytes=10,
            commit_hash="abc123",
        )


def test_patch_result_rejects_failed_construction_carrying_success_metadata() -> None:
    from codeagent.controller import PatchResult
    from codeagent.errors import OperationalError

    with pytest.raises(ValueError):
        PatchResult(
            success=False,
            operation_count=1,  # a failure must report 0
            changed_paths=("a.py",),
            diff_bytes=10,
            commit_hash="abc123",
            error=OperationalError(
                code=ErrorCode.PATCH_VALIDATION_FAILED, error_id="e1", message="x"
            ),
        )


def test_patch_result_rejects_failed_construction_without_an_error() -> None:
    from codeagent.controller import PatchResult

    with pytest.raises(ValueError):
        PatchResult(success=False, operation_count=0, changed_paths=(), diff_bytes=0, error=None)
