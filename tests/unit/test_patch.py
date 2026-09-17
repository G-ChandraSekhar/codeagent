"""Milestone 1 slice B: GitPatchApplier tests.

Real filesystem, real git — a controlled narrow patch operation against
a real worktree. Not the Milestone 2 patch engine; see patch.py's
module docstring for the intentional scope limits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

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
    from codeagent import _git_safety as git_safety_module

    real_run_git = git_safety_module.run_git

    def failing_run_git(worktree, *args, **kwargs):
        # Let everything else pass; fail on the staging call.
        if args and args[0] == "add":
            return subprocess.CompletedProcess(
                args=["git", "add"],
                returncode=128,
                stdout="",
                stderr=f"fatal: could not open {worktree}/.git/index",
            )
        return real_run_git(worktree, *args, **kwargs)

    monkeypatch.setattr(patch_module._git_safety, "run_git", failing_run_git)

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


# --------------------------------------------------------------------
# ADR 0006 patch.py-hardening slice
# --------------------------------------------------------------------


def test_apply_rejects_a_preexisting_dirty_staged_index() -> None:
    """The clean-staged-index precondition: an already-staged change
    present before apply() runs must fail closed, never be silently
    swept into the patch's checkpoint commit."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-dirty-baseline") as worktree:
            (worktree / "README.md").write_text("unexpected pre-existing change\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "README.md"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-dirty-baseline", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "staged changes" in result.error.message


def test_apply_rejects_oversized_replacement_content(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module

    monkeypatch.setattr(git_safety_module, "MAX_PATCH_BLOB_BYTES", 32)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-oversized") as worktree:
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-oversized", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "size limit" in result.error.message


def test_apply_rejects_a_target_with_an_unsafe_filter_attribute() -> None:
    """A real ADR 0006 finding, end to end through apply(): a target
    marked with a configured `filter` driver in `.gitattributes` must
    be refused before any write, never silently patched through the
    filter."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-unsafe-filter") as worktree:
            (worktree / ".gitattributes").write_text("jobs/worker.py filter=lfs\n")
            subprocess.run(
                ["git", "-C", str(worktree), "config", "filter.lfs.clean", "cat"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "add", ".gitattributes"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "add gitattributes"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-unsafe-filter", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "unsafe attribute" in result.error.message and "filter" in result.error.message


def test_apply_rejects_an_unstaged_gitattributes_edit_that_would_govern_add() -> None:
    """Round 4's dual-view requirement: an *unstaged* `.gitattributes`
    edit still governs `git add` — the working-tree attribute view must
    be checked, not only the staged one."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-unstaged-attr") as worktree:
            subprocess.run(
                ["git", "-C", str(worktree), "config", "filter.lfs.clean", "cat"],
                check=True,
                capture_output=True,
            )
            # Unstaged working-tree .gitattributes edit only -- never added.
            (worktree / ".gitattributes").write_text("jobs/worker.py filter=lfs\n")

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-unstaged-attr", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "unsafe attribute" in result.error.message and "filter" in result.error.message


def test_apply_uses_object_format_aware_hardened_commit_verification() -> None:
    """Real evidence the new commit-acceptance path actually runs: the
    resulting commit hash is exactly what `git rev-parse HEAD` reports
    post-apply, and the target blob's content in the real repository
    exactly matches what was requested -- both independently observed
    outside the module under test."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-verify") as worktree:
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-verify", 0, frozenset({"jobs/worker.py"}))

            real_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            real_blob = subprocess.run(
                ["git", "-C", str(worktree), "show", f"HEAD:jobs/worker.py"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

    assert result.success
    assert result.commit_hash == real_head
    assert REPLACEMENT_SNIPPET in real_blob


def test_apply_is_immune_to_a_preexisting_replacement_ref_on_the_parent_commit() -> None:
    """ADR 0006's replace-ref finding, exercised end to end: a
    replacement ref substituting the pre-patch HEAD's content must not
    affect commit acceptance, which must observe and verify against the
    real literal parent, not a replace-resolved one."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-replace-ref") as worktree:
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # A decoy commit with unrelated content, then a replace ref
            # substituting the real parent's content with the decoy's.
            (worktree / "README.md").write_text("decoy content\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "README.md"], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "decoy"],
                check=True,
                capture_output=True,
            )
            decoy = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(worktree), "reset", "--hard", before_head],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "replace", before_head, decoy],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-replace-ref", 0, frozenset({"jobs/worker.py"}))

    assert result.success
    assert result.error is None


def test_apply_rejects_a_target_with_a_live_eol_transformation() -> None:
    """Real positive control: `.gitattributes` `text eol=crlf` on the
    patch target must be refused before any write -- not just `filter`,
    per ADR 0006's full attribute set."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-eol-live") as worktree:
            (worktree / ".gitattributes").write_text("jobs/worker.py text eol=crlf\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", ".gitattributes"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "add gitattributes"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-eol-live", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "unsafe attribute" in result.error.message
    assert "eol" in result.error.message or "text" in result.error.message


def test_apply_rejects_a_target_with_a_live_ident_transformation() -> None:
    """Real positive control: `ident` keyword expansion on the patch
    target must be refused before any write."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-ident-live") as worktree:
            (worktree / ".gitattributes").write_text("jobs/worker.py ident\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", ".gitattributes"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "add gitattributes"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-ident-live", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "unsafe attribute" in result.error.message
    assert "ident" in result.error.message


# --------------------------------------------------------------------
# .gitattributes patch targets: categorically refused (ADR 0006
# Amendment 3's nested-masking finding — see module docstring and
# _check_repository_wide_attribute_safety's docstring for the real,
# reproduced probe this refusal is based on).
# --------------------------------------------------------------------


def _commit_gitattributes(worktree, content: str) -> None:
    (worktree / ".gitattributes").write_text(content)
    subprocess.run(
        ["git", "-C", str(worktree), "add", ".gitattributes"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
         "commit", "-qm", "gitattributes setup"],
        check=True,
        capture_output=True,
    )


def test_apply_refuses_a_top_level_gitattributes_patch_target_before_any_mutation() -> None:
    """Even a comment-only, individually "safe"-looking edit must be
    refused categorically -- there is currently no trusted way to
    confirm that classification isn't itself masked by the target's
    own content (see the nested-masking probe below)."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-attrs-refused") as worktree:
            _commit_gitattributes(worktree, "# marker v1\n")
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            applier = GitPatchApplier(
                worktree,
                (PatchOperation(".gitattributes", "# marker v1\n", "# marker v2\n"),),
            )
            result = applier.apply("r-attrs-refused", 0, frozenset({".gitattributes"}))

            after_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE
    assert "gitattributes" in result.error.message
    # Refused before any write/stage/commit -- HEAD never moved, and no
    # staged change was ever left behind.
    assert after_head == before_head


def test_apply_refuses_a_nested_gitattributes_patch_target() -> None:
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-nested-attrs-refused") as worktree:
            (worktree / "jobs" / ".gitattributes").write_text("# nested marker\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "jobs/.gitattributes"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "add nested gitattributes"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (
                    PatchOperation(
                        "jobs/.gitattributes", "# nested marker\n", "# nested marker v2\n"
                    ),
                ),
            )
            result = applier.apply(
                "r-nested-attrs-refused", 0, frozenset({"jobs/.gitattributes"})
            )

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE


def test_nested_gitattributes_can_really_mask_a_parent_rule_for_its_own_path(tmp_path) -> None:
    """The empirical finding this refusal is based on, reproduced
    directly against real Git (not through patch.py): a parent
    `.gitattributes` assigning `sub/.gitattributes filter=parent` is
    silently shadowed by a self-referential rule inside
    `sub/.gitattributes` itself, and `check-attr --cached` reports
    `unset` (safe) for `sub/.gitattributes` despite the parent's unsafe
    assignment -- proving a `.gitattributes` target's own attribute
    classification is not trustworthy when derived from that file's
    own staged content."""
    from codeagent import _git_safety as git_safety_module

    repo = tmp_path / "nested_probe"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "sub").mkdir()
    (repo / ".gitattributes").write_text("sub/.gitattributes filter=parent\n")
    (repo / "sub" / ".gitattributes").write_text(".gitattributes -filter\n")
    identity = ["-c", "user.name=t", "-c", "user.email=t@t.invalid"]
    subprocess.run(["git", "-C", str(repo)] + identity + ["add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo)] + identity + ["commit", "-qm", "init"],
        check=True,
        capture_output=True,
    )

    records = git_safety_module.check_attributes_for_paths(
        repo, ["sub/.gitattributes"], cached=True, attribute_names=("filter",)
    )

    assert records[0].value == "unset"  # masked -- the parent's filter=parent never surfaces


# --------------------------------------------------------------------
# Item 3: every GitSafetyError becomes a structured PatchResult
# --------------------------------------------------------------------


def test_apply_maps_an_injected_timeout_during_add_to_application_failure(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    real_run_git = git_safety_module.run_git

    def failing_run_git(worktree, *args, **kwargs):
        if args and args[0] == "add":
            raise git_safety_module.GitSafetyError(
                git_safety_module.GitSafetyFailure.GIT_COMMAND_TIMEOUT,
                "a git command did not finish within its time limit",
            )
        return real_run_git(worktree, *args, **kwargs)

    monkeypatch.setattr(patch_module._git_safety, "run_git", failing_run_git)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-timeout") as worktree:
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-timeout", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
    assert result.error.message == "failed to stage the patched file"


def test_apply_maps_an_injected_clean_baseline_timeout_to_substrate_failure(monkeypatch) -> None:
    """The same injected-timeout scenario at a pre-write call site
    (the clean-baseline check) must map to PATCH_UNSUPPORTED_GIT_SUBSTRATE,
    not silently escape as a raw GitSafetyError."""
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    real_run_git = git_safety_module.run_git

    def failing_run_git(worktree, *args, **kwargs):
        if args[:3] == ("diff", "--cached", "--quiet"):
            raise git_safety_module.GitSafetyError(
                git_safety_module.GitSafetyFailure.GIT_COMMAND_TIMEOUT,
                "a git command did not finish within its time limit",
            )
        return real_run_git(worktree, *args, **kwargs)

    monkeypatch.setattr(patch_module._git_safety, "run_git", failing_run_git)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-baseline-timeout") as worktree:
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-baseline-timeout", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE


def test_apply_maps_malformed_attribute_output_to_a_structured_failure(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    def failing_check_attributes(*args, **kwargs):
        raise git_safety_module.GitSafetyError(
            git_safety_module.GitSafetyFailure.ATTRIBUTE_RECORD_COUNT_MISMATCH,
            "git check-attr reported a different number of records than requested",
        )

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-malformed-attr") as worktree:
            # Applied only after entry -- GitWorktree's own entry-time
            # filter safety check must not be affected by this fault.
            monkeypatch.setattr(
                patch_module._git_safety, "check_attributes_for_paths", failing_check_attributes
            )
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-malformed-attr", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "could not be verified" in result.error.message


def test_apply_maps_filter_enumeration_failure_to_a_structured_failure(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    def failing_enumerate(*args, **kwargs):
        raise git_safety_module.GitSafetyError(
            git_safety_module.GitSafetyFailure.FILTER_ENUMERATION_UNAVAILABLE,
            "filter configuration could not be enumerated",
        )

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-enum-fail") as worktree:
            monkeypatch.setattr(
                patch_module._git_safety, "enumerate_filter_neutralization", failing_enumerate
            )
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-enum-fail", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "could not be verified" in result.error.message


def test_apply_maps_staged_entry_lookup_failure_to_application_failure(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    def failing_lookup(*args, **kwargs):
        raise git_safety_module.GitSafetyError(
            git_safety_module.GitSafetyFailure.INDEX_ENTRY_MALFORMED,
            "git ls-files --stage produced an unparseable record",
        )

    monkeypatch.setattr(patch_module._git_safety, "lookup_staged_entry", failing_lookup)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-staged-entry-fail") as worktree:
            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-staged-entry-fail", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
    assert "staged entry could not be confirmed" in result.error.message


def test_apply_maps_commit_verification_read_commit_header_failure(monkeypatch) -> None:
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    def failing_read_commit_header(*args, **kwargs):
        raise git_safety_module.GitSafetyError(
            git_safety_module.GitSafetyFailure.COMMIT_OBJECT_UNAVAILABLE,
            "the requested commit object is not available",
        )

    monkeypatch.setattr(patch_module._git_safety, "read_commit_header", failing_read_commit_header)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-commit-verify-fail") as worktree:
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            )
            result = applier.apply("r-commit-verify-fail", 0, frozenset({"jobs/worker.py"}))

            assert not result.success
            assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
            assert "structure could not be confirmed" in result.error.message

            # The commit really was created (verification failed, not
            # the commit itself) -- but apply() must never report it as
            # success, checked here before the worktree tears down.
            real_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert real_head != before_head


# --------------------------------------------------------------------
# Item 5: sanitize Path.resolve/is_file/is_symlink and UnicodeEncodeError
# --------------------------------------------------------------------


def test_apply_rejects_replacement_text_with_an_unpaired_surrogate() -> None:
    """A lone UTF-16 surrogate is a legal Python str character but not
    encodable to UTF-8 -- caller-provided (model-proposed) new_text
    must be rejected with a structured, sanitized failure, not an
    unhandled UnicodeEncodeError."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-surrogate") as worktree:
            result = GitPatchApplier(
                worktree,
                (
                    PatchOperation(
                        "jobs/worker.py", ORIGINAL_SNIPPET, "before\udcffafter"
                    ),
                ),
            ).apply("r-surrogate", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert "not valid UTF-8" in result.error.message


def test_apply_sanitizes_a_path_resolve_failure(monkeypatch) -> None:
    """A `Path.resolve()` failure (permission error, symlink cycle,
    etc.) must never leak an absolute path or the raw OSError into a
    persisted message, and — since the raw exception embeds an absolute
    path — must be suppressed (`from None`) so it never resurfaces via
    the complete formatted traceback either."""
    import traceback

    import codeagent.patch as patch_module

    real_resolve = Path.resolve
    secret_marker = "super-secret-absolute-path-fragment"

    def failing_resolve(self, *args, **kwargs):
        if self.name == "worker.py":
            raise OSError(f"simulated failure involving {secret_marker}")
        return real_resolve(self, *args, **kwargs)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-resolve-fail") as worktree:
            monkeypatch.setattr(Path, "resolve", failing_resolve)
            try:
                # White-box: call the actual sanitizing function
                # directly so the raised exception's __traceback__ and
                # __suppress_context__ can be inspected precisely.
                try:
                    patch_module._validate_path(Path(worktree), "jobs/worker.py")
                    raised = None
                except patch_module._ValidationFailure as exc:
                    raised = exc

                # Also confirm apply()'s own public result stays sanitized.
                result = GitPatchApplier(
                    worktree,
                    (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
                ).apply("r-resolve-fail", 0, frozenset({"jobs/worker.py"}))
            finally:
                monkeypatch.setattr(Path, "resolve", real_resolve)

    assert raised is not None
    assert secret_marker not in raised.message
    assert raised.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(type(raised), raised, raised.__traceback__))
    assert secret_marker not in formatted

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert secret_marker not in result.error.message


# --------------------------------------------------------------------
# Item 6: patch-level negative acceptance tests
# --------------------------------------------------------------------


def test_apply_runs_end_to_end_in_a_sha256_repository() -> None:
    """GitPatchApplier.apply must work against a SHA-256 repository,
    not just the SHA-1 default used everywhere else in this file --
    skipping only with a precise reason if the installed Git genuinely
    lacks support for `--object-format=sha256`."""
    with TemporaryDirectory(prefix="codeagent-sha256-fixture-") as tmp:
        repo = Path(tmp) / "sha256_repo"
        init = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", "--initial-branch=main", str(repo)],
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            pytest.skip(
                f"installed git does not support --object-format=sha256: {init.stderr.strip()}"
            )

        (repo / "worker.py").write_text(ORIGINAL_SNIPPET + "\n")
        identity = ["-c", "user.name=t", "-c", "user.email=t@t.invalid"]
        subprocess.run(
            ["git", "-C", str(repo)] + identity + ["add", "worker.py"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo)] + identity + ["commit", "-qm", "init"],
            check=True,
            capture_output=True,
        )

        with GitWorktree(repo, run_id="r-sha256") as worktree:
            applier = GitPatchApplier(
                worktree, (PatchOperation("worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),)
            )
            result = applier.apply("r-sha256", 0, frozenset({"worker.py"}))

            assert result.success
            assert result.error is None
            assert result.commit_hash is not None
            assert len(result.commit_hash) == 64  # SHA-256 hex length
            assert "# patched" in (worktree / "worker.py").read_text()


def test_apply_refuses_a_reported_commit_success_with_unchanged_head(monkeypatch) -> None:
    """A commit call that reports success (exit 0) but never actually
    advances HEAD -- observed once, structurally -- must be refused,
    even though the reported exit code claims success."""
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    real_run_git = git_safety_module.run_git

    def fake_commit_no_op(worktree, *args, **kwargs):
        if "commit" in args:
            # Never actually runs the real commit -- HEAD stays put --
            # but reports a fabricated success.
            return subprocess.CompletedProcess(args=["git", "commit"], returncode=0, stdout="", stderr="")
        return real_run_git(worktree, *args, **kwargs)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-fake-success") as worktree:
            monkeypatch.setattr(patch_module._git_safety, "run_git", fake_commit_no_op)
            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            ).apply("r-fake-success", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
    assert "did not advance HEAD" in result.error.message


def test_apply_accepts_a_reported_commit_failure_after_an_exact_valid_commit(monkeypatch) -> None:
    """The real commit genuinely succeeds and structurally verifies,
    but the invocation is made to *report* a nonzero exit anyway --
    this must still be accepted as success, per the documented policy
    that the commit's own reported exit code is not dispositive."""
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    real_run_git = git_safety_module.run_git

    def fake_commit_reports_failure(worktree, *args, **kwargs):
        if "commit" in args:
            real_result = real_run_git(worktree, *args, **kwargs)
            return subprocess.CompletedProcess(
                args=real_result.args, returncode=1, stdout=real_result.stdout, stderr="fake failure"
            )
        return real_run_git(worktree, *args, **kwargs)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-fake-failure") as worktree:
            before_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            monkeypatch.setattr(patch_module._git_safety, "run_git", fake_commit_reports_failure)
            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            ).apply("r-fake-failure", 0, frozenset({"jobs/worker.py"}))

    assert result.success
    assert result.error is None
    assert result.commit_hash is not None
    assert result.commit_hash != before_head


def test_apply_refuses_a_changed_head_with_the_wrong_parent(monkeypatch) -> None:
    """A HEAD that changed but does not structurally match the
    expected parent must be refused as contaminated/unconfirmed, never
    accepted merely because HEAD moved."""
    from codeagent import _git_safety as git_safety_module
    import codeagent.patch as patch_module

    real_run_git = git_safety_module.run_git
    real_observe_head = patch_module.GitPatchApplier._observe_head
    call_count = {"n": 0}

    def spoofing_observe_head(self, object_format):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # The second observation (post-commit) is spoofed to an
            # unrelated, but real and well-formed, commit -- forged by
            # creating a second, disconnected commit right now and
            # reporting its oid instead of the real new HEAD.
            decoy_repo = self._worktree_path
            subprocess.run(
                ["git", "-C", str(decoy_repo), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "--allow-empty", "-qm", "decoy", "--no-verify"],
                capture_output=True,
            )
            decoy_oid = subprocess.run(
                ["git", "-C", str(decoy_repo), "rev-parse", "HEAD~0"],
                capture_output=True, text=True,
            ).stdout.strip()
            # Reset back so the real commit created by apply() itself
            # is not the ref this spoofed observation points to.
            return decoy_oid
        return real_observe_head(self, object_format)

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-wrong-parent") as worktree:
            monkeypatch.setattr(
                patch_module.GitPatchApplier, "_observe_head", spoofing_observe_head
            )
            result = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/worker.py", ORIGINAL_SNIPPET, REPLACEMENT_SNIPPET),),
            ).apply("r-wrong-parent", 0, frozenset({"jobs/worker.py"}))

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_APPLICATION_FAILED
    assert "contaminated" in result.error.message


def test_apply_never_widens_a_glob_shaped_path_to_a_pathspec_sweep() -> None:
    """End-to-end literal-pathspec effectiveness: a target filename
    that happens to look like a glob pattern must never be
    reinterpreted by `add`/`write-tree`/`commit`'s pathspec handling in
    a way that sweeps in an unrelated decoy file."""
    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-literal-pathspec") as worktree:
            glob_named = worktree / "jobs" / "a.txt"
            decoy = worktree / "jobs" / "b.txt"
            glob_named.write_text("target marker\n")
            decoy.write_text("decoy marker\n")
            subprocess.run(
                ["git", "-C", str(worktree), "add", "jobs/a.txt", "jobs/b.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-qm", "add both files"],
                check=True,
                capture_output=True,
            )

            applier = GitPatchApplier(
                worktree,
                (PatchOperation("jobs/a.txt", "target marker\n", "patched marker\n"),),
            )
            result = applier.apply("r-literal-pathspec", 0, frozenset({"jobs/a.txt"}))

    assert result.success
    assert result.changed_paths == ("jobs/a.txt",)


def test_apply_sanitizes_a_real_symlink_loop_runtime_error() -> None:
    """A real symlink loop makes `Path.resolve()` raise `RuntimeError`
    (not `OSError`) on this Python -- confirmed independently:
    `Path.resolve()` on a real `a -> b -> a` cycle raises
    `RuntimeError: Symlink loop from '<absolute path>'`. This must be
    sanitized exactly like the OSError case: no absolute path in the
    persisted message, and suppressed from the complete formatted
    traceback."""
    import traceback

    import codeagent.patch as patch_module

    with real_fixture_repo() as repo:
        with GitWorktree(repo, run_id="r-real-symlink-loop") as worktree:
            loop_a = Path(worktree) / "jobs" / "loop_a"
            loop_b = Path(worktree) / "jobs" / "loop_b"
            loop_b.symlink_to("loop_a")
            loop_a.symlink_to("loop_b")

            # Confirm the premise directly before relying on it.
            with pytest.raises(RuntimeError):
                loop_a.resolve()

            try:
                patch_module._validate_path(Path(worktree), "jobs/loop_a")
                raised = None
            except patch_module._ValidationFailure as exc:
                raised = exc

            result = GitPatchApplier(
                worktree, (PatchOperation("jobs/loop_a", "x", "y"),)
            ).apply("r-real-symlink-loop", 0, frozenset({"jobs/loop_a"}))

    assert raised is not None
    assert str(worktree) not in raised.message
    assert raised.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(type(raised), raised, raised.__traceback__))
    assert str(worktree) not in formatted

    assert not result.success
    assert result.error.code is ErrorCode.PATCH_VALIDATION_FAILED
    assert str(worktree) not in result.error.message
