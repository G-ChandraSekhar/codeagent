"""Tests for the trusted checkpoint-ref primitive (ADR 0003 Amendment 1).

Real throwaway Git repositories are used for anything that depends on
real Git semantics — compare-and-swap, symbolic refs, object formats —
because that behavior is exactly what is being relied on. Only the
failure paths that cannot be provoked with a real repository (a hung or
unlaunchable git, a malformed object-format string) monkeypatch the
single `_run_git` seam. No network and no Docker.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeagent.checkpoint_ref import (
    CheckpointRef,
    CheckpointRefError,
    CheckpointRefFailure,
    ObjectFormat,
    RefObservation,
)
from codeagent import checkpoint_ref as checkpoint_ref_module

LIFECYCLE_ID = "0123456789abcdef0123456789abcdef"
OTHER_LIFECYCLE_ID = "fedcba9876543210fedcba9876543210"

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent Test",
    "-c",
    "user.email=codeagent-test@example.invalid",
]


def _clean_env() -> dict[str, str]:
    """The test oracle must not itself be redirected by the hostile
    `GIT_*` variables some tests set. Computed independently of the
    module under test, so the oracle never depends on the behavior it
    is checking."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=check,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _make_repo(path: Path, object_format: str = "sha1") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", f"--object-format={object_format}", str(path)],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    (path / "file.txt").write_text("one\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-qm", "first")
    return path


def _commit(repo: Path, text: str) -> str:
    (repo / "file.txt").write_text(text)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", f"commit {text.strip()}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _ref_value(repo: Path, ref: str) -> str:
    return _git(repo, "for-each-ref", "--format=%(objectname)", ref).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "source")


@pytest.fixture()
def ref(repo: Path) -> CheckpointRef:
    return CheckpointRef(repo, LIFECYCLE_ID)


# --------------------------------------------------------------------
# Ref naming, identity validation, namespace containment
# --------------------------------------------------------------------


def test_ref_name_is_the_owned_namespace(ref: CheckpointRef) -> None:
    assert ref.ref_name == f"refs/codeagent/runs/{LIFECYCLE_ID}/checkpoint"
    assert ref.lifecycle_id == LIFECYCLE_ID


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "short",
        "0123456789ABCDEF0123456789ABCDEF",  # uppercase
        "0123456789abcdef0123456789abcde",  # 31 chars
        "0123456789abcdef0123456789abcdef0",  # 33 chars
        "0123456789abcdef0123456789abcdeg",  # non-hex
        "../../../../etc/passwd",
        "../heads/main",
        "aaaa/../../heads/main",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/extra",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa..",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ",
        "refs/heads/main",
    ],
)
def test_malformed_lifecycle_id_is_rejected(repo: Path, bad_id: str) -> None:
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        CheckpointRef(repo, bad_id)


def test_rejected_lifecycle_id_is_not_echoed(repo: Path) -> None:
    """A rejected value is caller-supplied and must never reach the
    message."""
    with pytest.raises(ValueError) as excinfo:
        CheckpointRef(repo, "../../refs/heads/main")
    assert "refs/heads/main" not in str(excinfo.value)
    assert ".." not in str(excinfo.value)


def test_non_string_lifecycle_id_is_rejected(repo: Path) -> None:
    with pytest.raises(ValueError):
        CheckpointRef(repo, 12345)  # type: ignore[arg-type]


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        CheckpointRef(tmp_path / "nope", LIFECYCLE_ID)


def test_non_repository_directory_fails_closed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(CheckpointRefError) as excinfo:
        CheckpointRef(plain, LIFECYCLE_ID)
    assert excinfo.value.reason is CheckpointRefFailure.REPOSITORY_UNAVAILABLE


def test_linked_worktree_is_refused(repo: Path, tmp_path: Path) -> None:
    """ADR 0003 Amendment 1: never operate through a disposable
    worktree's `.git` file."""
    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()
    with pytest.raises(ValueError, match="not a linked worktree"):
        CheckpointRef(worktree, LIFECYCLE_ID)


# --------------------------------------------------------------------
# Object format
# --------------------------------------------------------------------


def test_sha1_repository_object_format(ref: CheckpointRef) -> None:
    assert ref.object_format is ObjectFormat.SHA1
    assert ref.object_format.hex_length == 40
    assert ref.object_format.zero_oid == "0" * 40


def test_sha256_repository_object_format(tmp_path: Path) -> None:
    probe = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(tmp_path / "probe")],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    if probe.returncode != 0:
        pytest.skip(
            "installed git does not support --object-format=sha256: "
            f"git init exited {probe.returncode}"
        )
    repo = _make_repo(tmp_path / "s256", object_format="sha256")
    ref = CheckpointRef(repo, LIFECYCLE_ID)
    assert ref.object_format is ObjectFormat.SHA256
    assert ref.object_format.hex_length == 64
    assert ref.object_format.zero_oid == "0" * 64

    head = _head(repo)
    assert len(head) == 64
    ref.create(head)
    assert ref.observe() == RefObservation(present=True, oid=head)
    second = _commit(repo, "two\n")
    ref.advance(expected_old_oid=head, new_oid=second)
    assert ref.observe().oid == second
    ref.delete(expected_oid=second)
    assert ref.observe() == RefObservation(present=False, oid=None)


def test_unknown_object_format_fails_closed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = checkpoint_ref_module._run_git

    def fake_run(repo_path, *args):
        if args[:2] == ("rev-parse", "--show-object-format"):
            return subprocess.CompletedProcess(args, 0, stdout="sha3-512\n", stderr="")
        return real_run(repo_path, *args)

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        CheckpointRef(repo, LIFECYCLE_ID)
    assert excinfo.value.reason is CheckpointRefFailure.OBJECT_FORMAT_UNSUPPORTED


def test_malformed_object_format_output_fails_closed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = checkpoint_ref_module._run_git

    def fake_run(repo_path, *args):
        if args[:2] == ("rev-parse", "--show-object-format"):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(repo_path, *args)

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        CheckpointRef(repo, LIFECYCLE_ID)
    assert excinfo.value.reason is CheckpointRefFailure.OBJECT_FORMAT_UNSUPPORTED


def test_object_format_command_failure_fails_closed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = checkpoint_ref_module._run_git

    def fake_run(repo_path, *args):
        if args[:2] == ("rev-parse", "--show-object-format"):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")
        return real_run(repo_path, *args)

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        CheckpointRef(repo, LIFECYCLE_ID)
    assert excinfo.value.reason is CheckpointRefFailure.OBJECT_FORMAT_UNAVAILABLE


@pytest.mark.parametrize(
    "bad_oid",
    [
        "",
        "abc",
        "0" * 39,
        "0" * 41,
        "0" * 64,  # a sha256-length id in a sha1 repository
        "g" * 40,
        "0123456789ABCDEF0123456789abcdef01234567",  # uppercase
        " " + "0" * 39,
    ],
)
def test_malformed_object_ids_are_rejected(ref: CheckpointRef, bad_oid: str) -> None:
    with pytest.raises(ValueError, match="40 lowercase hexadecimal"):
        ref.create(bad_oid)


# --------------------------------------------------------------------
# Absence, create, observe, advance, delete
# --------------------------------------------------------------------


def test_observe_reports_absence_not_a_zero_oid(ref: CheckpointRef) -> None:
    observation = ref.observe()
    assert observation == RefObservation(present=False, oid=None)
    assert observation.oid is None


def test_create_observe_advance_delete_round_trip(repo: Path, ref: CheckpointRef) -> None:
    first = _head(repo)
    ref.create(first)
    assert ref.observe() == RefObservation(present=True, oid=first)

    second = _commit(repo, "two\n")
    ref.advance(expected_old_oid=first, new_oid=second)
    assert ref.observe() == RefObservation(present=True, oid=second)

    ref.delete(expected_oid=second)
    assert ref.observe() == RefObservation(present=False, oid=None)
    assert _ref_value(repo, ref.ref_name) == ""


def test_create_refuses_when_ref_already_exists(repo: Path, ref: CheckpointRef) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(second)
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE
    assert _ref_value(repo, ref.ref_name) == first


def test_advance_rejects_equal_old_and_new(repo: Path, ref: CheckpointRef) -> None:
    first = _head(repo)
    ref.create(first)
    with pytest.raises(ValueError, match="must differ"):
        ref.advance(expected_old_oid=first, new_oid=first)


# --------------------------------------------------------------------
# Stale expectations and competing updates never overwrite
# --------------------------------------------------------------------


def test_advance_with_stale_expected_old_does_not_overwrite(
    repo: Path, ref: CheckpointRef
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    ref.advance(expected_old_oid=first, new_oid=second)

    third = _commit(repo, "three\n")
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=third)  # stale
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE
    assert excinfo.value.observed_oid == second
    assert excinfo.value.expected_oid == first
    assert _ref_value(repo, ref.ref_name) == second


def test_competing_update_is_not_overwritten(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competitor moved the ref after our observation. Git's
    compare-and-swap — not the pre-check — must be what protects it, so
    the pre-check is bypassed here to isolate that guarantee."""
    first = _head(repo)
    ref.create(first)
    competitor = _commit(repo, "competitor\n")
    _git(repo, "update-ref", "--no-deref", ref.ref_name, competitor, first)
    ours = _commit(repo, "ours\n")

    monkeypatch.setattr(CheckpointRef, "_require_state", lambda self, expected, when: None)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=ours)
    # The competitor's value survived, and the error reports what was
    # actually observed rather than assuming anything.
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE
    assert excinfo.value.observed_oid == competitor
    assert _ref_value(repo, ref.ref_name) == competitor


def test_delete_with_wrong_expected_value_does_not_delete(
    repo: Path, ref: CheckpointRef
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    ref.advance(expected_old_oid=first, new_oid=second)

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.delete(expected_oid=first)
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE
    assert _ref_value(repo, ref.ref_name) == second


def test_delete_when_absent_is_refused(ref: CheckpointRef, repo: Path) -> None:
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.delete(expected_oid=_head(repo))
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE


def test_advance_when_absent_is_refused(repo: Path, ref: CheckpointRef) -> None:
    first = _head(repo)
    second = _commit(repo, "two\n")
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=second)
    assert excinfo.value.reason is CheckpointRefFailure.UNEXPECTED_VALUE
    assert _ref_value(repo, ref.ref_name) == ""


# --------------------------------------------------------------------
# Symbolic refs
# --------------------------------------------------------------------


def test_observe_refuses_a_symbolic_ref(repo: Path, ref: CheckpointRef) -> None:
    # An explicitly created ref, never the repository's default branch
    # name (which is an environment-dependent assumption — e.g. "main"
    # vs. "master" — not something these tests should rely on).
    _git(repo, "update-ref", "refs/heads/pin", _head(repo))
    _git(repo, "symbolic-ref", ref.ref_name, "refs/heads/pin")
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF


def test_mutations_refuse_a_symbolic_ref_and_leave_it_symbolic(
    repo: Path, ref: CheckpointRef
) -> None:
    """Load-bearing: `git update-ref --no-deref` against a symbolic ref
    succeeds and silently rewrites it into a regular ref, so
    compare-and-swap alone would not protect the owned name."""
    head = _head(repo)
    _git(repo, "update-ref", "refs/heads/pin", head)
    _git(repo, "symbolic-ref", ref.ref_name, "refs/heads/pin")
    second = _commit(repo, "two\n")

    for call in (
        lambda: ref.create(second),
        lambda: ref.advance(expected_old_oid=head, new_oid=second),
        lambda: ref.delete(expected_oid=head),
    ):
        with pytest.raises(CheckpointRefError) as excinfo:
            call()
        assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
        assert (
            _git(repo, "symbolic-ref", ref.ref_name).stdout.strip() == "refs/heads/pin"
        ), "the owned ref must still be symbolic — no mutation may have happened"


# --------------------------------------------------------------------
# Observation failures are never treated as absence
# --------------------------------------------------------------------


def test_observation_failure_is_not_absence(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")
        raise AssertionError("unexpected git call")

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.OBSERVATION_FAILED


def test_ambiguous_observation_is_refused(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            line = f"{'a' * 40}\t\t{ref.ref_name}"
            return subprocess.CompletedProcess(args, 0, stdout=f"{line}\n{line}\n", stderr="")
        raise AssertionError("unexpected git call")

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.AMBIGUOUS_OBSERVATION


def test_observation_of_a_different_ref_is_refused(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            other = f"refs/codeagent/runs/{OTHER_LIFECYCLE_ID}/checkpoint"
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{'a' * 40}\t\t{other}\n", stderr=""
            )
        raise AssertionError("unexpected git call")

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.AMBIGUOUS_OBSERVATION


def test_unreadable_observation_record_is_refused(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 0, stdout="not-tab-separated\n", stderr="")
        raise AssertionError("unexpected git call")

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.OBSERVATION_FAILED


def test_malformed_object_id_from_git_is_refused(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            return subprocess.CompletedProcess(
                args, 0, stdout=f"deadbeef\t\t{ref.ref_name}\n", stderr=""
            )
        raise AssertionError("unexpected git call")

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.OBSERVATION_FAILED


def test_git_timeout_fails_closed(ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(checkpoint_ref_module.subprocess, "run", fake_subprocess_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.GIT_COMMAND_TIMEOUT


def test_git_executable_unavailable_fails_closed(
    ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_subprocess_run(*args, **kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(checkpoint_ref_module.subprocess, "run", fake_subprocess_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE


def test_reported_success_without_effect_is_reported_as_not_taken_effect(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git reports success but the ref did not change. The outcome must
    come from observing the ref, never from the report."""
    head = _head(repo)
    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "commit", lambda self: None)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(head)
    assert excinfo.value.reason is CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED
    assert "still absent" in str(excinfo.value)
    assert _ref_value(repo, ref.ref_name) == ""


def test_reported_failure_after_a_real_effect_is_reported_as_success(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation actually took effect but the command reported a
    failure. ADR 0004: observing the intended value means it succeeded."""
    head = _head(repo)
    real_commit = checkpoint_ref_module._RefTransaction.commit

    def commit_then_report_failure(self) -> None:
        real_commit(self)
        raise CheckpointRefError(
            CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED, "simulated ambiguous result"
        )

    monkeypatch.setattr(
        checkpoint_ref_module._RefTransaction, "commit", commit_then_report_failure
    )
    ref.create(head)  # must not raise
    assert _ref_value(repo, ref.ref_name) == head


def test_outcome_is_unknown_when_the_ref_cannot_be_observed_afterwards(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the post-mutation observation itself fails, the outcome is
    explicitly unknown — never reported as unchanged."""
    head = _head(repo)
    real_run = checkpoint_ref_module._run_git
    observations = {"count": 0}

    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            observations["count"] += 1
            # Allow the pre-check and the locked check; fail the final
            # confirmation.
            if observations["count"] >= 3:
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")
        return real_run(repo_path, *args)

    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(head)
    assert excinfo.value.reason is CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN
    assert "could not be determined" in str(excinfo.value)
    # The mutation did in fact happen; the point is that the primitive
    # refused to claim either way.
    assert _ref_value(repo, ref.ref_name) == head


# --------------------------------------------------------------------
# Sanitized errors
# --------------------------------------------------------------------


def test_errors_do_not_leak_git_stderr_or_host_paths(tmp_path: Path) -> None:
    plain = tmp_path / "secret-dir-name"
    plain.mkdir()
    with pytest.raises(CheckpointRefError) as excinfo:
        CheckpointRef(plain, LIFECYCLE_ID)
    message = str(excinfo.value)
    assert "secret-dir-name" not in message
    assert str(tmp_path) not in message
    assert "not a git repository" not in message
    assert "fatal:" not in message


def test_failed_real_transaction_error_does_not_leak_stderr(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine `prepare` failure: git's own stderr names the absolute
    repository path, which must not reach the raised error."""
    first = _head(repo)
    ref.create(first)
    competitor = _commit(repo, "competitor\n")
    _git(repo, "update-ref", "--no-deref", ref.ref_name, competitor, first)
    ours = _commit(repo, "ours\n")

    monkeypatch.setattr(CheckpointRef, "_require_state", lambda self, expected, when: None)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=ours)  # stale: prepare fails for real
    message = str(excinfo.value)
    assert str(repo) not in message
    assert "fatal:" not in message
    assert "cannot lock ref" not in message


# --------------------------------------------------------------------
# The rest of the repository is left alone
# --------------------------------------------------------------------


def test_repository_state_outside_the_owned_ref_is_unchanged(
    repo: Path, ref: CheckpointRef
) -> None:
    _git(repo, "update-ref", "refs/heads/keep-me", _head(repo))
    _git(repo, "tag", "v1")

    def snapshot() -> dict[str, object]:
        refs = _git(
            repo, "for-each-ref", "--format=%(refname) %(objectname)"
        ).stdout.splitlines()
        return {
            "head": _git(repo, "rev-parse", "HEAD").stdout.strip(),
            "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "status": _git(repo, "status", "--porcelain=v1").stdout,
            "index": _git(repo, "ls-files", "--stage").stdout,
            "worktree": (repo / "file.txt").read_text(),
            "other_refs": sorted(r for r in refs if "refs/codeagent/" not in r),
        }

    before = snapshot()
    first = _head(repo)
    ref.create(first)
    during = snapshot()
    second = _commit(repo, "two\n")  # ordinary commit, unrelated to the ref

    ref.advance(expected_old_oid=first, new_oid=second)
    ref.delete(expected_oid=second)
    after = snapshot()

    # The owned ref never disturbed HEAD, the branch, the index, the
    # working tree, or any unrelated ref.
    assert during["branch"] == before["branch"]
    assert during["head"] == before["head"]
    assert during["status"] == before["status"]
    assert during["index"] == before["index"]
    assert during["worktree"] == before["worktree"]
    assert during["other_refs"] == before["other_refs"]

    # After teardown the owned ref is gone. Unrelated refs still exist
    # with the same names; `refs/heads/main` legitimately moved because
    # the test itself made an ordinary commit, so names — not values —
    # are what must be unchanged here.
    assert _ref_value(repo, ref.ref_name) == ""
    all_refs_after = _git(repo, "for-each-ref", "--format=%(refname)").stdout.split()
    assert not [r for r in all_refs_after if r.startswith("refs/codeagent/")]

    def names(snapshot_refs: object) -> list[str]:
        return sorted(line.split(" ", 1)[0] for line in snapshot_refs)  # type: ignore[union-attr]

    assert names(after["other_refs"]) == names(before["other_refs"])
    assert "refs/heads/keep-me" in names(after["other_refs"])
    assert "refs/tags/v1" in names(after["other_refs"])
    # The unrelated branch and tag did not move at all.
    assert [r for r in after["other_refs"] if "keep-me" in r] == [
        r for r in before["other_refs"] if "keep-me" in r
    ]
    assert [r for r in after["other_refs"] if "tags/v1" in r] == [
        r for r in before["other_refs"] if "tags/v1" in r
    ]


def test_another_lifecycles_ref_is_untouched(repo: Path, ref: CheckpointRef) -> None:
    other = CheckpointRef(repo, OTHER_LIFECYCLE_ID)
    head = _head(repo)
    other.create(head)

    second = _commit(repo, "two\n")
    ref.create(second)
    ref.delete(expected_oid=second)

    assert other.observe() == RefObservation(present=True, oid=head)
    assert _ref_value(repo, other.ref_name) == head


# --------------------------------------------------------------------
# Git environment cannot redirect the operation (regression)
#
# Reproduced against the previous implementation: with GIT_DIR pointing
# at another repository, construction succeeded and create() wrote the
# owned ref into that other repository.
# --------------------------------------------------------------------


def test_git_environment_strips_every_git_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.setenv(name, "/somewhere/hostile")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/someone")

    env = checkpoint_ref_module.git_environment()
    assert not [key for key in env if key.startswith("GIT_")]
    # Everything needed to launch git normally is preserved.
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/someone"


@pytest.fixture()
def decoy(tmp_path: Path) -> Path:
    """A second real repository that hostile Git variables point at."""
    return _make_repo(tmp_path / "decoy")


def _assert_only_repo_was_touched(repo: Path, decoy: Path, ref: CheckpointRef) -> None:
    head = _head(repo)
    ref.create(head)
    assert ref.observe() == RefObservation(present=True, oid=head)
    assert _ref_value(repo, ref.ref_name) == head
    # Nothing was written into the decoy, under any namespace.
    decoy_refs = _git(decoy, "for-each-ref", "--format=%(refname)").stdout
    assert "codeagent" not in decoy_refs
    ref.delete(expected_oid=head)
    assert _ref_value(repo, ref.ref_name) == ""


def test_git_dir_and_work_tree_cannot_redirect(
    repo: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    _assert_only_repo_was_touched(repo, decoy, CheckpointRef(repo, LIFECYCLE_ID))


def test_git_common_dir_cannot_redirect(
    repo: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_COMMON_DIR", str(decoy / ".git"))
    _assert_only_repo_was_touched(repo, decoy, CheckpointRef(repo, LIFECYCLE_ID))


def test_git_namespace_cannot_redirect_the_owned_ref(
    repo: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_NAMESPACE", "hostile")
    ref = CheckpointRef(repo, LIFECYCLE_ID)
    _assert_only_repo_was_touched(repo, decoy, ref)
    # The ref never landed under refs/namespaces/.
    assert "refs/namespaces" not in _git(repo, "for-each-ref", "--format=%(refname)").stdout


def test_object_index_and_replace_variables_cannot_redirect(
    repo: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(decoy / ".git" / "objects"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/hostile-replace")
    _assert_only_repo_was_touched(repo, decoy, CheckpointRef(repo, LIFECYCLE_ID))


def test_command_line_config_injection_cannot_redirect(
    repo: Path, decoy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GIT_CONFIG_* is how configuration is injected into a git process
    # without a command line; point the ref storage and safe-directory
    # rules somewhere hostile and prove it has no effect.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "/nowhere")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bare=true'")
    _assert_only_repo_was_touched(repo, decoy, CheckpointRef(repo, LIFECYCLE_ID))


def test_hostile_environment_is_not_leaked_in_errors(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", "/hostile/SECRET_PATH/.git")
    ref = CheckpointRef(repo, LIFECYCLE_ID)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.delete(expected_oid=_head(repo))  # absent -> refused
    assert "SECRET_PATH" not in str(excinfo.value)
    assert "/hostile" not in str(excinfo.value)


# --------------------------------------------------------------------
# Symbolic-ref substitution race (regression)
# --------------------------------------------------------------------


def test_plain_update_ref_would_convert_a_substituted_symref(repo: Path) -> None:
    """Characterization of the hazard this primitive must survive:
    plain `git update-ref --no-deref` against a symbolic ref whose
    target resolves to the expected old value *succeeds* and silently
    rewrites it into a direct ref. This is why compare-and-swap alone
    is not sufficient."""
    first = _head(repo)
    second = _commit(repo, "two\n")
    victim = f"refs/codeagent/runs/{OTHER_LIFECYCLE_ID}/checkpoint"
    _git(repo, "update-ref", "refs/heads/pin", first)
    _git(repo, "symbolic-ref", victim, "refs/heads/pin")

    _git(repo, "update-ref", "--no-deref", victim, second, first)

    assert _git(repo, "symbolic-ref", "--quiet", victim, check=False).returncode != 0
    assert _ref_value(repo, victim) == second


def test_symbolic_substitution_after_the_precheck_is_refused(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the pre-check alone cannot cover: the ref is a direct
    ref at the expected value when checked, and is replaced by a
    symbolic ref resolving to that same value before the mutation. The
    transaction must refuse it and leave it symbolic."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    original = CheckpointRef._require_state
    substituted = {"done": False}

    def racing_precheck(self, expected, *, when):
        original(self, expected, when=when)
        if not substituted["done"]:
            substituted["done"] = True
            # Another process substitutes a symref resolving to `first`.
            _git(repo, "update-ref", "refs/heads/pin", first)
            _git(repo, "update-ref", "--no-deref", "-d", self.ref_name, first)
            _git(repo, "symbolic-ref", self.ref_name, "refs/heads/pin")

    monkeypatch.setattr(CheckpointRef, "_require_state", racing_precheck)

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=second)

    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
    assert _git(repo, "symbolic-ref", ref.ref_name).stdout.strip() == "refs/heads/pin"
    assert _git(repo, "rev-parse", ref.ref_name).stdout.strip() == first


def test_symbolic_substitution_race_on_delete_is_refused(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _head(repo)
    ref.create(first)
    original = CheckpointRef._require_state
    substituted = {"done": False}

    def racing_precheck(self, expected, *, when):
        original(self, expected, when=when)
        if not substituted["done"]:
            substituted["done"] = True
            _git(repo, "update-ref", "refs/heads/pin", first)
            _git(repo, "update-ref", "--no-deref", "-d", self.ref_name, first)
            _git(repo, "symbolic-ref", self.ref_name, "refs/heads/pin")

    monkeypatch.setattr(CheckpointRef, "_require_state", racing_precheck)

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.delete(expected_oid=first)
    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
    assert _git(repo, "symbolic-ref", ref.ref_name).stdout.strip() == "refs/heads/pin"


# --------------------------------------------------------------------
# Ref storage backends
# --------------------------------------------------------------------


def _ref_format_supported(tmp_path: Path, ref_format: str) -> bool:
    probe = subprocess.run(
        ["git", "init", "-q", f"--ref-format={ref_format}", str(tmp_path / f"probe-{ref_format}")],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return probe.returncode == 0


def test_round_trip_and_symbolic_race_on_the_reftable_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantees must hold on the `reftable` ref backend, not
    only on `files`."""
    if not _ref_format_supported(tmp_path, "reftable"):
        pytest.skip("installed git does not support --ref-format=reftable")

    repo = tmp_path / "reftable-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--ref-format=reftable", str(repo)],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "first")

    ref = CheckpointRef(repo, LIFECYCLE_ID)
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    ref.advance(expected_old_oid=first, new_oid=second)
    assert ref.observe() == RefObservation(present=True, oid=second)

    # And the substitution race is refused here too.
    original = CheckpointRef._require_state
    substituted = {"done": False}

    def racing_precheck(self, expected, *, when):
        original(self, expected, when=when)
        if not substituted["done"]:
            substituted["done"] = True
            _git(repo, "update-ref", "refs/heads/pin", second)
            _git(repo, "update-ref", "--no-deref", "-d", self.ref_name, second)
            _git(repo, "symbolic-ref", self.ref_name, "refs/heads/pin")

    monkeypatch.setattr(CheckpointRef, "_require_state", racing_precheck)
    third = _commit(repo, "three\n")
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=second, new_oid=third)
    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
    assert _git(repo, "symbolic-ref", ref.ref_name).stdout.strip() == "refs/heads/pin"


# --------------------------------------------------------------------
# Repository-configured hooks must never run (regression)
#
# Reproduced against the previous implementation: a repository with
# core.hooksPath set executed its own `reference-transaction` hook on
# the host during create(). Clearing GIT_* does not disable hooks.
# --------------------------------------------------------------------


def _install_hostile_hook(repo: Path, tmp_path: Path, hook_name: str) -> Path:
    """Point the repository at a hooks directory whose hook writes a
    marker file, so any execution is provable."""
    hooks_dir = tmp_path / "hostile-hooks"
    hooks_dir.mkdir(exist_ok=True)
    marker = tmp_path / f"{hook_name}-marker.txt"
    hook = hooks_dir / hook_name
    hook.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 0\n')
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks_dir))
    return marker


def test_repository_configured_reference_transaction_hook_never_runs(
    repo: Path, ref: CheckpointRef, tmp_path: Path
) -> None:
    # All setup commits happen first: an ordinary `git commit` updates a
    # ref and so legitimately fires this hook, which would mask the
    # result. Only CheckpointRef operations run once the hook is live.
    first = _head(repo)
    second = _commit(repo, "two\n")
    marker = _install_hostile_hook(repo, tmp_path, "reference-transaction")

    ref.create(first)
    ref.advance(expected_old_oid=first, new_oid=second)
    ref.delete(expected_oid=second)

    assert not marker.exists(), "a repository-configured hook executed on the host"
    # Normal ref behavior is unaffected by the hooks policy.
    assert ref.observe() == RefObservation(present=False, oid=None)

    # Positive control: the hook really was armed the whole time, so the
    # assertion above is about suppression, not a dead hook.
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "--no-deref", ref.ref_name, first],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert marker.exists(), "the hostile hook was never armed; the test proves nothing"


def test_hook_suppression_does_not_break_observation_or_object_format(
    repo: Path, tmp_path: Path
) -> None:
    head = _head(repo)
    marker = _install_hostile_hook(repo, tmp_path, "reference-transaction")
    ref = CheckpointRef(repo, LIFECYCLE_ID)  # construction runs rev-parse
    ref.create(head)
    assert ref.object_format is ObjectFormat.SHA1
    assert ref.observe() == RefObservation(present=True, oid=head)
    assert not marker.exists()


def test_hostile_hook_is_suppressed_for_every_operation_including_failures(
    repo: Path, ref: CheckpointRef, tmp_path: Path
) -> None:
    """The policy is command-line configuration, which outranks every
    configuration file, and it applies to refused operations too."""
    first = _head(repo)
    marker = _install_hostile_hook(repo, tmp_path, "reference-transaction")

    ref.create(first)
    with pytest.raises(CheckpointRefError):
        ref.create(first)  # refused: already exists
    with pytest.raises(CheckpointRefError):
        ref.delete(expected_oid="0" * 40)  # refused: wrong expected value
    ref.delete(expected_oid=first)

    assert not marker.exists()


# --------------------------------------------------------------------
# The precise transaction failure cause is preserved
# --------------------------------------------------------------------


def _fail_transaction(monkeypatch: pytest.MonkeyPatch, method: str, error: Exception) -> None:
    def raising(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, method, raising)


def test_transaction_launch_failure_preserves_its_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_transaction(
        monkeypatch,
        "__enter__",
        CheckpointRefError(
            CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE,
            "the git executable could not be launched",
        ),
    )
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(_head(repo))
    assert excinfo.value.reason is CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE
    assert _ref_value(repo, ref.ref_name) == ""


def test_acknowledgement_timeout_preserves_its_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_transaction(
        monkeypatch,
        "begin",
        CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
            "the git ref transaction did not respond within its time limit",
        ),
    )
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(_head(repo))
    assert excinfo.value.reason is CheckpointRefFailure.GIT_COMMAND_TIMEOUT


def test_protocol_failure_preserves_its_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed acknowledgement is a protocol failure, not a
    compare-and-swap rejection."""
    real_expect = checkpoint_ref_module._RefTransaction._expect

    def bad_ack(self, acknowledgement):
        if acknowledgement == "prepare: ok":
            raise CheckpointRefError(
                CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                "git did not acknowledge the ref transaction stage",
            )
        return real_expect(self, acknowledgement)

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "_expect", bad_ack)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(_head(repo))
    assert excinfo.value.reason is CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED
    assert "acknowledge" in str(excinfo.value)


def test_commit_failure_preserves_its_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_transaction(
        monkeypatch,
        "commit",
        CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
            "the git ref transaction did not finish within its time limit",
        ),
    )
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(_head(repo))
    # Not flattened into COMPARE_AND_SWAP_REJECTED.
    assert excinfo.value.reason is CheckpointRefFailure.GIT_COMMAND_TIMEOUT
    assert _ref_value(repo, ref.ref_name) == ""


def test_unknown_outcome_is_chained_from_the_original_failure(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_failure = CheckpointRefError(
        CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE, "the git executable could not be launched"
    )
    _fail_transaction(monkeypatch, "begin", launch_failure)
    real_run = checkpoint_ref_module._run_git

    def fake_run(repo_path, *args):
        if args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")
        return real_run(repo_path, *args)

    # Pre-check observation must succeed; only the classification one fails.
    monkeypatch.setattr(CheckpointRef, "_require_state", lambda self, expected, when: None)
    monkeypatch.setattr(checkpoint_ref_module, "_run_git", fake_run)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.create(_head(repo))
    assert excinfo.value.reason is CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN
    assert excinfo.value.__cause__ is launch_failure


# --------------------------------------------------------------------
# Prepared transactions hold the ref lock; cleanup always releases it
# --------------------------------------------------------------------


def _competing_writes_fail(repo: Path, ref_name: str, oid: str) -> tuple[int, int]:
    symbolic = _git(repo, "symbolic-ref", ref_name, "refs/heads/main", check=False)
    update = _git(repo, "update-ref", "--no-deref", ref_name, oid, check=False)
    return symbolic.returncode, update.returncode


def _assert_lock_released(repo: Path, ref: CheckpointRef) -> None:
    """Portable proof the lock is gone: another writer succeeds. Works
    on both ref backends, unlike checking for a `.lock` file."""
    probe = _git(repo, "update-ref", "refs/heads/lock-probe", _head(repo), check=False)
    assert probe.returncode == 0
    _git(repo, "update-ref", "-d", "refs/heads/lock-probe")


def test_prepared_transaction_blocks_competing_writers_then_releases_on_abort(
    repo: Path, ref: CheckpointRef
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    with checkpoint_ref_module._RefTransaction(repo) as transaction:
        transaction.begin(f"update {ref.ref_name} {second} {first}")
        symbolic_rc, update_rc = _competing_writes_fail(repo, ref.ref_name, second)
        assert symbolic_rc != 0, "a competing symbolic-ref succeeded while prepared"
        assert update_rc != 0, "a competing update-ref succeeded while prepared"
        transaction.abort()

    assert _ref_value(repo, ref.ref_name) == first
    assert _git(repo, "update-ref", "--no-deref", ref.ref_name, second, first).returncode == 0
    _assert_lock_released(repo, ref)


def test_prepared_transaction_releases_the_lock_after_commit(
    repo: Path, ref: CheckpointRef
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    with checkpoint_ref_module._RefTransaction(repo) as transaction:
        transaction.begin(f"update {ref.ref_name} {second} {first}")
        transaction.commit()

    assert _ref_value(repo, ref.ref_name) == second
    # The ref is writable again by anyone.
    assert _git(repo, "update-ref", "--no-deref", ref.ref_name, first, second).returncode == 0
    _assert_lock_released(repo, ref)


def test_prepared_lock_behavior_on_the_reftable_backend(
    tmp_path: Path
) -> None:
    if not _ref_format_supported(tmp_path, "reftable"):
        pytest.skip("installed git does not support --ref-format=reftable")
    repo = tmp_path / "reftable-lock"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--ref-format=reftable", str(repo)],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "first")
    ref = CheckpointRef(repo, LIFECYCLE_ID)
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    with checkpoint_ref_module._RefTransaction(repo) as transaction:
        transaction.begin(f"update {ref.ref_name} {second} {first}")
        symbolic_rc, update_rc = _competing_writes_fail(repo, ref.ref_name, second)
        assert symbolic_rc != 0
        assert update_rc != 0
        transaction.abort()

    assert _ref_value(repo, ref.ref_name) == first
    _assert_lock_released(repo, ref)


def test_exception_after_prepare_releases_the_lock(repo: Path, ref: CheckpointRef) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with checkpoint_ref_module._RefTransaction(repo) as transaction:
            transaction.begin(f"update {ref.ref_name} {second} {first}")
            raise Boom("failure while the ref is locked")

    assert _ref_value(repo, ref.ref_name) == first
    assert _git(repo, "update-ref", "--no-deref", ref.ref_name, second, first).returncode == 0
    _assert_lock_released(repo, ref)


def test_abort_failure_still_kills_and_reaps_the_child(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must not depend on a clean abort."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    captured: dict[str, subprocess.Popen] = {}

    def broken_abort(self):
        captured["process"] = self._process
        raise CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT, "simulated abort failure"
        )

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "abort", broken_abort)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with checkpoint_ref_module._RefTransaction(repo) as transaction:
            transaction.begin(f"update {ref.ref_name} {second} {first}")
            raise Boom("abort will fail during cleanup")

    process = captured["process"]
    assert process.poll() is not None, "the git child was not reaped"
    assert _ref_value(repo, ref.ref_name) == first
    _assert_lock_released(repo, ref)


def test_timeout_leaves_no_git_process_or_ref_lock(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")
    captured: dict[str, subprocess.Popen] = {}
    real_begin = checkpoint_ref_module._RefTransaction.begin

    def begin_then_timeout(self, update_line):
        real_begin(self, update_line)  # really prepares and locks
        captured["process"] = self._process
        raise CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
            "the git ref transaction did not respond within its time limit",
        )

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "begin", begin_then_timeout)

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=second)
    assert excinfo.value.reason is CheckpointRefFailure.GIT_COMMAND_TIMEOUT

    process = captured["process"]
    assert process.poll() is not None, "a git process was left running"
    assert _ref_value(repo, ref.ref_name) == first
    _assert_lock_released(repo, ref)
    # And the ref is genuinely writable again.
    assert _git(repo, "update-ref", "--no-deref", ref.ref_name, second, first).returncode == 0


def test_transaction_errors_never_carry_raw_git_stderr(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's stderr is discarded outright, so it cannot reach an error
    even by accident."""
    first = _head(repo)
    ref.create(first)
    competitor = _commit(repo, "competitor\n")
    _git(repo, "update-ref", "--no-deref", ref.ref_name, competitor, first)
    ours = _commit(repo, "ours\n")

    monkeypatch.setattr(CheckpointRef, "_require_state", lambda self, expected, when: None)
    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=ours)
    message = str(excinfo.value)
    for leak in (str(repo), "fatal:", "cannot lock ref", "error:"):
        assert leak not in message


# --------------------------------------------------------------------
# Transaction-cleanup confirmation is fail-closed, not assumed
# --------------------------------------------------------------------


def test_transaction_cleanup_confirms_real_kill_and_reap(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When abort itself fails, __exit__ must still fall through to a
    real kill+wait and confirm the child actually exited."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    def broken_abort(self):
        raise CheckpointRefError(CheckpointRefFailure.GIT_COMMAND_TIMEOUT, "simulated")

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "abort", broken_abort)

    transaction = checkpoint_ref_module._RefTransaction(repo)
    transaction.__enter__()
    transaction.begin(f"update {ref.ref_name} {second} {first}")
    transaction.__exit__(None, None, None)  # must not raise: real kill/wait succeeds

    assert transaction._process.poll() is not None
    assert _ref_value(repo, ref.ref_name) == first
    _assert_lock_released(repo, ref)


def test_transaction_cleanup_unconfirmed_when_final_wait_times_out(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that cannot be confirmed dead after every bounded
    attempt must be surfaced, never silently treated as cleaned up."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    transaction = checkpoint_ref_module._RefTransaction(repo)
    transaction.__enter__()
    transaction.begin(f"update {ref.ref_name} {second} {first}")
    process = transaction._process
    original_kill = process.kill
    original_wait = process.wait

    monkeypatch.setattr(process, "poll", lambda: None)  # always "still running"
    monkeypatch.setattr(
        process,
        "wait",
        lambda timeout=None: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        ),
    )
    kill_calls = {"count": 0}

    def fake_kill():
        kill_calls["count"] += 1

    monkeypatch.setattr(process, "kill", fake_kill)

    try:
        with pytest.raises(CheckpointRefError) as excinfo:
            transaction.__exit__(None, None, None)
        assert excinfo.value.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED
        assert kill_calls["count"] >= 1
        # No false claim that the lock was released: the message must
        # say the release status is unknown, phrased as a question
        # ("whether ... was released is unknown"), never asserted as a
        # fact on its own.
        message = str(excinfo.value).lower()
        assert "whether" in message and "lock was released is unknown" in message
    finally:
        # Real cleanup so this test does not itself leak a process/lock.
        original_kill()
        original_wait(timeout=5)


def test_cleanup_unconfirmed_preserves_abort_failure_as_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    abort_failure = CheckpointRefError(
        CheckpointRefFailure.GIT_COMMAND_TIMEOUT, "simulated abort failure"
    )

    transaction = checkpoint_ref_module._RefTransaction(repo)
    transaction.__enter__()
    transaction.begin(f"update {ref.ref_name} {second} {first}")
    process = transaction._process
    original_kill = process.kill
    original_wait = process.wait

    monkeypatch.setattr(transaction, "abort", lambda: (_ for _ in ()).throw(abort_failure))
    monkeypatch.setattr(process, "poll", lambda: None)
    monkeypatch.setattr(
        process,
        "wait",
        lambda timeout=None: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        ),
    )
    monkeypatch.setattr(process, "kill", lambda: None)

    try:
        with pytest.raises(CheckpointRefError) as excinfo:
            transaction.__exit__(None, None, None)
        assert excinfo.value.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED
        assert excinfo.value.__cause__ is abort_failure
    finally:
        original_kill()
        original_wait(timeout=5)


def test_cleanup_unconfirmed_preserves_body_exception_as_context(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real exception from the `with`-block body must remain visible
    (as __context__) even though __exit__ itself also fails."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    class Boom(RuntimeError):
        pass

    def fake_abort(self):
        pass  # pretend to abort without touching the real process

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "abort", fake_abort)

    holder: dict = {}
    with pytest.raises(CheckpointRefError) as excinfo:
        with checkpoint_ref_module._RefTransaction(repo) as transaction:
            transaction.begin(f"update {ref.ref_name} {second} {first}")
            process = transaction._process
            holder["kill"] = process.kill
            holder["wait"] = process.wait
            monkeypatch.setattr(process, "poll", lambda: None)
            monkeypatch.setattr(
                process,
                "wait",
                lambda timeout=None: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(cmd="git", timeout=timeout)
                ),
            )
            monkeypatch.setattr(process, "kill", lambda: None)
            raise Boom("body failed")

    assert excinfo.value.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED
    assert isinstance(excinfo.value.__context__, Boom)

    # Real cleanup: the fake abort left the real process alive.
    holder["kill"]()
    holder["wait"](timeout=5)


# --------------------------------------------------------------------
# Causal chaining for a symbolic ref found after an original failure
# --------------------------------------------------------------------


def test_symbolic_state_after_original_failure_preserves_cause(
    repo: Path, ref: CheckpointRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the post-mutation observation finds a symbolic ref after the
    transaction itself already failed for a known reason, that original
    reason must be recoverable via __cause__."""
    first = _head(repo)
    ref.create(first)
    second = _commit(repo, "two\n")

    launch_failure = CheckpointRefError(
        CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE,
        "the git executable could not be launched",
    )

    def fake_begin(self, update_line):
        # A competitor substitutes a symbolic ref before our own
        # transaction even manages to start.
        _git(repo, "update-ref", "refs/heads/pin", first)
        _git(repo, "update-ref", "--no-deref", "-d", ref.ref_name, first)
        _git(repo, "symbolic-ref", ref.ref_name, "refs/heads/pin")
        raise launch_failure

    monkeypatch.setattr(checkpoint_ref_module._RefTransaction, "begin", fake_begin)

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.advance(expected_old_oid=first, new_oid=second)
    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
    assert excinfo.value.__cause__ is launch_failure


def test_symbolic_state_without_original_failure_is_not_falsely_chained(
    repo: Path, ref: CheckpointRef
) -> None:
    """The ordinary case (no prior transaction failure): a bare
    SYMBOLIC_REF is raised with no fabricated cause."""
    first = _head(repo)
    ref.create(first)
    _git(repo, "update-ref", "refs/heads/pin", first)
    _git(repo, "update-ref", "--no-deref", "-d", ref.ref_name, first)
    _git(repo, "symbolic-ref", ref.ref_name, "refs/heads/pin")

    with pytest.raises(CheckpointRefError) as excinfo:
        ref.observe()
    assert excinfo.value.reason is CheckpointRefFailure.SYMBOLIC_REF
    assert excinfo.value.__cause__ is None
