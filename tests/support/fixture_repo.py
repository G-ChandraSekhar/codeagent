"""Test-only helper: copy the version-controlled retry_worker fixture
into a fresh temporary directory and turn it into a real, throwaway Git
repository (git init + initial commit). No nested .git is ever committed
as fixture data — this is what creates one, per test, at run time.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

FIXTURE_SOURCE = Path(__file__).resolve().parent.parent / "fixtures" / "retry_worker"

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent Test",
    "-c",
    "user.email=codeagent-test@example.invalid",
]


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )


@contextmanager
def real_fixture_repo() -> Iterator[Path]:
    """Yield the path to a fresh, real Git repository containing a copy
    of the retry_worker fixture, with one initial commit. Removed on
    exit regardless of what the caller does to it."""
    with TemporaryDirectory(prefix="codeagent-fixture-") as tmp:
        repo_path = Path(tmp) / "retry_worker"
        shutil.copytree(FIXTURE_SOURCE, repo_path)
        _run_git(repo_path, "init", "--initial-branch=main", "--quiet")
        _run_git(repo_path, "add", "-A")
        _run_git(repo_path, "commit", "--quiet", "-m", "Initial commit of retry_worker fixture")
        yield repo_path
