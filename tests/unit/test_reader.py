"""Milestone 1: WorktreeFileReader tests — the read half of the
read-plan-approve-patch-verify-report vertical slice.

Real filesystem, real fixture repo. Not the Milestone 2 repository-read
toolkit; see reader.py's module docstring for the intentional scope
limits (one UTF-8 text file, bounded size, no listing/pagination/
search).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent.errors import ErrorCode
from codeagent.reader import MAX_READ_BYTES, WorktreeFileReader
from tests.support.fixture_repo import real_fixture_repo


def test_reads_a_real_file_from_the_worktree() -> None:
    with real_fixture_repo() as repo:
        reader = WorktreeFileReader(repo)
        result = reader.read_file("jobs/worker.py")

        assert result.success
        assert result.error is None
        assert result.content == (repo / "jobs" / "worker.py").read_text()
        assert result.byte_count == len((repo / "jobs" / "worker.py").read_bytes())


def test_rejects_nonexistent_worktree_path(tmp_path) -> None:
    with pytest.raises(ValueError):
        WorktreeFileReader(tmp_path / "does-not-exist")


def test_rejects_non_directory_worktree_path(tmp_path) -> None:
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x")
    with pytest.raises(ValueError):
        WorktreeFileReader(a_file)


def test_rejects_empty_relative_path() -> None:
    with real_fixture_repo() as repo:
        result = WorktreeFileReader(repo).read_file("")
        assert not result.success
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID


def test_rejects_absolute_path_without_reading_host_content(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret content")

    with real_fixture_repo() as repo:
        result = WorktreeFileReader(repo).read_file(str(secret))

        assert not result.success
        assert result.content is None
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID
        assert "absolute" in result.error.message
        # Fails closed before ever touching the host path — the
        # sanitized error message never echoes the secret's content.
        assert "host secret content" not in result.error.message


def test_rejects_dotdot_traversal_without_reading_host_content(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret content")

    with real_fixture_repo() as repo:
        result = WorktreeFileReader(repo).read_file("../secret.txt")

        assert not result.success
        assert result.content is None
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID
        assert ".." in result.error.message
        assert "host secret content" not in result.error.message


def test_rejects_nonexistent_nested_target_path(tmp_path) -> None:
    """A nested path whose intermediate directories don't exist either
    — distinct from test_rejects_nonexistent_target_file's single
    missing leaf. This does NOT exercise containment/escape: without a
    literal '..' or an actual symlink, Path.resolve() has nothing to
    escape through here, so this is purely a does-not-exist rejection.
    See test_rejects_symlink_escape_without_reading_host_content for
    the real containment-boundary evidence."""
    with real_fixture_repo() as repo:
        result = WorktreeFileReader(repo).read_file("nonexistent/deeper/file.py")

        assert not result.success
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID


def test_rejects_symlink_escape_without_reading_host_content() -> None:
    with real_fixture_repo() as repo:
        outside_target = Path(repo).parent / "outside_target.py"
        outside_target.write_text("outside secret content\n")
        link = Path(repo) / "jobs" / "evil_link.py"
        os.symlink(outside_target, link)

        result = WorktreeFileReader(repo).read_file("jobs/evil_link.py")

        assert not result.success
        assert result.content is None
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID
        assert "symlink" in result.error.message or "worktree" in result.error.message
        assert "outside secret content" not in result.error.message


def test_rejects_nonexistent_target_file() -> None:
    with real_fixture_repo() as repo:
        result = WorktreeFileReader(repo).read_file("jobs/does_not_exist.py")
        assert not result.success
        assert result.error.code is ErrorCode.TOOL_INPUT_INVALID


def test_rejects_oversized_file_as_structured_failure() -> None:
    with real_fixture_repo() as repo:
        big = Path(repo) / "jobs" / "big.py"
        big.write_bytes(b"x" * (MAX_READ_BYTES + 1))

        result = WorktreeFileReader(repo).read_file("jobs/big.py")

        assert not result.success
        assert result.content is None
        assert result.error.code is ErrorCode.TOOL_EXECUTION_FAILED
        assert "limit" in result.error.message


def test_read_never_requests_more_than_the_limit_plus_one_byte(monkeypatch) -> None:
    """Proves the bound is enforced on the actual read operation, not
    via a preceding stat() of the whole file: even against a file many
    times the limit, the reader must never ask the file object for more
    than MAX_READ_BYTES + 1 bytes in a single read() call — enough to
    detect "too large" without ever materializing the full file."""
    with real_fixture_repo() as repo:
        huge = Path(repo) / "jobs" / "huge.py"
        huge.write_bytes(b"y" * (MAX_READ_BYTES * 5))

        requested_sizes: list[int] = []
        real_open = Path.open

        def spy_open(self, *args, **kwargs):
            fh = real_open(self, *args, **kwargs)
            real_read = fh.read

            def spy_read(n=-1):
                requested_sizes.append(n)
                return real_read(n)

            fh.read = spy_read
            return fh

        monkeypatch.setattr(Path, "open", spy_open)

        result = WorktreeFileReader(repo).read_file("jobs/huge.py")

        assert not result.success
        assert result.error.code is ErrorCode.TOOL_EXECUTION_FAILED
        assert requested_sizes == [MAX_READ_BYTES + 1]


def test_accepts_file_exactly_at_the_byte_limit() -> None:
    with real_fixture_repo() as repo:
        exact = Path(repo) / "jobs" / "exact.py"
        exact.write_bytes(b"x" * MAX_READ_BYTES)

        result = WorktreeFileReader(repo).read_file("jobs/exact.py")

        assert result.success
        assert result.byte_count == MAX_READ_BYTES


def test_rejects_non_utf8_file_as_structured_failure_not_an_exception() -> None:
    with real_fixture_repo() as repo:
        binary_path = Path(repo) / "jobs" / "binary.py"
        binary_path.write_bytes(b"\xff\xfe\x00\x01not valid utf-8 \xfe")

        # Must not raise UnicodeDecodeError — must come back as a
        # structured ReadResult failure.
        result = WorktreeFileReader(repo).read_file("jobs/binary.py")

        assert not result.success
        assert result.content is None
        assert result.error.code is ErrorCode.TOOL_EXECUTION_FAILED
        assert "UTF-8" in result.error.message


def test_error_messages_never_contain_absolute_worktree_path(tmp_path) -> None:
    with real_fixture_repo() as repo:
        reader = WorktreeFileReader(repo)
        result = reader.read_file("/etc/passwd")
        assert not result.success
        assert str(repo) not in result.error.message


def test_each_failure_gets_a_distinct_error_id() -> None:
    with real_fixture_repo() as repo:
        reader = WorktreeFileReader(repo)
        first = reader.read_file("")
        second = reader.read_file("also/missing.py")
        assert first.error.error_id != second.error.error_id


def test_read_result_rejects_successful_construction_without_content() -> None:
    from codeagent.controller import ReadResult

    with pytest.raises(ValueError):
        ReadResult(success=True, content=None, byte_count=0)


def test_read_result_rejects_failed_construction_carrying_content() -> None:
    from codeagent.controller import ReadResult

    with pytest.raises(ValueError):
        ReadResult(success=False, content="x", byte_count=1, error=None)


def test_read_result_rejects_failed_construction_without_an_error() -> None:
    from codeagent.controller import ReadResult

    with pytest.raises(ValueError):
        ReadResult(success=False, content=None, byte_count=0, error=None)
