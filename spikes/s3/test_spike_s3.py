"""Small tests for spike_s3.py's own helper logic (validation,
subprocess-failure handling, and manifest comparison) -- not part of
the main CodeAgent test suite.

Not collected by the project's pytest config (pyproject.toml's
`testpaths = ["tests"]` excludes spikes/), and not imported by any
production code. Run directly:

    pytest spikes/s3/test_spike_s3.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from spike_s3 import (  # noqa: E402
    PatchOp,
    ValidationError,
    apply_one,
    git_head,
    git_status,
    manifest,
    run,
    validate_all,
    validate_op,
)


@pytest.fixture()
def scratch_dir(tmp_path: Path) -> Path:
    """Plain files, no git -- for the validate/apply helpers, which
    never touch git themselves."""
    (tmp_path / "a.py").write_text("def a():\n    return 'A_ORIGINAL'\n")
    (tmp_path / "b.py").write_text("def b():\n    return 'B_ORIGINAL'\n")
    return tmp_path


@pytest.fixture()
def git_scratch_dir(tmp_path: Path) -> Path:
    """A real minimal git repo -- for manifest()/git_head()/
    git_status(), which shell out to git and must be tested against a
    real repository, not a directory that merely happens to exist."""
    (tmp_path / "a.py").write_text("def a():\n    return 'A_ORIGINAL'\n")
    (tmp_path / "b.py").write_text("def b():\n    return 'B_ORIGINAL'\n")
    run(["git", "init", "-q"], cwd=tmp_path)
    run(["git", "config", "user.email", "spike@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "Spike"], cwd=tmp_path)
    run(["git", "add", "-A"], cwd=tmp_path)
    run(["git", "commit", "-q", "-m", "fixture"], cwd=tmp_path)
    return tmp_path


def test_validate_op_accepts_a_present_unique_match(scratch_dir: Path) -> None:
    op = PatchOp("a.py", "A_ORIGINAL", "A_PATCHED")
    validate_op(scratch_dir, op)  # must not raise


def test_validate_op_rejects_missing_file(scratch_dir: Path) -> None:
    op = PatchOp("does_not_exist.py", "x", "y")
    with pytest.raises(ValidationError, match="does not exist"):
        validate_op(scratch_dir, op)


def test_validate_op_rejects_text_not_found(scratch_dir: Path) -> None:
    op = PatchOp("a.py", "TEXT_NOT_PRESENT", "y")
    with pytest.raises(ValidationError, match="not found"):
        validate_op(scratch_dir, op)


def test_validate_op_rejects_ambiguous_match(tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text("X\nX\n")
    op = PatchOp("dup.py", "X", "Y")
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_op(tmp_path, op)


def test_validate_all_collects_every_error_not_just_the_first(scratch_dir: Path) -> None:
    ops = [
        PatchOp("a.py", "NOT_FOUND_1", "y"),
        PatchOp("b.py", "NOT_FOUND_2", "y"),
    ]
    errors = validate_all(scratch_dir, ops)
    assert len(errors) == 2


def test_validate_all_returns_empty_for_all_valid_ops(scratch_dir: Path) -> None:
    ops = [
        PatchOp("a.py", "A_ORIGINAL", "A_PATCHED"),
        PatchOp("b.py", "B_ORIGINAL", "B_PATCHED"),
    ]
    assert validate_all(scratch_dir, ops) == []


def test_validate_all_never_writes_anything(scratch_dir: Path) -> None:
    before = (scratch_dir / "a.py").read_bytes()
    ops = [PatchOp("a.py", "A_ORIGINAL", "A_PATCHED"), PatchOp("c.py", "x", "y")]
    validate_all(scratch_dir, ops)
    after = (scratch_dir / "a.py").read_bytes()
    assert before == after
    assert not (scratch_dir / "c.py").exists()


def test_apply_one_replaces_exactly_once(scratch_dir: Path) -> None:
    apply_one(scratch_dir, PatchOp("a.py", "A_ORIGINAL", "A_PATCHED"))
    assert "A_PATCHED" in (scratch_dir / "a.py").read_text()
    assert "A_ORIGINAL" not in (scratch_dir / "a.py").read_text()


# --------------------------------------------------------------------
# run(): fails loudly by default -- the core of this correction pass.
# --------------------------------------------------------------------


def test_run_raises_on_nonzero_exit_by_default(tmp_path: Path) -> None:
    """A failing git command (not a git repo at all) must raise, not
    return an empty/garbage CompletedProcess that a caller could
    mistake for real evidence."""
    with pytest.raises(subprocess.CalledProcessError):
        run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"])


def test_run_with_check_false_returns_nonzero_without_raising(tmp_path: Path) -> None:
    result = run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=False)
    assert result.returncode != 0


def test_git_head_raises_rather_than_returning_empty_string(tmp_path: Path) -> None:
    """The exact bug this correction pass closes: git_head() on a
    non-git directory must raise, not silently return "" -- an empty
    HEAD must never be readable as "matches another empty HEAD"."""
    with pytest.raises(subprocess.CalledProcessError):
        git_head(tmp_path)


def test_git_status_raises_rather_than_returning_empty_string(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        git_status(tmp_path)


def test_manifest_raises_for_a_non_git_directory(scratch_dir: Path) -> None:
    """manifest() must propagate the failure, not silently produce a
    manifest that looks valid for a directory that isn't a git repo at
    all."""
    with pytest.raises(subprocess.CalledProcessError):
        manifest(scratch_dir)


def test_manifest_equal_for_untouched_git_repo_across_two_calls(git_scratch_dir: Path) -> None:
    assert manifest(git_scratch_dir) == manifest(git_scratch_dir)


def test_manifest_detects_a_single_byte_change(git_scratch_dir: Path) -> None:
    m1 = manifest(git_scratch_dir)
    (git_scratch_dir / "a.py").write_text("def a():\n    return 'A_MUTATED'\n")
    m2 = manifest(git_scratch_dir)
    assert m1 != m2
    assert m1["files"]["a.py"]["sha256"] != m2["files"]["a.py"]["sha256"]
