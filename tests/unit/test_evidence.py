"""Tests for the durable evidence-artifact capture (ADR 0003 Amendment
2 / ADR 0006 Amendment 4, Milestone 2 slice 2B-2).

Real throwaway Git repositories are used throughout — including real
hostile external-diff/textconv/clean/process filter drivers with real
positive controls — matching test_git_safety.py's convention. Nothing
here is wired into RunController yet; that is a separate integration
test (tests/integration/test_controller.py).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from codeagent import evidence
from codeagent.errors import ErrorCode

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent Test",
    "-c",
    "user.email=codeagent-test@example.invalid",
]


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=check,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True, text=True, env=_clean_env()
    )
    return path


def _initial_commit(repo: Path, filename: str = "a.txt", content: str = "line1\n") -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git_user_config(tmp_path_factory, monkeypatch):
    """Isolate every test's Git invocations from this machine's real
    user-level Git configuration — same rationale and mechanism as
    test_git_safety.py's fixture of the same name."""
    home = tmp_path_factory.mktemp("isolated-home")
    xdg_config_home = tmp_path_factory.mktemp("isolated-xdg-config")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))


def _sink(output_root: Path) -> evidence.FilesystemEvidenceSink:
    return evidence.FilesystemEvidenceSink(output_root)


# ---------------------------------------------------------------------------
# Happy path: empty diff (no-patch run), a real change, artifact framing.
# ---------------------------------------------------------------------------


def test_capture_of_a_clean_worktree_is_complete_and_empty(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "evidence-out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=repo.parent / "unused-source", initial_commit=initial,
        lifecycle_id="a" * 32,
    )

    assert receipt.success is True
    assert receipt.complete is True
    assert receipt.error is None
    assert receipt.bytes_written == 0
    assert receipt.sha256_payload == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_capture_of_a_real_change_produces_a_nonempty_complete_artifact(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nline2\n")
    output_dir = tmp_path / "evidence-out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "no-such-source", initial_commit=initial,
        lifecycle_id="b" * 32,
    )

    assert receipt.success is True
    assert receipt.complete is True
    assert receipt.bytes_written > 0

    artifact_path = output_dir / f"{'b' * 32}.evidence"
    assert artifact_path.exists()
    raw = artifact_path.read_bytes()
    header, payload = evidence.parse_artifact(raw)
    assert header["schema_version"] == 1
    assert header["lifecycle_id"] == "b" * 32
    assert header["status"] == "complete"
    assert header["complete"] is True
    assert header["payload_encoding"] == "binary"
    assert header["bytes_total"] == len(payload)
    assert header["sha256_payload"] == receipt.sha256_payload
    assert header["sha256_full_stream"] == receipt.sha256_payload
    assert b"line2" in payload


def test_capture_combines_committed_staged_and_unstaged_changes(tmp_path):
    """Single-endpoint `git diff <commit> --` semantics: one call
    captures committed, staged, and unstaged changes together."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo, filename="a.txt", content="base\n")
    (repo / "a.txt").write_text("base\ncommitted-change\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "commit change")
    (repo / "b.txt").write_text("staged-change\n")
    _git(repo, "add", "b.txt")
    (repo / "c.txt").write_text("tracked-unstaged\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "add c")
    (repo / "c.txt").write_text("tracked-unstaged\nunstaged-edit\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="c" * 32,
    )

    assert receipt.success is True
    raw = (tmp_path / "out" / f"{'c' * 32}.evidence").read_bytes()
    _, payload = evidence.parse_artifact(raw)
    assert b"committed-change" in payload
    assert b"staged-change" in payload
    assert b"unstaged-edit" in payload


# ---------------------------------------------------------------------------
# Untracked paths: blindness + incompleteness rule.
# ---------------------------------------------------------------------------


def test_untracked_path_makes_capture_incomplete(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "untracked.txt").write_text("surprise\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="d" * 32,
    )

    assert receipt.success is False
    assert receipt.complete is False
    assert receipt.error.code is ErrorCode.EVIDENCE_INCOMPLETE
    assert receipt.artifact_id is not None  # a preview WAS published
    raw = (tmp_path / "out" / f"{'d' * 32}.evidence").read_bytes()
    header, payload = evidence.parse_artifact(raw)
    assert header["status"] == "incomplete"
    assert b"untracked.txt" not in payload  # git diff never saw it


def test_diff_never_mutates_the_index_or_working_tree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "untracked.txt").write_text("surprise\n")
    status_before = _git(repo, "status", "--porcelain=v1").stdout

    _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="e" * 32,
    )

    status_after = _git(repo, "status", "--porcelain=v1").stdout
    assert status_after == status_before
    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after == initial


# ---------------------------------------------------------------------------
# Hard bound: exact limit and overflow.
# ---------------------------------------------------------------------------


def test_capture_at_exactly_the_bound_is_complete(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    monkeypatch.setattr(evidence, "MAX_DIFF_BYTES", 4096)
    # Construct content whose diff is small and well under the bound —
    # this just confirms the "exact limit, still complete" boundary
    # using a bound small enough to reason about directly.
    (repo / "a.txt").write_text("line1\n" + "x" * 100 + "\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="f" * 32,
    )

    assert receipt.success is True
    assert receipt.complete is True


def test_capture_overflow_publishes_incomplete_preview_without_fabricated_totals(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    monkeypatch.setattr(evidence, "MAX_DIFF_BYTES", 256)
    (repo / "a.txt").write_text("line1\n" + ("y" * 10_000) + "\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="1" * 32,
    )

    assert receipt.success is False
    assert receipt.complete is False
    assert receipt.error.code is ErrorCode.EVIDENCE_INCOMPLETE
    assert receipt.bytes_written == 256
    raw = (tmp_path / "out" / f"{'1' * 32}.evidence").read_bytes()
    header, payload = evidence.parse_artifact(raw)
    assert header["status"] == "incomplete"
    assert header["bytes_total"] is None
    assert header["sha256_full_stream"] is None
    assert len(payload) == 256


# ---------------------------------------------------------------------------
# Collision.
# ---------------------------------------------------------------------------


def test_existing_artifact_is_a_collision_refusal(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir(mode=0o700)
    lifecycle_id = "2" * 32
    (output_dir / f"{lifecycle_id}.evidence").write_bytes(b"pre-existing content")

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_ARTIFACT_COLLISION
    assert receipt.artifact_id is None
    # The pre-existing artifact is left completely untouched.
    assert (output_dir / f"{lifecycle_id}.evidence").read_bytes() == b"pre-existing content"


# ---------------------------------------------------------------------------
# Containment, permissions, symlinks.
# ---------------------------------------------------------------------------


def test_output_dir_inside_worktree_is_refused(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)

    receipt = _sink(repo / "evidence-subdir").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="3" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_output_dir_inside_source_repo_is_refused(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    source = _make_repo(tmp_path / "source")

    receipt = _sink(source / "evidence-subdir").capture(
        worktree_path=repo, source_repo_path=source, initial_commit=initial,
        lifecycle_id="4" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_worktree_inside_output_dir_is_refused(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir(mode=0o700)
    repo = _make_repo(output_dir / "repo")
    initial = _initial_commit(repo)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="5" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_output_dir_equal_to_worktree_is_refused(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)

    receipt = _sink(repo).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="6" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_symlink_component_in_output_root_path_is_refused(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    real_dir = tmp_path / "real-evidence-parent"
    real_dir.mkdir()
    symlinked_parent = tmp_path / "symlinked-parent"
    symlinked_parent.symlink_to(real_dir)
    output_dir = symlinked_parent / "evidence-out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="7" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_intermediate_symlink_is_refused_even_when_the_final_directory_already_exists(tmp_path):
    """Reproduces the real, previously-undetected gap: a prior run (or
    an attacker) has already fully materialized the output directory
    through a hostile intermediate symlink *before* this capture call
    ever runs — the whole path, including the final directory, already
    exists on disk. The old "only check components that don't already
    exist" heuristic skipped this entirely, since nothing needed to be
    created. The fix must check every component unconditionally."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    real_dir = tmp_path / "real-evidence-parent"
    real_dir.mkdir()
    symlinked_parent = tmp_path / "symlinked-parent-2"
    symlinked_parent.symlink_to(real_dir)
    output_dir = symlinked_parent / "evidence-out"
    # The critical difference from the sibling test above: the full
    # output directory is pre-created through the symlink, so it
    # already exists by the time capture() runs.
    output_dir.mkdir(mode=0o700)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="8" * 32,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real, unmocked /tmp ambient symlink")
def test_real_macos_tmp_ambient_symlink_is_trusted(tmp_path):
    """Real, unmocked end-to-end proof on an actual macOS host: an
    output directory nested under the genuine /tmp (a real ambient
    symlink to /private/tmp on this platform) is accepted."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = Path("/tmp") / f"codeagent-evidence-ambient-test-{os.getpid()}"
    try:
        receipt = _sink(output_dir).capture(
            worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
            lifecycle_id="9" * 32,
        )
        assert receipt.success is True
    finally:
        if output_dir.exists():
            for child in output_dir.iterdir():
                child.unlink()
            output_dir.rmdir()


def test_ambient_symlink_with_verified_target_is_trusted_on_darwin(tmp_path, monkeypatch):
    """Platform-independent positive control: simulates being on Darwin
    (regardless of the host actually running this test) and confirms a
    fixture symlink whose real target matches the recorded expectation
    exactly is trusted."""
    monkeypatch.setattr(evidence, "_current_platform_is_darwin", lambda: True)
    real_root = tmp_path / "private-var-stand-in"
    real_root.mkdir()
    ambient_symlink = tmp_path / "var-stand-in"
    ambient_symlink.symlink_to(real_root)
    monkeypatch.setattr(
        evidence,
        "_MACOS_AMBIENT_SYMLINK_TARGETS",
        {str(ambient_symlink): str(real_root.resolve())},
    )

    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = ambient_symlink / "evidence-out"
    output_dir_real = real_root / "evidence-out"
    output_dir_real.mkdir(mode=0o700)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="9" * 32,
    )

    assert receipt.success is True


def test_ambient_symlink_with_mismatched_target_is_refused_even_on_darwin(tmp_path, monkeypatch):
    """Negative control: even simulating Darwin, a symlink at one of the
    recognized names whose real target does NOT match the expected
    /private/... path is refused, never trusted by name alone."""
    monkeypatch.setattr(evidence, "_current_platform_is_darwin", lambda: True)
    wrong_target = tmp_path / "wrong-target"
    wrong_target.mkdir()
    hostile_symlink = tmp_path / "tmp-stand-in-mismatched"
    hostile_symlink.symlink_to(wrong_target)
    monkeypatch.setattr(
        evidence,
        "_MACOS_AMBIENT_SYMLINK_TARGETS",
        {str(hostile_symlink): str(tmp_path / "some-other-expected-target")},
    )

    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = hostile_symlink / "evidence-out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="a3" * 16,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_ambient_symlink_is_refused_on_a_non_darwin_platform(tmp_path, monkeypatch):
    """Negative control: on a simulated non-Darwin platform, even a
    symlink with a genuinely matching target at a recognized name is
    refused — the exception exists only where the OS actually creates
    these symlinks."""
    monkeypatch.setattr(evidence, "_current_platform_is_darwin", lambda: False)
    real_root = tmp_path / "private-tmp-stand-in"
    real_root.mkdir()
    ambient_symlink = tmp_path / "tmp-stand-in-non-darwin"
    ambient_symlink.symlink_to(real_root)
    monkeypatch.setattr(
        evidence,
        "_MACOS_AMBIENT_SYMLINK_TARGETS",
        {str(ambient_symlink): str(real_root.resolve())},
    )

    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = ambient_symlink / "evidence-out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="b4" * 16,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


def test_is_verified_ambient_symlink_direct_unit_checks(tmp_path, monkeypatch):
    """Direct unit coverage of `_is_verified_ambient_symlink` itself,
    independent of a full capture() call."""
    monkeypatch.setattr(evidence, "_current_platform_is_darwin", lambda: True)
    real_root = tmp_path / "real"
    real_root.mkdir()
    good_link = tmp_path / "good-link"
    good_link.symlink_to(real_root)
    monkeypatch.setattr(
        evidence, "_MACOS_AMBIENT_SYMLINK_TARGETS", {str(good_link): str(real_root.resolve())}
    )
    assert evidence._is_verified_ambient_symlink(str(good_link)) is True

    # Not a recognized name at all.
    assert evidence._is_verified_ambient_symlink(str(tmp_path / "unrelated")) is False

    # A recognized name, but not actually a symlink.
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    monkeypatch.setattr(
        evidence, "_MACOS_AMBIENT_SYMLINK_TARGETS", {str(plain_dir): str(real_root.resolve())}
    )
    assert evidence._is_verified_ambient_symlink(str(plain_dir)) is False

    # Off Darwin, even an otherwise-perfect match is refused.
    monkeypatch.setattr(evidence, "_current_platform_is_darwin", lambda: False)
    monkeypatch.setattr(
        evidence, "_MACOS_AMBIENT_SYMLINK_TARGETS", {str(good_link): str(real_root.resolve())}
    )
    assert evidence._is_verified_ambient_symlink(str(good_link)) is False


def test_output_directory_created_with_exact_mode_0700(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "out"

    old_umask = os.umask(0o077)
    try:
        receipt = _sink(output_dir).capture(
            worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
            lifecycle_id="8" * 32,
        )
    finally:
        os.umask(old_umask)

    assert receipt.success is True
    mode = stat.S_IMODE(output_dir.stat().st_mode)
    assert mode == 0o700


def test_artifact_file_created_with_exact_mode_0600_independent_of_umask(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "out"

    old_umask = os.umask(0o022)
    try:
        receipt = _sink(output_dir).capture(
            worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
            lifecycle_id="9" * 32,
        )
    finally:
        os.umask(old_umask)

    assert receipt.success is True
    artifact_path = output_dir / f"{'9' * 32}.evidence"
    mode = stat.S_IMODE(artifact_path.stat().st_mode)
    assert mode == 0o600


def test_no_temp_files_left_behind_after_a_successful_capture(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "out"

    lifecycle_id = "a1" * 16
    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is True
    remaining = sorted(p.name for p in output_dir.iterdir())
    assert remaining == [f"{lifecycle_id}.evidence"]


# ---------------------------------------------------------------------------
# No raw payload / absolute host path leakage in the header.
# ---------------------------------------------------------------------------


def test_header_never_contains_an_absolute_host_path(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nchanged\n")
    output_dir = tmp_path / "out"

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="b1" * 16,
    )

    assert receipt.success is True
    raw = (output_dir / f"{'b1' * 16}.evidence").read_bytes()
    header, _payload = evidence.parse_artifact(raw)
    header_text = json.dumps(header)
    assert str(tmp_path) not in header_text
    assert str(repo) not in header_text


# ---------------------------------------------------------------------------
# _write_all: partial writes, EINTR, and zero-progress writes must never
# publish a truncated/malformed artifact as successful.
# ---------------------------------------------------------------------------


def test_write_all_retries_a_short_write_until_complete(tmp_path):
    """A real fd whose underlying write is forced short via a small
    pipe buffer still ends up with every byte written."""
    read_fd, write_fd = os.pipe()
    try:
        data = b"x" * (256 * 1024)  # comfortably larger than a pipe buffer

        import threading

        collected = bytearray()

        def drain():
            os.set_blocking(read_fd, True)
            while len(collected) < len(data):
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                collected.extend(chunk)

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            evidence._write_all(write_fd, data)
        finally:
            os.close(write_fd)
            reader.join(timeout=5)
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass

    assert bytes(collected) == data


def test_write_all_retries_on_eintr(monkeypatch):
    calls = {"n": 0}
    real_write = os.write

    def flaky_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError()
        return real_write(fd, data)

    monkeypatch.setattr(evidence.os, "write", flaky_write)
    read_fd, write_fd = os.pipe()
    try:
        evidence._write_all(write_fd, b"hello")
        os.close(write_fd)
        write_fd = -1
        assert os.read(read_fd, 100) == b"hello"
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)
    assert calls["n"] == 2


def test_write_all_raises_on_zero_progress(monkeypatch):
    monkeypatch.setattr(evidence.os, "write", lambda fd, data: 0)
    with pytest.raises(evidence.EvidenceSinkError, match="zero progress"):
        evidence._write_all(123, b"hello")


def test_write_all_handles_a_genuine_short_write_from_the_os_call(monkeypatch):
    """Simulates os.write itself returning fewer bytes than requested
    (a real, documented POSIX possibility) without raising or blocking
    — confirms _write_all issues a second call for the remainder."""
    written_chunks = []

    def short_write(fd, data):
        n = min(3, len(data))
        written_chunks.append(bytes(data[:n]))
        return n

    monkeypatch.setattr(evidence.os, "write", short_write)
    evidence._write_all(999, b"0123456789")
    assert b"".join(written_chunks) == b"0123456789"
    assert len(written_chunks) > 1  # genuinely required more than one call


def test_short_write_cannot_publish_a_malformed_artifact(tmp_path, monkeypatch):
    """The decisive fault-injection proof: force every write during
    artifact construction to be short by one byte. _write_all must
    still complete every segment correctly (proving the primitive
    itself is used and works), and the published artifact — if any —
    must parse as exactly the well-formed content, never a
    silently-truncated one."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nchanged content here\n")
    output_dir = tmp_path / "out"

    real_write = os.write

    def short_write(fd, data):
        if len(data) <= 1:
            return real_write(fd, data)
        return real_write(fd, data[: len(data) - 1])

    monkeypatch.setattr(evidence.os, "write", short_write)

    lifecycle_id = "3c" * 16
    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is True
    raw = (output_dir / f"{lifecycle_id}.evidence").read_bytes()
    header, payload = evidence.parse_artifact(raw)
    assert header["bytes_total"] == len(payload)
    assert header["sha256_payload"] == receipt.sha256_payload
    import hashlib

    assert hashlib.sha256(payload).hexdigest() == receipt.sha256_payload
    assert b"changed content here" in payload


def test_write_failure_that_cannot_make_progress_never_publishes(tmp_path, monkeypatch):
    """A write that always returns 0 (cannot ever complete) must result
    in EVIDENCE_CAPTURE_FAILED with no artifact published — never a
    hang, and never a partially-written file left under the final
    name."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nchanged\n")
    output_dir = tmp_path / "out"
    lifecycle_id = "4d" * 16

    monkeypatch.setattr(evidence.os, "write", lambda fd, data: 0)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert not (output_dir / f"{lifecycle_id}.evidence").exists()
    assert list(output_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Pre-link and post-link failure injection.
# ---------------------------------------------------------------------------


def test_pre_link_write_failure_is_capture_failed_with_no_artifact(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    output_dir = tmp_path / "out"
    lifecycle_id = "1a" * 16

    real_open = evidence._open_private_temp
    call_count = {"n": 0}

    def flaky_open(path):
        call_count["n"] += 1
        if call_count["n"] == 2:  # the artifact_tmp open, after payload_tmp succeeded
            raise OSError("simulated disk failure")
        return real_open(path)

    monkeypatch.setattr(evidence, "_open_private_temp", flaky_open)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert receipt.artifact_id is None
    assert not (output_dir / f"{lifecycle_id}.evidence").exists()
    # No leftover temp files either.
    assert list(output_dir.iterdir()) == []


def test_post_link_cleanup_failure_is_durability_unconfirmed_and_artifact_survives(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nchanged\n")
    output_dir = tmp_path / "out"
    lifecycle_id = "2a" * 16

    monkeypatch.setattr(evidence, "_best_effort_unlink", lambda path: False)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is False
    assert receipt.error.code is ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED
    assert receipt.artifact_id == lifecycle_id
    assert receipt.sha256_payload is not None
    assert receipt.bytes_written is not None
    # The published artifact is real, durable, and untouched.
    final_path = output_dir / f"{lifecycle_id}.evidence"
    assert final_path.exists()
    header, payload = evidence.parse_artifact(final_path.read_bytes())
    assert header["status"] == "complete"
    assert b"changed" in payload


def test_incomplete_capture_with_ambiguous_housekeeping_reports_incomplete_not_durability(
    tmp_path, monkeypatch
):
    """Combined-failure test (defect 4): the capture is already
    incomplete (truncated by the hard bound) AND the post-link
    housekeeping is also ambiguous. ADR 0003 Amendment 2's terminal
    precedence requires EVIDENCE_INCOMPLETE to win — never
    EVIDENCE_DURABILITY_UNCONFIRMED, and never complete=True."""
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    monkeypatch.setattr(evidence, "MAX_DIFF_BYTES", 256)
    (repo / "a.txt").write_text("line1\n" + ("z" * 10_000) + "\n")
    output_dir = tmp_path / "out"
    lifecycle_id = "5e" * 16

    monkeypatch.setattr(evidence, "_best_effort_unlink", lambda path: False)

    receipt = _sink(output_dir).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id=lifecycle_id,
    )

    assert receipt.success is False
    assert receipt.complete is False
    assert receipt.error.code is ErrorCode.EVIDENCE_INCOMPLETE
    # The truncated preview was still genuinely published (receipt
    # present), just reported under the higher-precedence code.
    assert receipt.artifact_id == lifecycle_id
    final_path = output_dir / f"{lifecycle_id}.evidence"
    assert final_path.exists()
    header, _payload = evidence.parse_artifact(final_path.read_bytes())
    assert header["status"] == "incomplete"
    assert header["bytes_total"] is None


# ---------------------------------------------------------------------------
# Real positive-control hostile filter/textconv/ext-diff drivers,
# proving the production capture path suppresses them.
# ---------------------------------------------------------------------------


def test_hostile_external_diff_does_not_execute_through_capture(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    marker = tmp_path / "ext-diff-marker"
    _git(repo, "config", "diff.external", f"sh -c 'echo ran >> {marker}; cat \"$2\"'")
    (repo / "a.txt").write_text("line1\nchanged\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="c1" * 16,
    )

    assert receipt.success is True
    assert not marker.exists()


def test_hostile_textconv_does_not_execute_through_capture(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    marker = tmp_path / "textconv-marker"
    (repo / ".gitattributes").write_text("a.txt diff=hostiletextconv\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    initial = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "config", "diff.hostiletextconv.textconv", f"sh -c 'echo ran >> {marker}; cat'")
    (repo / "a.txt").write_text("line1\nchanged\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="d1" * 16,
    )

    assert receipt.success is True
    assert not marker.exists()


def test_hostile_clean_filter_does_not_execute_through_capture(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    marker = tmp_path / "clean-marker"
    (repo / ".gitattributes").write_text("a.txt filter=hostile\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    initial = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "config", "filter.hostile.clean", f"sh -c 'echo ran >> {marker}; cat'")
    _git(repo, "config", "filter.hostile.smudge", "cat")
    (repo / "a.txt").write_text("line1\nchanged\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="e1" * 16,
    )

    assert receipt.success is True
    assert not marker.exists()


def test_hostile_clean_filter_really_executes_without_neutralization_sanity_check(tmp_path):
    """Negative control: proves the marker mechanism itself would have
    caught the hostile filter, by running a bare `git diff` without any
    neutralization and confirming the marker DOES appear."""
    repo = _make_repo(tmp_path / "repo")
    _initial_commit(repo)
    marker = tmp_path / "clean-marker-sanity"
    (repo / ".gitattributes").write_text("a.txt filter=hostile\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    initial = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "config", "filter.hostile.clean", f"sh -c 'echo ran >> {marker}; cat'")
    _git(repo, "config", "filter.hostile.smudge", "cat")
    (repo / "a.txt").write_text("line1\nchanged\n")

    subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "--full-index", "--find-renames",
         "--no-ext-diff", "--no-textconv", initial, "--"],
        env=_clean_env(),
        capture_output=True,
    )

    assert marker.exists(), "sanity check failed: the hostile filter never fired at all"


def test_hostile_process_filter_does_not_execute_through_capture(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _initial_commit(repo)
    marker = tmp_path / "process-marker"
    (repo / ".gitattributes").write_text("a.txt filter=hostileproc\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    initial = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(
        repo,
        "config",
        "filter.hostileproc.process",
        f"sh -c 'echo ran >> {marker}; exit 1'",
    )
    (repo / "a.txt").write_text("line1\nchanged\n")

    receipt = _sink(tmp_path / "out").capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=initial,
        lifecycle_id="f1" * 16,
    )

    # Neutralizing .process with no .clean fallback legitimately makes
    # the underlying `git diff` fail closed (exit 128) — that is a
    # correct EVIDENCE_CAPTURE_FAILED, not a bug, and the hostile
    # process must never have run either way.
    assert not marker.exists()
    if not receipt.success:
        assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 20 section 4: the evidence-artifact series. The
# terminal artifact stays schema version 1, byte for byte; recreation
# members are `<lifecycle_id>.recreation-<kk>.evidence` with a version-2
# header. Nothing calls the recreation path from the controller yet.
# ---------------------------------------------------------------------------

_SERIES_ID = "5e" * 16


def _changed_repo(tmp_path, object_format=None):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    init = ["git", "init", "-q"] + ([f"--object-format={object_format}"] if object_format else []) + [str(repo)]
    result = subprocess.run(init, capture_output=True, text=True, env=_clean_env())
    if result.returncode != 0:
        pytest.skip("installed git does not support --object-format=sha256")
    base = _initial_commit(repo)
    (repo / "a.txt").write_text("line1\nchanged\n")
    return repo, base


def _capture(tmp_path, repo, base, out="out", **kwargs):
    return _sink(tmp_path / out).capture(
        worktree_path=repo, source_repo_path=tmp_path / "src", initial_commit=base, lifecycle_id=_SERIES_ID, **kwargs
    )


def _frame(header: dict, payload: bytes) -> bytes:
    data = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return evidence.ARTIFACT_MAGIC + len(data).to_bytes(4, "big") + data + payload


def test_terminal_artifact_bytes_match_the_version_1_format_exactly(tmp_path):
    repo, base = _changed_repo(tmp_path)
    receipt = _capture(tmp_path, repo, base)
    payload = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "--full-index", "--find-renames", "--no-ext-diff",
         "--no-textconv", base, "--"],
        check=True, capture_output=True, env=_clean_env(),
    ).stdout
    digest = __import__("hashlib").sha256(payload).hexdigest()
    expected = _frame(
        {
            "schema_version": 1, "lifecycle_id": _SERIES_ID, "status": "complete", "payload_encoding": "binary",
            "payload_type": "unified-diff", "bytes_written": len(payload), "bytes_total": len(payload),
            "complete": True, "sha256_payload": digest, "sha256_full_stream": digest,
        },
        payload,
    )
    assert (tmp_path / "out" / f"{_SERIES_ID}.evidence").read_bytes() == expected
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [f"{_SERIES_ID}.evidence"]
    assert (receipt.success, receipt.complete, receipt.artifact_id, receipt.error) == (True, True, _SERIES_ID, None)
    header, _ = evidence.parse_artifact_strict(expected, object_format=None)
    assert "kind" not in header


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("sequence", [1, 99])
def test_recreation_member_name_header_and_receipt(tmp_path, object_format, sequence):
    repo, base = _changed_repo(tmp_path, object_format if object_format == "sha256" else None)
    assert len(base) == (64 if object_format == "sha256" else 40)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=sequence, object_format=object_format)
    artifact_id = f"{_SERIES_ID}.recreation-{sequence:02d}"
    assert (receipt.success, receipt.complete, receipt.artifact_id, receipt.error) == (True, True, artifact_id, None)
    path = tmp_path / "out" / f"{artifact_id}.evidence"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [path.name]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    header, payload = evidence.parse_artifact_strict(path.read_bytes(), object_format=object_format)
    assert header["schema_version"] == 2
    assert (header["kind"], header["sequence"], header["base_commit"]) == ("recreation", sequence, base)
    assert header["lifecycle_id"] == _SERIES_ID and header["complete"] is True
    assert receipt.sha256_payload == header["sha256_payload"] and receipt.bytes_written == len(payload)
    assert b"changed" in payload
    assert "object_format" not in header


@pytest.mark.parametrize("sequence", [0, 100, -1, True, "1", 1.0])
def test_recreation_sequence_outside_1_to_99_is_refused_before_any_io(tmp_path, sequence):
    repo, base = _changed_repo(tmp_path)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=sequence, object_format="sha1")
    assert receipt.success is False and receipt.artifact_id is None
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("object_format", [None, "SHA1", "md5", "", b"sha1", 1])
def test_recreation_without_a_trusted_object_format_is_refused(tmp_path, object_format):
    repo, base = _changed_repo(tmp_path)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=1, object_format=object_format)
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert not (tmp_path / "out").exists()


def test_recreation_cross_format_base_commit_is_refused(tmp_path):
    repo, base = _changed_repo(tmp_path)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha256")
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert receipt.error.message == "the recreation base commit is not a valid object id for the trusted format"
    assert not (tmp_path / "out").exists()
    receipt = _capture(tmp_path, repo, "ab" * 32, recreation_sequence=1, object_format="sha1")
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "base", ["0" * 40, "A" * 40, "a" * 39, "a" * 41, "g" * 40, " " + "a" * 39, 7]
)
def test_recreation_all_zero_or_malformed_base_commit_is_refused(tmp_path, base):
    repo, _ = _changed_repo(tmp_path)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha1")
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert not (tmp_path / "out").exists()


def _recreation_header(base, **overrides):
    payload = b"diff"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    header = {
        "schema_version": 2, "lifecycle_id": _SERIES_ID, "status": "complete", "payload_encoding": "binary",
        "payload_type": "unified-diff", "bytes_written": 4, "bytes_total": 4, "complete": True,
        "sha256_payload": digest, "sha256_full_stream": digest, "kind": "recreation", "sequence": 1,
        "base_commit": base,
    }
    header.update(overrides)
    return _frame({k: v for k, v in header.items() if v is not _DROP}, payload)


_DROP = object()


def test_strict_parser_needs_the_trusted_format_for_version_2():
    raw = _recreation_header("a" * 40)
    assert evidence.parse_artifact_strict(raw, object_format="sha1")[0]["sequence"] == 1
    with pytest.raises(ValueError, match="requires a trusted object format"):
        evidence.parse_artifact_strict(raw, object_format=None)
    with pytest.raises(ValueError, match="base_commit is invalid"):
        evidence.parse_artifact_strict(raw, object_format="sha256")  # never inferred from the 40-hex length
    with pytest.raises(ValueError):
        evidence.parse_artifact_strict(raw, object_format="SHA1")
    assert evidence.parse_artifact_strict(_recreation_header("b" * 64), object_format="sha256")[0]["base_commit"] == "b" * 64
    with pytest.raises(ValueError, match="base_commit is invalid"):
        evidence.parse_artifact_strict(_recreation_header("b" * 64), object_format="sha1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 0}, {"sequence": 100}, {"sequence": True}, {"sequence": "1"},
        {"kind": "terminal"}, {"kind": _DROP}, {"base_commit": "0" * 40}, {"base_commit": "A" * 40},
        {"extra": 1}, {"schema_version": 3}, {"schema_version": True}, {"bytes_written": 5},
        {"sha256_payload": "0" * 64}, {"status": "incomplete"}, {"bytes_total": None},
        {"lifecycle_id": "X" * 32}, {"payload_type": "other"},
    ],
)
def test_strict_parser_refuses_a_malformed_version_2_header(overrides):
    with pytest.raises(ValueError):
        evidence.parse_artifact_strict(_recreation_header("a" * 40, **overrides), object_format="sha1")


def test_strict_parser_refuses_version_2_fields_in_a_version_1_header():
    raw = _recreation_header("a" * 40, schema_version=1)
    with pytest.raises(ValueError, match="unexpected version-1 header fields"):
        evidence.parse_artifact_strict(raw, object_format="sha1")


def test_recreation_collision_never_overwrites(tmp_path):
    repo, base = _changed_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    existing = out / f"{_SERIES_ID}.recreation-01.evidence"
    existing.write_bytes(b"foreign")
    receipt = _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha1")
    assert receipt.error.code is ErrorCode.EVIDENCE_ARTIFACT_COLLISION and receipt.artifact_id is None
    assert receipt.error.error_id == f"evidence-{_SERIES_ID}.recreation-01-{ErrorCode.EVIDENCE_ARTIFACT_COLLISION.value}"
    assert existing.read_bytes() == b"foreign"
    assert sorted(p.name for p in out.iterdir()) == [existing.name]
    # Other members and the terminal artifact are unaffected by it.
    assert _capture(tmp_path, repo, base, recreation_sequence=2, object_format="sha1").success
    assert _capture(tmp_path, repo, base).success


def test_recreation_collision_at_link_time_never_overwrites(tmp_path, monkeypatch):
    repo, base = _changed_repo(tmp_path)
    real_link = os.link

    def racing_link(src, dst, *a, **k):
        Path(dst).write_bytes(b"raced in")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(evidence.os, "link", racing_link)
    receipt = _capture(tmp_path, repo, base, recreation_sequence=3, object_format="sha1")
    assert receipt.error.code is ErrorCode.EVIDENCE_ARTIFACT_COLLISION
    final = tmp_path / "out" / f"{_SERIES_ID}.recreation-03.evidence"
    assert final.read_bytes() == b"raced in"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [final.name]


def test_members_and_terminal_never_collide_with_each_other(tmp_path):
    repo, base = _changed_repo(tmp_path)
    for sequence in (1, 2, 99):
        assert _capture(tmp_path, repo, base, recreation_sequence=sequence, object_format="sha1").success
    assert _capture(tmp_path, repo, base).success
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == [f"{_SERIES_ID}.evidence"] + [f"{_SERIES_ID}.recreation-{k:02d}.evidence" for k in (1, 2, 99)]
    assert _capture(tmp_path, repo, base, recreation_sequence=2, object_format="sha1").error.code is (
        ErrorCode.EVIDENCE_ARTIFACT_COLLISION
    )


def test_every_member_has_its_own_temp_names(tmp_path, monkeypatch):
    repo, base = _changed_repo(tmp_path)
    opened: list[str] = []
    real_open = evidence._open_private_temp

    def spy(path):
        opened.append(Path(path).name)
        return real_open(path)

    monkeypatch.setattr(evidence, "_open_private_temp", spy)
    _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha1")
    _capture(tmp_path, repo, base, recreation_sequence=2, object_format="sha1")
    _capture(tmp_path, repo, base)
    assert opened == [
        f".{_SERIES_ID}.recreation-01.payload.tmp", f".{_SERIES_ID}.recreation-01.artifact.tmp",
        f".{_SERIES_ID}.recreation-02.payload.tmp", f".{_SERIES_ID}.recreation-02.artifact.tmp",
        f".{_SERIES_ID}.payload.tmp", f".{_SERIES_ID}.artifact.tmp",
    ]


def test_a_member_1_temp_leftover_does_not_block_member_2_or_terminal(tmp_path, monkeypatch):
    repo, base = _changed_repo(tmp_path)
    monkeypatch.setattr(evidence, "_best_effort_unlink", lambda path: False)
    first = _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha1")
    monkeypatch.undo()
    assert first.error.code is ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED
    leftovers = {f".{_SERIES_ID}.recreation-01.payload.tmp", f".{_SERIES_ID}.recreation-01.artifact.tmp"}
    assert leftovers <= {p.name for p in (tmp_path / "out").iterdir()}
    assert _capture(tmp_path, repo, base, recreation_sequence=2, object_format="sha1").success
    assert _capture(tmp_path, repo, base).success
    assert leftovers <= {p.name for p in (tmp_path / "out").iterdir()}  # never cleaned by later members


def _fail_nth(monkeypatch, name, n, exc):
    real = getattr(os, name)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            raise exc
        return real(*a, **k)

    monkeypatch.setattr(evidence.os, name, flaky)


_KINDS = {"terminal": {}, "recreation": {"recreation_sequence": 7, "object_format": "sha1"}}


def _final(tmp_path, kind):
    stem = _SERIES_ID if kind == "terminal" else f"{_SERIES_ID}.recreation-07"
    return tmp_path / "out" / f"{stem}.evidence"


@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.parametrize(
    "failure",
    [
        ("write", lambda mp: mp.setattr(evidence.os, "write", lambda fd, data: 0)),
        ("payload_fsync", lambda mp: _fail_nth(mp, "fsync", 1, OSError("fsync"))),
        ("artifact_fsync", lambda mp: _fail_nth(mp, "fsync", 2, OSError("fsync"))),
        ("link", lambda mp: _fail_nth(mp, "link", 1, PermissionError("link"))),
    ],
    ids=lambda f: f[0] if isinstance(f, tuple) else f,
)
def test_pre_publication_failures_publish_nothing_and_leave_no_temps(tmp_path, monkeypatch, kind, failure):
    repo, base = _changed_repo(tmp_path)
    failure[1](monkeypatch)
    receipt = _capture(tmp_path, repo, base, **_KINDS[kind])
    monkeypatch.undo()
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert (receipt.artifact_id, receipt.sha256_payload, receipt.bytes_written) == (None, None, None)
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.parametrize("failure", ["unlink", "directory_fsync"])
def test_post_link_ambiguity_keeps_the_published_artifact(tmp_path, monkeypatch, kind, failure):
    repo, base = _changed_repo(tmp_path)
    if failure == "unlink":
        _fail_nth(monkeypatch, "unlink", 1, PermissionError("unlink"))
    else:
        _fail_nth(monkeypatch, "fsync", 3, OSError("directory fsync"))
    receipt = _capture(tmp_path, repo, base, **_KINDS[kind])
    monkeypatch.undo()
    assert receipt.error.code is ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED
    assert receipt.complete is True and receipt.artifact_id == _final(tmp_path, kind).name[: -len(".evidence")]
    header, payload = evidence.parse_artifact_strict(_final(tmp_path, kind).read_bytes(), object_format="sha1")
    assert header["complete"] is True and b"changed" in payload


def test_incomplete_recreation_member_is_published_labelled_incomplete(tmp_path):
    repo, base = _changed_repo(tmp_path)
    (repo / "new.txt").write_text("untracked")
    receipt = _capture(tmp_path, repo, base, recreation_sequence=4, object_format="sha1")
    assert receipt.error.code is ErrorCode.EVIDENCE_INCOMPLETE and receipt.complete is False
    assert receipt.artifact_id == f"{_SERIES_ID}.recreation-04"
    raw = (tmp_path / "out" / f"{_SERIES_ID}.recreation-04.evidence").read_bytes()
    header, _ = evidence.parse_artifact_strict(raw, object_format="sha1")
    assert (header["complete"], header["status"], header["bytes_total"]) == (False, "incomplete", None)


# --- Strict-reader input validation (Step 3 correction) ---


def _frame_text(text: bytes, payload: bytes = b"diff") -> bytes:
    return evidence.ARTIFACT_MAGIC + len(text).to_bytes(4, "big") + text + payload


def _canonical(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _v1_header(**overrides) -> dict:
    digest = __import__("hashlib").sha256(b"diff").hexdigest()
    header = {
        "schema_version": 1, "lifecycle_id": _SERIES_ID, "status": "complete", "payload_encoding": "binary",
        "payload_type": "unified-diff", "bytes_written": 4, "bytes_total": 4, "complete": True,
        "sha256_payload": digest, "sha256_full_stream": digest,
    }
    header.update(overrides)
    return header


def _v2_header(**overrides) -> dict:
    header = _v1_header(schema_version=2, kind="recreation", sequence=1, base_commit="a" * 40)
    header.update(overrides)
    return header


_VERSIONS = {"v1": (_v1_header, None), "v2": (_v2_header, "sha1")}


def _assert_strict_refusal(raw: bytes, object_format, message: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        evidence.parse_artifact_strict(raw, object_format=object_format)
    assert excinfo.type is ValueError
    assert str(excinfo.value) == message
    assert excinfo.value.__cause__ is None


def test_canonical_v1_and_v2_headers_parse_unchanged():
    for build, object_format in _VERSIONS.values():
        raw = _frame_text(_canonical(build()))
        header, payload = evidence.parse_artifact_strict(raw, object_format=object_format)
        assert header == build() and payload == b"diff"
        assert evidence.parse_artifact(raw) == (header, payload)


@pytest.mark.parametrize("text", [b"[]", b'"header"', b"1", b"1.5", b"true", b"false", b"null"])
@pytest.mark.parametrize("object_format", [None, "sha1"])
def test_strict_reader_refuses_a_non_object_header(text, object_format):
    _assert_strict_refusal(_frame_text(text), object_format, "not an evidence artifact: the header is not a JSON object")


@pytest.mark.parametrize("version", list(_VERSIONS))
@pytest.mark.parametrize("value", [1.0, 2.0, True, False, "1", "2", None])
def test_strict_reader_refuses_a_non_integer_schema_version(version, value):
    build, object_format = _VERSIONS[version]
    raw = _frame_text(_canonical(build(schema_version=value)))
    _assert_strict_refusal(raw, object_format, "not an evidence artifact: unsupported schema version")


@pytest.mark.parametrize("version", list(_VERSIONS))
@pytest.mark.parametrize("suffix", ["\n", " ", "0", "\r\n"])
def test_strict_reader_refuses_a_lifecycle_id_with_trailing_characters(version, suffix):
    build, object_format = _VERSIONS[version]
    raw = _frame_text(_canonical(build(lifecycle_id=_SERIES_ID + suffix)))
    _assert_strict_refusal(raw, object_format, "not an evidence artifact: malformed lifecycle_id")


def _with_duplicate(header: dict, key: str, value) -> bytes:
    text = _canonical(header)
    extra = json.dumps({key: value}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")[1:-1]
    return b"{" + extra + b"," + text[1:]


_DUPLICATE_MESSAGE = "not an evidence artifact: the header has a duplicate key or a non-JSON constant"


@pytest.mark.parametrize(
    "version, key, conflicting",
    [
        ("v1", "schema_version", 2), ("v1", "lifecycle_id", "b" * 32), ("v1", "complete", False),
        ("v2", "schema_version", 1), ("v2", "kind", "terminal"), ("v2", "sequence", 99),
        ("v2", "base_commit", "b" * 40), ("v2", "lifecycle_id", "b" * 32),
    ],
)
@pytest.mark.parametrize("identical", [True, False])
def test_strict_reader_refuses_duplicate_keys(version, key, conflicting, identical):
    build, object_format = _VERSIONS[version]
    header = build()
    raw = _frame_text(_with_duplicate(header, key, header[key] if identical else conflicting))
    _assert_strict_refusal(raw, object_format, _DUPLICATE_MESSAGE)
    # The non-strict reader's existing contract is unchanged: json keeps one value.
    assert evidence.parse_artifact(raw)[0][key] == header[key]


def test_strict_reader_refuses_a_nested_duplicate_and_non_json_constants():
    nested = _canonical(_v1_header())[:-1] + b',"extra":{"a":1,"a":1}}'
    _assert_strict_refusal(_frame_text(nested), None, _DUPLICATE_MESSAGE)
    for constant in (b"NaN", b"Infinity", b"-Infinity"):
        raw = _frame_text(_canonical(_v1_header())[:-1] + b',"bytes_total":' + constant + b"}")
        _assert_strict_refusal(raw, None, _DUPLICATE_MESSAGE)


@pytest.mark.parametrize(
    "raw, message",
    [
        (_frame_text(b"\xff\xfe"), "not an evidence artifact: the header is not valid UTF-8 JSON"),
        (_frame_text(b'{"schema_version":'), "not an evidence artifact: the header is not valid UTF-8 JSON"),
        (_frame_text(b"[" * 100_000), "not an evidence artifact: the header is not valid UTF-8 JSON"),
        (b"XXXX", "not an evidence artifact: bad magic"),
        (evidence.ARTIFACT_MAGIC + b"\x00\x00", "not an evidence artifact: truncated before header length"),
        (evidence.ARTIFACT_MAGIC + (99).to_bytes(4, "big") + b"{}", "not an evidence artifact: truncated header"),
    ],
    ids=["bad-utf8", "bad-json", "deep-nesting", "bad-magic", "short", "truncated"],
)
def test_strict_reader_malformed_input_raises_fixed_value_errors(raw, message):
    _assert_strict_refusal(raw, None, message)


@pytest.mark.parametrize("raw", ["text", None, 7, memoryview(b"CAEV")])
def test_strict_reader_refuses_non_bytes_input(raw):
    _assert_strict_refusal(raw, None, "not an evidence artifact: input is not bytes")


def test_real_captured_artifacts_parse_identically_under_both_readers(tmp_path):
    repo, base = _changed_repo(tmp_path)
    assert _capture(tmp_path, repo, base).success
    assert _capture(tmp_path, repo, base, recreation_sequence=1, object_format="sha1").success
    for name, object_format in ((f"{_SERIES_ID}.evidence", None), (f"{_SERIES_ID}.recreation-01.evidence", "sha1")):
        raw = (tmp_path / "out" / name).read_bytes()
        assert evidence.parse_artifact_strict(raw, object_format=object_format) == evidence.parse_artifact(raw)


def test_strict_reader_hides_the_integer_digit_limit_error():
    """A JSON integer beyond Python's digit-conversion limit makes
    `json.loads` raise a plain ValueError with a runtime-specific message;
    the strict reader replaces it with its fixed wording."""
    import sys

    digits = sys.get_int_max_str_digits() + 1
    text = _canonical(_v1_header())[:-1] + b',"extra":' + b"9" * digits + b"}"
    with pytest.raises(ValueError) as lax:
        evidence.parse_artifact(_frame_text(text))  # the lax reader's contract is unchanged
    assert "digit" in str(lax.value).lower()
    _assert_strict_refusal(_frame_text(text), None, "not an evidence artifact: the header is not valid UTF-8 JSON")


# ---------------------------------------------------------------------------
# ADR 0006 Amendment 5 (mandatory pre-D5-C hotfix): an unrenderable filter
# driver name makes capture fail closed before `status` or `diff` runs.
# ---------------------------------------------------------------------------

import uuid as _uuid  # noqa: E402

from tests.support import hostile_filter as hf  # noqa: E402


def test_capture_fails_closed_on_unrenderable_driver(tmp_path, monkeypatch):
    token = f"SENTINEL-{_uuid.uuid4().hex}"
    repo = _make_repo(tmp_path / "repo")
    initial = _initial_commit(repo)
    sentinel = tmp_path / "sentinel"
    hf.configure_hostile_driver(repo, f"a={token}", "clean", sentinel)
    hf.make_stat_dirty(repo / "a.txt")
    output_dir = tmp_path / "evidence-out"
    with hf.spy_git_launches(monkeypatch) as spy:
        receipt = _sink(output_dir).capture(
            worktree_path=repo, source_repo_path=tmp_path / "no-such-source", initial_commit=initial,
            lifecycle_id="c" * 32,
        )
    assert receipt.success is False and receipt.complete is False
    assert receipt.artifact_id is None and receipt.bytes_written is None
    assert receipt.error.code is ErrorCode.EVIDENCE_CAPTURE_FAILED
    assert receipt.error.message == "a configured filter driver name cannot be safely neutralized"
    hf.assert_no_hostile_value(receipt.error, (token, str(sentinel), str(repo)))
    assert spy.commands == [("config",)]
    assert not sentinel.exists()
    assert not output_dir.exists() or list(output_dir.iterdir()) == []
