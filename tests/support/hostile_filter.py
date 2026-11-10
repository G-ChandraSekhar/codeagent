"""Shared test support for ADR 0006 Amendment 5 (the mandatory pre-D5-C
hotfix): a real hostile filter driver whose helper touches a sentinel, a
recording spy over every launched `git` process, and a cycle-safe
reachability walk proving a surfaced failure retains no hostile value.

Test-only; never imported by production code.
"""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_GIT_IDENTITY = ["-c", "user.name=CodeAgent Test", "-c", "user.email=codeagent-test@example.invalid"]

NOTHING_RAISED = object()

# A configured name that Git's `-c` parsing cannot address (ADR 0006 A5.1).
REFUSED_NAMES = ("a=b", "=a", "a=", "x.y=z")
RENDERABLE_NAMES = ("ab", "a.b", "x.clean", "é")


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=check, capture_output=True, text=True, env=clean_env(),
    )


def helper_command(sentinel: Path, subkey: str) -> str:
    """`clean` passes content through, so a neutralized or bypassed run is
    otherwise indistinguishable; `process` exits at once (Git then fails the
    protocol, but the sentinel already proves the helper executed)."""
    if subkey == "process":
        return f"sh -c 'touch {sentinel}'"
    return f"sh -c 'touch {sentinel}; cat'"


def configure_hostile_driver(
    repo: Path, name: str, subkey: str, sentinel: Path, *, pattern: str = "*.txt"
) -> None:
    """Attribute `pattern` to driver `name` through the **common-directory**
    `info/attributes` (no committed `.gitattributes`), and configure the
    driver's `subkey` to a helper that touches `sentinel`."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    (common / "info").mkdir(parents=True, exist_ok=True)
    (common / "info" / "attributes").write_text(f"{pattern} filter={name}\n")
    git(repo, "config", f"filter.{name}.{subkey}", helper_command(sentinel, subkey))


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=clean_env())
    (path / "a.txt").write_text("one\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


def make_stat_dirty(path: Path) -> None:
    """Advance the file's mtime so Git must re-read (and filter) it."""
    st = path.stat()
    later = max(st.st_mtime, time.time()) + 2
    os.utime(path, (later, later))


def take_sentinel(sentinel: Path) -> bool:
    present = sentinel.exists()
    if present:
        sentinel.unlink()
    return present


_GLOBAL_FLAGS = {"--no-pager", "--no-lazy-fetch", "--no-replace-objects", "--literal-pathspecs"}


def describe(argv) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return `(subcommand words, -c override values after -C)` for a git argv.

    The subcommand is the first non-option word plus its first argument
    (for `diff`, which distinguishes `--cached` from `--quiet`)."""
    args = list(argv[1:])
    overrides: list[str] = []
    seen_repo = False
    i = 0
    while i < len(args):
        word = args[i]
        if word in _GLOBAL_FLAGS:
            i += 1
        elif word == "-c":
            if seen_repo:
                overrides.append(args[i + 1])
            i += 2
        elif word == "-C":
            seen_repo = True
            i += 2
        else:
            rest = args[i:]
            words = (rest[0], rest[1]) if rest[0] == "diff" and len(rest) > 1 else (rest[0],)
            return words, tuple(overrides)
    return (), tuple(overrides)


class GitLaunchSpy:
    """Records every `git` process launched through `subprocess.Popen`."""

    def __init__(self) -> None:
        self.launches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    @property
    def commands(self) -> list[tuple[str, ...]]:
        return [words for words, _ in self.launches]

    def count(self, *words: str) -> int:
        return sum(1 for got in self.commands if got[: len(words)] == words)


@contextmanager
def spy_git_launches(monkeypatch) -> Iterator[GitLaunchSpy]:
    spy = GitLaunchSpy()
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == "git":
                spy.launches.append(describe(args))
            super().__init__(args, *a, **k)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", Recording)
        yield spy


def capture(call):
    """Run `call` and return what it raised (any `BaseException`), or
    `NOTHING_RAISED`. Catching here keeps pytest itself uninterrupted."""
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - test-side capture only
        return exc
    return NOTHING_RAISED


def reachable(root) -> tuple[list[BaseException], list[str]]:
    """Cycle-safe walk over `__cause__`, `__context__`, `args` and
    `__dict__` values; returns every reachable exception and every
    reachable string (non-string leaves contribute their `repr`)."""
    exceptions: list[BaseException] = []
    strings: list[str] = []
    seen: set[int] = set()
    stack = [root]
    while stack:
        item = stack.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, BaseException):
            exceptions.append(item)
            strings.append(str(item))
            stack.extend([item.__cause__, item.__context__, *item.args])
            stack.extend(getattr(item, "__dict__", {}).values())
        elif isinstance(item, str):
            strings.append(item)
        elif isinstance(item, (tuple, list, set, frozenset)):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        else:
            strings.append(repr(item))
            stack.extend(getattr(item, "__dict__", {}).values())
    return exceptions, strings


def assert_no_hostile_value(root, hostile: tuple[str, ...]) -> list[BaseException]:
    exceptions, strings = reachable(root)
    for value in hostile:
        assert value, "empty hostile value would match everything"
        leaked = [s for s in strings if value in s]
        assert leaked == [], (value, leaked)
    return exceptions
