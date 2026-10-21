"""Ledger D4a: the descriptor-relative repository toolkit (`repo_tools`).

Real temporary directories throughout. Covers the D4 plan's R7 (search
pagination), R8 (bounded enumeration), R10/R10b (administrative name and
inode rules), R13-unit (cooperative stops), R17 (no subprocess, no
lifecycle imports), R20 (payload cap), R21 (raw-byte list cursors), R22
(descriptor ownership and cleanup dominance), R28/R37 (completeness and
authenticated chains), R32 (root checks), R35 (depth) and R40 (nonces).
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from codeagent import repo_tools, tool_schema
from codeagent.errors import ErrorCode
from codeagent.repo_tools import (
    PAYLOAD_CAP_BYTES,
    READ_CONTENT_CAP_BYTES,
    SEARCH_RESERVE,
    LIST_RESERVE,
    RepoTools,
)
from codeagent.domain import ToolName
from codeagent.tool_schema import (
    ArgumentRejection,
    ListDirectoryArgs,
    ReadFileArgs,
    SearchTextArgs,
    ToolArgumentError,
    ToolCleanupUnconfirmedError,
    canonical_json_bytes,
    encode_search_cursor,
    parse_tool_arguments,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "codeagent"


def _tree(root: pathlib.Path, files: dict[str, str | bytes]) -> pathlib.Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    return root


def _ok(outcome):
    assert outcome.ok, outcome.error
    return outcome.payload


def _err(outcome, code: ErrorCode):
    assert not outcome.ok
    assert outcome.error.code is code, outcome.error
    return outcome.error.message


def _size(payload) -> int:
    return len(canonical_json_bytes(payload))


def _open_fds() -> set[int]:
    path = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return {int(n) for n in os.listdir(path)}


def _nonces(*values: str):
    it = iter(values)
    return lambda: next(it)


# --- construction and root checks (R32) --------------------------------------


def test_construction_requires_a_real_directory(tmp_path):
    (tmp_path / "f").write_text("x")
    os.symlink(tmp_path, tmp_path / "link")
    for bad in (tmp_path / "missing", tmp_path / "f", tmp_path / "link"):
        with pytest.raises(ValueError):
            RepoTools(bad)
    RepoTools(tmp_path)


def test_root_replaced_by_another_directory_is_refused(tmp_path):
    root = tmp_path / "wt"
    _tree(root, {"a.py": "x\n"})
    tools = RepoTools(root)
    root.rename(tmp_path / "old")
    _tree(root, {"a.py": "evil\n"})
    msg = _err(tools.read_file(ReadFileArgs("a.py")), ErrorCode.TOOL_EXECUTION_FAILED)
    assert "worktree root changed" in msg


def test_root_replaced_by_a_symlink_is_refused(tmp_path):
    root = tmp_path / "wt"
    _tree(root, {"a.py": "x\n"})
    tools = RepoTools(root)
    root.rename(tmp_path / "old")
    os.symlink(tmp_path / "old", root)
    _err(tools.read_file(ReadFileArgs("a.py")), ErrorCode.TOOL_EXECUTION_FAILED)


def test_tools_hold_no_descriptor_and_have_no_close(tmp_path):
    before = _open_fds()
    tools = RepoTools(tmp_path)
    assert _open_fds() == before
    assert not hasattr(tools, "close")
    assert set(vars(tools)) == {"_path", "_identity", "_chains", "_error_seq"}


# --- read_file ----------------------------------------------------------------


def test_read_file_ranges_and_line_numbers(tmp_path):
    _tree(tmp_path, {"a.py": "one\ntwo\nthree"})
    tools = RepoTools(tmp_path)
    page = _ok(tools.read_file(ReadFileArgs("a.py")))
    assert page == {"eof": True, "lines": [[1, "one\n"], [2, "two\n"], [3, "three"]], "next_start_line": None,
                    "page_truncated": False}
    page = _ok(tools.read_file(ReadFileArgs("a.py", 2, 2)))
    assert page["lines"] == [[2, "two\n"]] and page["next_start_line"] == 3 and not page["eof"]
    assert _ok(tools.read_file(ReadFileArgs("a.py", 9)))["lines"] == []


def test_read_file_default_window_is_2000_lines(tmp_path):
    _tree(tmp_path, {"a.py": "x\n" * 2500})
    page = _ok(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")))
    assert len(page["lines"]) == 2000 and page["next_start_line"] == 2001 and not page["page_truncated"]


def test_read_file_content_cap_stops_at_a_line_boundary_and_resumes_exactly(tmp_path):
    line = "y" * 1023 + "\n"
    text = line * 150
    _tree(tmp_path, {"a.py": text})
    tools = RepoTools(tmp_path)
    collected, start = "", 1
    while True:
        page = _ok(tools.read_file(ReadFileArgs("a.py", start)))
        assert sum(len(t.encode()) for _, t in page["lines"]) <= READ_CONTENT_CAP_BYTES
        collected += "".join(t for _, t in page["lines"])
        if page["eof"]:
            break
        assert page["page_truncated"]
        start = page["next_start_line"]
    assert collected == text


def test_read_file_payload_cap_is_tight(tmp_path):
    # Control characters escape six-fold, so the payload cap binds first.
    line = "\x01" * 499 + "\n"
    _tree(tmp_path, {"a.py": line * 100})
    page = _ok(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")))
    assert page["page_truncated"]
    assert _size(page) <= PAYLOAD_CAP_BYTES
    nxt = [page["next_start_line"], line]
    projected = repo_tools.READ_RESERVE + sum(_size(i) for i in page["lines"]) + len(page["lines"]) - 1
    assert projected + 1 + _size(nxt) > PAYLOAD_CAP_BYTES


def test_read_file_content_cap_boundary_is_exact(tmp_path):
    # 64 lines of 1,024 bytes fill the cap exactly; a one-byte 65th line
    # would make it 65,537 and must start the next page.
    _tree(tmp_path, {"a.py": ("q" * 1023 + "\n") * 64 + "\n"})
    page = _ok(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")))
    assert len(page["lines"]) == 64 and page["page_truncated"] and page["next_start_line"] == 65
    assert sum(len(t.encode()) for _, t in page["lines"]) == READ_CONTENT_CAP_BYTES


def test_projected_size_equals_the_real_canonical_size():
    """`_Page` accounting is exact: with every reserved scalar at its
    maximum, the projected size equals the canonical size."""
    cursor = "x" * tool_schema.MAX_CURSOR_BYTES
    items = [{"name": f"n{i}", "type": "file", "size": i} for i in range(7)]
    page = repo_tools._Page(LIST_RESERVE)
    for item in items:
        page.add(item)
    assert page.projected == _size({"entries": items, "next_cursor": cursor, "page_truncated": False})
    lines = [[i, "t\x01" * i] for i in range(1, 6)]
    page = repo_tools._Page(repo_tools.READ_RESERVE)
    for line in lines:
        page.add(line)
    assert page.projected == _size({"eof": False, "lines": lines, "next_start_line": 10**9, "page_truncated": False})


def test_fits_counts_the_separator_exactly():
    page = repo_tools._Page(LIST_RESERVE)
    page.add({"name": "a", "type": "file"})
    room = PAYLOAD_CAP_BYTES - page.projected  # includes the one separator byte

    def item_of_size(n):
        base = _size({"name": "", "type": "file"})
        return {"name": "x" * (n - base), "type": "file"}

    assert _size(item_of_size(room - 1)) == room - 1 and page.fits(item_of_size(room - 1))
    assert _size(item_of_size(room)) == room and not page.fits(item_of_size(room))


def test_read_file_single_overlong_line_is_an_error(tmp_path):
    _tree(tmp_path, {"a.py": "z" * (READ_CONTENT_CAP_BYTES + 1)})
    msg = _err(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")), ErrorCode.TOOL_EXECUTION_FAILED)
    assert "line exceeds" in msg


def test_read_file_overlong_pending_line_truncates_after_earlier_lines(tmp_path):
    _tree(tmp_path, {"a.py": "short\n" + "z" * (READ_CONTENT_CAP_BYTES + 10)})
    page = _ok(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")))
    assert page["lines"] == [[1, "short\n"]] and page["page_truncated"] and page["next_start_line"] == 2


def test_read_file_refuses_files_over_the_scan_limit_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "big.py"
    with open(path, "wb") as fh:
        fh.truncate(repo_tools.READ_FILE_MAX_BYTES + 1)
    reads = []
    monkeypatch.setattr(repo_tools, "_read", lambda fd, n: reads.append(n) or b"")
    msg = _err(RepoTools(tmp_path).read_file(ReadFileArgs("big.py")), ErrorCode.TOOL_EXECUTION_FAILED)
    assert "scan limit" in msg and reads == []


def test_read_file_non_utf8(tmp_path):
    _tree(tmp_path, {"a.py": b"ok\n\xff\xfe\n"})
    _err(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")), ErrorCode.TOOL_EXECUTION_FAILED)


def test_read_file_symlinks_are_refused_at_every_component(tmp_path):
    _tree(tmp_path, {"real/a.py": "x\n"})
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("OUTSIDE_SECRET\n")
    os.symlink(outside, tmp_path / "leaf.py")
    os.symlink(tmp_path / "real", tmp_path / "dirlink")
    tools = RepoTools(tmp_path)
    for path in ("leaf.py", "dirlink/a.py"):
        msg = _err(tools.read_file(ReadFileArgs(path)), ErrorCode.TOOL_INPUT_INVALID)
        assert "symlink" in msg and "OUTSIDE_SECRET" not in msg


def test_read_file_swap_to_symlink_between_calls_is_refused(tmp_path):
    _tree(tmp_path, {"d/a.py": "inside\n", "other/a.py": "OTHER\n"})
    tools = RepoTools(tmp_path)
    assert _ok(tools.read_file(ReadFileArgs("d/a.py")))["lines"] == [[1, "inside\n"]]
    (tmp_path / "d").rename(tmp_path / "d-old")
    os.symlink(tmp_path / "other", tmp_path / "d")
    _err(tools.read_file(ReadFileArgs("d/a.py")), ErrorCode.TOOL_INPUT_INVALID)


def test_read_file_fifo_never_blocks(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    tools = RepoTools(tmp_path)
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("o", tools.read_file(ReadFileArgs("pipe"))), daemon=True
    )
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "opening a FIFO blocked"
    assert "not a regular file" in _err(result["o"], ErrorCode.TOOL_INPUT_INVALID)


def test_read_file_missing_and_non_directory_components(tmp_path):
    _tree(tmp_path, {"f.py": "x\n"})
    tools = RepoTools(tmp_path)
    assert "does not exist" in _err(tools.read_file(ReadFileArgs("nope.py")), ErrorCode.TOOL_INPUT_INVALID)
    assert "not a directory" in _err(tools.read_file(ReadFileArgs("f.py/x")), ErrorCode.TOOL_INPUT_INVALID)


def test_read_file_cooperative_stop(tmp_path):
    _tree(tmp_path, {"a.py": "x\n"})
    msg = _err(RepoTools(tmp_path).read_file(ReadFileArgs("a.py"), lambda: True), ErrorCode.TOOL_EXECUTION_FAILED)
    assert "interrupted" in msg


# --- administrative entry (R10, R10b) -------------------------------------------


def _linked_worktree(tmp_path: pathlib.Path) -> pathlib.Path:
    src = tmp_path / "src"
    src.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", str(src)], check=True)
    (src / "a.py").write_text("visible\n")
    subprocess.run([*git, "-C", str(src), "add", "a.py"], check=True)
    subprocess.run([*git, "-C", str(src), "commit", "-q", "-m", "c"], check=True)
    wt = tmp_path / "wt"
    subprocess.run([*git, "-C", str(src), "worktree", "add", "-q", str(wt)], check=True)
    assert (wt / ".git").is_file()
    return wt


@pytest.mark.parametrize("name_rule", [True, False])
def test_hard_link_alias_of_the_git_file_is_refused_by_inode(tmp_path, monkeypatch, name_rule):
    wt = _linked_worktree(tmp_path)
    os.link(wt / ".git", wt / "alias")
    monkeypatch.setattr(tool_schema, "_ADMIN_NAME_RULE_ENABLED", name_rule)
    tools = RepoTools(wt)
    msg = _err(tools.read_file(ReadFileArgs("alias")), ErrorCode.TOOL_INPUT_INVALID)
    assert "administrative" in msg and str(tmp_path) not in msg
    names = {e["name"] for e in _ok(tools.list_directory(ListDirectoryArgs()))["entries"]}
    assert "alias" not in names and ".git" not in names and "a.py" in names
    page = _ok(tools.search_text(SearchTextArgs("gitdir")))
    assert page["results"] == [] and page["excluded"]["administrative"] == 2
    whole = _ok(tools.read_file(ReadFileArgs("a.py")))
    assert whole["lines"] == [[1, "visible\n"]]


def test_name_rule_disabled_still_refuses_the_git_file_by_inode(tmp_path, monkeypatch):
    wt = _linked_worktree(tmp_path)
    monkeypatch.setattr(tool_schema, "_ADMIN_NAME_RULE_ENABLED", False)
    msg = _err(RepoTools(wt).read_file(ReadFileArgs(".git")), ErrorCode.TOOL_INPUT_INVALID)
    assert "administrative" in msg


def test_case_variant_opens_the_same_inode_on_case_insensitive_filesystems(tmp_path, monkeypatch):
    wt = _linked_worktree(tmp_path)
    if not (wt / ".GIT").exists():
        pytest.skip("case-sensitive filesystem; R10b covers the inode rule here")
    monkeypatch.setattr(tool_schema, "_ADMIN_NAME_RULE_ENABLED", False)
    _err(RepoTools(wt).read_file(ReadFileArgs(".GIT")), ErrorCode.TOOL_INPUT_INVALID)


def test_nested_git_directories_are_hidden_from_listing_and_search(tmp_path):
    _tree(tmp_path, {"sub/.git/config": "needle\n", "sub/ok.py": "needle\n", "other/.GIT": "needle\n"})
    tools = RepoTools(tmp_path)
    names = {e["name"] for e in _ok(tools.list_directory(ListDirectoryArgs("sub")))["entries"]}
    assert names == {"ok.py"}
    assert _ok(tools.list_directory(ListDirectoryArgs("other")))["entries"] == []
    page = _ok(tools.search_text(SearchTextArgs("needle")))
    assert [r["path"] for r in page["results"]] == ["sub/ok.py"]
    assert page["excluded"]["administrative"] == 2 and page["page_coverage_complete"]


# --- list_directory (R8, R20, R21) ---------------------------------------------


def test_list_directory_entries_types_and_sizes(tmp_path):
    _tree(tmp_path, {"b.py": "12345", "a/x": "", "c.txt": ""})
    os.symlink(tmp_path / "b.py", tmp_path / "l")
    os.mkfifo(tmp_path / "p")
    page = _ok(RepoTools(tmp_path).list_directory(ListDirectoryArgs()))
    assert page["entries"] == [
        {"name": "a", "type": "dir"},
        {"name": "b.py", "type": "file", "size": 5},
        {"name": "c.txt", "type": "file", "size": 0},
        {"name": "l", "type": "symlink"},
        {"name": "p", "type": "other"},
    ]
    assert page["next_cursor"] is None and not page["page_truncated"]


def test_list_directory_pagination_has_no_duplication_or_omission(tmp_path):
    names = [f"f{i:03d}" for i in range(23)]
    _tree(tmp_path, {n: "" for n in names})
    tools = RepoTools(tmp_path)
    seen, cursor = [], None
    while True:
        page = _ok(tools.list_directory(ListDirectoryArgs(".", cursor, 5)))
        seen += [e["name"] for e in page["entries"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == names


def test_list_directory_payload_cap_is_tight(tmp_path):
    _tree(tmp_path, {("n" * 200 + f"{i:04d}"): "" for i in range(500)})
    page = _ok(RepoTools(tmp_path).list_directory(ListDirectoryArgs(".", None, 500)))
    assert page["page_truncated"] and _size(page) <= PAYLOAD_CAP_BYTES
    items = page["entries"]
    projected = LIST_RESERVE + sum(_size(i) for i in items) + len(items) - 1
    assert projected + 1 + _size(items[-1]) > PAYLOAD_CAP_BYTES


def test_list_directory_entry_maximum_fits_far_below_the_cap():
    worst = {"name": "\x01" * 255, "type": "other", "size": 10**18}
    assert _size(worst) <= 1600
    assert LIST_RESERVE + _size(worst) < PAYLOAD_CAP_BYTES


def test_list_directory_refuses_oversized_directories(tmp_path, monkeypatch):
    _tree(tmp_path, {f"f{i}": "" for i in range(11)})
    monkeypatch.setattr(repo_tools, "DIRECTORY_MAX_ENTRIES", 10)
    msg = _err(RepoTools(tmp_path).list_directory(ListDirectoryArgs()), ErrorCode.TOOL_EXECUTION_FAILED)
    assert "enumeration limit" in msg
    monkeypatch.setattr(repo_tools, "DIRECTORY_MAX_ENTRIES", 11)
    monkeypatch.setattr(repo_tools, "DIRECTORY_MAX_NAME_BYTES", 21)
    _err(RepoTools(tmp_path).list_directory(ListDirectoryArgs()), ErrorCode.TOOL_EXECUTION_FAILED)


def test_enumeration_stops_without_reading_past_the_cap(tmp_path, monkeypatch):
    _tree(tmp_path, {f"f{i}": "" for i in range(50)})
    monkeypatch.setattr(repo_tools, "DIRECTORY_MAX_ENTRIES", 10)
    stats = []
    real_lstat = os.lstat

    def counting_lstat(path, *a, **k):
        stats.append(path)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(repo_tools.os, "lstat", counting_lstat)
    RepoTools(tmp_path).list_directory(ListDirectoryArgs())
    assert len([s for s in stats if isinstance(s, bytes)]) <= 10  # entry lstats only


def test_list_directory_cooperative_stop_every_256_entries(tmp_path):
    _tree(tmp_path, {f"f{i}": "" for i in range(300)})
    calls = []
    outcome = RepoTools(tmp_path).list_directory(ListDirectoryArgs(), lambda: calls.append(1) or True)
    assert "interrupted" in _err(outcome, ErrorCode.TOOL_EXECUTION_FAILED) and calls == [1]


def test_list_cursor_with_a_non_utf8_name_on_the_page_boundary(tmp_path):
    try:
        fd = os.open(os.path.join(os.fsencode(tmp_path), b"m\xff"), os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 names")
    os.close(fd)
    _tree(tmp_path, {"a": "", "z": ""})
    tools = RepoTools(tmp_path)
    first = _ok(tools.list_directory(ListDirectoryArgs(".", None, 2)))
    assert first["entries"][1] == {"name": None, "name_b64": "bf8", "type": "file", "size": 0}
    second = _ok(tools.list_directory(ListDirectoryArgs(".", first["next_cursor"], 2)))
    assert [e["name"] for e in second["entries"]] == ["z"]


# --- search_text (R7, R28, R35, R37, R40) -----------------------------------------


def _search_all(tools, query, **kw):
    pages, cursor = [], None
    while True:
        assert len(pages) < 500, "pagination does not terminate"
        page = _ok(tools.search_text(SearchTextArgs(query, cursor=cursor, **kw)))
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_search_results_one_per_line_with_column_and_occurrences(tmp_path):
    _tree(tmp_path, {"a.py": "x needle needle\nnone\n  needle"})
    page = _ok(RepoTools(tmp_path).search_text(SearchTextArgs("needle")))
    assert [(r["line"], r["column"], r["occurrences"]) for r in page["results"]] == [(1, 3, 2), (3, 3, 1)]
    assert page["search_complete"] and page["page_coverage_complete"] and page["next_cursor"] is None


def test_search_snippet_is_bounded_and_contains_the_match(tmp_path):
    _tree(tmp_path, {"a.py": "a" * 1000 + "NEEDLE" + "b" * 1000})
    (result,) = _ok(RepoTools(tmp_path).search_text(SearchTextArgs("NEEDLE")))["results"]
    assert len(result["snippet"]) == repo_tools.SNIPPET_MAX_CHARS and "NEEDLE" in result["snippet"]


def _multi_tree(tmp_path):
    return _tree(
        tmp_path,
        {
            "a/one.py": "hit\nmiss\nhit hit\n" * 4,
            "a/b/two.py": "hit\n" * 7,
            "c.py": "nothing\n",
            "d/e/f/three.py": "x hit\n" * 3,
            "z.py": "hit\n",
        },
    )


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_search_pagination_equals_a_single_unbounded_search(tmp_path, limit):
    tools = RepoTools(_multi_tree(tmp_path))
    full = _ok(tools.search_text(SearchTextArgs("hit", limit=100)))
    assert full["next_cursor"] is None
    pages = _search_all(tools, "hit", limit=limit)
    assert [r for p in pages for r in p["results"]] == full["results"]
    assert pages[-1]["search_complete"] and all(not p["search_complete"] for p in pages[:-1])
    assert all(p["page_truncated"] for p in pages[:-1])


def test_search_entry_and_scan_caps_paginate_without_loss(tmp_path, monkeypatch):
    tools = RepoTools(_multi_tree(tmp_path))
    full = _ok(tools.search_text(SearchTextArgs("hit", limit=100)))["results"]
    # 70 bytes: above the largest file (68) so every page can progress.
    for name, value in (("SEARCH_ENTRY_CAP", 1), ("SEARCH_SCAN_CAP_BYTES", 70)):
        monkeypatch.setattr(repo_tools, name, value)
        pages = _search_all(tools, "hit", limit=100)
        assert len(pages) > 1 and [r for p in pages for r in p["results"]] == full
        assert pages[-1]["search_complete"]
        monkeypatch.undo()


def test_search_result_maximum_fits_far_below_the_cap():
    worst = {
        "column": 10**9,
        "line": 10**9,
        "occurrences": 10**9,
        "path": '"' * tool_schema.MAX_PATH_BYTES,
        "snippet": "\x01" * repo_tools.SNIPPET_MAX_CHARS,
    }
    assert SEARCH_RESERVE + _size(worst) < PAYLOAD_CAP_BYTES
    assert _size(worst) <= 2600


def test_search_payload_cap_is_tight(tmp_path):
    _tree(tmp_path, {f"f{i:03d}.py": ("\x01" * 230 + "NEEDLE\n") * 1 for i in range(100)})
    page = _ok(RepoTools(tmp_path).search_text(SearchTextArgs("NEEDLE", limit=100)))
    assert page["page_truncated"] and _size(page) <= PAYLOAD_CAP_BYTES
    items = page["results"]
    projected = SEARCH_RESERVE + sum(_size(i) for i in items) + len(items) - 1
    assert projected + 1 + _size(items[-1]) > PAYLOAD_CAP_BYTES


def _counter_case(tmp_path, monkeypatch, case):
    """Build a tree producing exactly one entry in one category."""
    _tree(tmp_path, {"ok.py": "needle\n"})
    if case == "symlinks":
        os.symlink(tmp_path / "ok.py", tmp_path / "l.py")
    elif case == "non_regular":
        os.mkfifo(tmp_path / "p")
    elif case == "administrative":
        _tree(tmp_path, {".git": "gitdir: needle\n"})
    elif case == "vanished":
        _tree(tmp_path, {"gone.py": "needle\n"})
        real = os.lstat

        def lstat(path, *a, **k):
            if path == b"gone.py":
                raise FileNotFoundError(path)
            return real(path, *a, **k)

        monkeypatch.setattr(repo_tools.os, "lstat", lstat)
    elif case == "oversized_files":
        _tree(tmp_path, {"big.py": "needle\n" + "x" * repo_tools.SEARCH_FILE_MAX_BYTES})
    elif case == "binary_files":
        _tree(tmp_path, {"bin.py": b"needle\x00\n"})
    elif case == "non_utf8_files":
        _tree(tmp_path, {"bad.py": b"needle \xff\n"})
    elif case == "non_utf8_names":
        try:
            fd = os.open(os.path.join(os.fsencode(tmp_path), b"n\xff"), os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError:
            pytest.skip("filesystem refuses non-UTF-8 names")
        os.close(fd)
    elif case == "oversized_directories":
        _tree(tmp_path, {f"big/f{i}": "needle\n" for i in range(4)})
        monkeypatch.setattr(repo_tools, "DIRECTORY_MAX_ENTRIES", 3)
    elif case == "too_deep":
        _tree(tmp_path, {"/".join(["d"] * 32) + "/deep.py": "needle\n"})
    elif case == "unaddressable_names":
        _tree(tmp_path, {"ctl\x01.py": "needle\n"})
    elif case == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root ignores file modes")
        _tree(tmp_path, {"secret.py": "needle\n"})
        os.chmod(tmp_path / "secret.py", 0)
    return RepoTools(tmp_path)


EXCLUDED = ["symlinks", "non_regular", "administrative", "vanished"]
UNSEARCHED = [
    "oversized_files",
    "binary_files",
    "non_utf8_files",
    "non_utf8_names",
    "oversized_directories",
    "too_deep",
    "unaddressable_names",
    "unreadable",
]


@pytest.mark.parametrize("case", EXCLUDED)
def test_excluded_categories_do_not_affect_coverage(tmp_path, monkeypatch, case):
    page = _ok(_counter_case(tmp_path, monkeypatch, case).search_text(SearchTextArgs("needle")))
    assert page["excluded"][case] == 1
    assert sum(page["excluded"].values()) == 1 and sum(page["unsearched"].values()) == 0
    assert page["page_coverage_complete"] and page["search_complete"]
    assert [r["path"] for r in page["results"]] == ["ok.py"]


@pytest.mark.parametrize("case", UNSEARCHED)
def test_unsearched_categories_make_the_search_incomplete(tmp_path, monkeypatch, case):
    page = _ok(_counter_case(tmp_path, monkeypatch, case).search_text(SearchTextArgs("needle")))
    assert page["unsearched"][case] == 1
    assert sum(page["unsearched"].values()) == 1
    assert not page["page_coverage_complete"] and not page["search_complete"]
    assert page["next_cursor"] is None


def test_depth_is_measured_from_the_worktree_root_not_the_search_path(tmp_path):
    deep = ["a", "b"] + ["d"] * 30  # depth 32 from the root
    _tree(tmp_path, {"/".join(deep) + "/ok.py": "needle\n"})
    tools = RepoTools(tmp_path)
    page = _ok(tools.search_text(SearchTextArgs("needle", path="a/b")))
    assert page["results"] == [] and page["unsearched"]["too_deep"] == 1
    _tree(tmp_path, {"/".join(deep[:-1]) + "/ok.py": "needle\n"})
    hits = _ok(tools.search_text(SearchTextArgs("needle", path="a/b")))["results"]
    assert [len(r["path"].split("/")) for r in hits] == [32]


def test_each_entry_is_counted_on_exactly_one_page(tmp_path):
    _tree(tmp_path, {f"f{i}.py": b"needle\x00" for i in range(5)})
    _tree(tmp_path, {"g.py": "needle\nneedle\nneedle\n"})
    pages = _search_all(RepoTools(tmp_path), "needle", limit=1)
    assert sum(p["unsearched"]["binary_files"] for p in pages) == 5
    assert not pages[-1]["search_complete"]  # coverage gap on an earlier page


def test_search_cooperative_stop_between_files(tmp_path, monkeypatch):
    """With a stop already requested, the check before each file fires
    before that file is ever opened."""
    _tree(tmp_path, {"a.py": "needle\n", "b.py": "needle\n"})
    tools = RepoTools(tmp_path)
    opened = []
    real_open = os.open

    def tracking_open(name, *a, **k):
        if isinstance(name, bytes):
            opened.append(name)
        return real_open(name, *a, **k)

    monkeypatch.setattr(repo_tools.os, "open", tracking_open)
    outcome = tools.search_text(SearchTextArgs("needle"), lambda: True)
    assert "interrupted" in _err(outcome, ErrorCode.TOOL_EXECUTION_FAILED)
    assert opened == []


# --- authenticated chains (R37) and nonce allocation (R40) ------------------------


def _two_page_tools(tmp_path, nonce_source=None):
    _tree(tmp_path, {"a.py": "hit\nhit\nhit\n"})
    return RepoTools(tmp_path, nonce_source=nonce_source)


def test_full_chain_is_the_only_completeness_claim(tmp_path):
    tools = _two_page_tools(tmp_path)
    pages = _search_all(tools, "hit", limit=1)
    assert [p["search_complete"] for p in pages] == [False, False, True]


def _reject(tools, args, reason):
    assert tools.check_search_cursor(args) == reason
    msg = _err(tools.search_text(args), ErrorCode.TOOL_INPUT_INVALID)
    assert reason in msg


def test_forged_nonce_is_unknown(tmp_path):
    tools = _two_page_tools(tmp_path)
    _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    _reject(tools, SearchTextArgs("hit", cursor=encode_search_cursor("f" * 32, 1), limit=1), "chain_unknown")


def test_replay_branch_and_skip_are_not_current(tmp_path):
    tools = _two_page_tools(tmp_path)
    first = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    c1 = first["next_cursor"]
    second = _ok(tools.search_text(SearchTextArgs("hit", cursor=c1, limit=1)))
    _reject(tools, SearchTextArgs("hit", cursor=c1, limit=1), "cursor_not_current")  # replay / branch
    nonce, page = tool_schema.decode_search_cursor(second["next_cursor"])
    skipped = encode_search_cursor(nonce, page + 1)
    _reject(tools, SearchTextArgs("hit", cursor=skipped, limit=1), "cursor_not_current")
    last = _ok(tools.search_text(SearchTextArgs("hit", cursor=second["next_cursor"], limit=1)))
    assert last["search_complete"]


@pytest.mark.parametrize("change", [{"query": "hi"}, {"limit": 2}, {"path": "."}])
def test_changed_arguments_are_a_mismatch(tmp_path, change):
    _tree(tmp_path, {"sub/a.py": "hit\nhit\nhit\n"})
    tools = RepoTools(tmp_path)
    first = _ok(tools.search_text(SearchTextArgs("hit", path="sub", limit=1)))
    kw = {"query": "hit", "path": "sub", "limit": 1, **change}
    _reject(tools, SearchTextArgs(cursor=first["next_cursor"], **kw), "args_mismatch")


def test_finished_and_evicted_chains_are_unknown(tmp_path):
    tools = _two_page_tools(tmp_path)
    pages = _search_all(tools, "hit", limit=1)
    _reject(tools, SearchTextArgs("hit", cursor=pages[1]["next_cursor"], limit=1), "chain_unknown")
    first = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    for _ in range(repo_tools.MAX_LIVE_CHAINS):
        _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    _reject(tools, SearchTextArgs("hit", cursor=first["next_cursor"], limit=1), "chain_unknown")


def test_cursor_checks_touch_no_filesystem(tmp_path, monkeypatch):
    tools = _two_page_tools(tmp_path)
    first = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    _ok(tools.search_text(SearchTextArgs("hit", cursor=first["next_cursor"], limit=1)))

    def forbidden(*a, **k):
        raise AssertionError("filesystem touched")

    monkeypatch.setattr(repo_tools.os, "open", forbidden)
    _reject(tools, SearchTextArgs("hit", cursor=first["next_cursor"], limit=1), "cursor_not_current")


def test_coverage_gap_on_a_middle_page_blocks_completeness(tmp_path):
    _tree(tmp_path, {"a.py": "hit\n", "b.py": b"hit\x00", "c.py": "hit\n"})
    pages = _search_all(RepoTools(tmp_path), "hit", limit=1)
    assert not pages[-1]["search_complete"]
    assert any(not p["page_coverage_complete"] for p in pages[:-1])


def test_colliding_nonce_never_overwrites_a_live_chain(tmp_path):
    a, b = "a" * 32, "b" * 32
    tools = _two_page_tools(tmp_path, _nonces(a, a, b))
    first = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    live_before = dict(vars(tools._chains._live[a]))
    second = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    assert tool_schema.decode_search_cursor(second["next_cursor"])[0] == b
    assert vars(tools._chains._live[a]) == live_before
    assert tools.check_search_cursor(SearchTextArgs("hit", cursor=first["next_cursor"], limit=1)) is None


@pytest.mark.parametrize("how", ["finished", "evicted"])
def test_retired_nonce_is_never_reissued(tmp_path, how):
    a = "a" * 32
    fresh = iter(f"{i:032x}" for i in range(1, 200))
    seq = [a]
    tools = _two_page_tools(tmp_path, lambda: seq.pop(0) if seq else next(fresh))
    first = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    if how == "finished":
        c = first["next_cursor"]
        while c is not None:
            c = _ok(tools.search_text(SearchTextArgs("hit", cursor=c, limit=1)))["next_cursor"]
    else:
        for _ in range(repo_tools.MAX_LIVE_CHAINS):
            _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    seq.append(a)  # the source now offers the retired nonce again
    again = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    assert tool_schema.decode_search_cursor(again["next_cursor"])[0] != a
    _reject(tools, SearchTextArgs("hit", cursor=first["next_cursor"], limit=1), "chain_unknown")


def test_malformed_and_raising_nonces_count_as_attempts(tmp_path):
    good = "c" * 32
    values = iter(["c" * 31, "C" * 32, "g" * 32, 42])

    def source():
        try:
            return next(values)
        except StopIteration:
            return good

    raising = {"n": 0}

    def flaky():
        raising["n"] += 1
        if raising["n"] == 1:
            raise RuntimeError("source failed")
        return source()

    tools = _two_page_tools(tmp_path, flaky)
    page = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    assert tool_schema.decode_search_cursor(page["next_cursor"])[0] == good


def test_nonce_exhaustion_fails_closed_and_changes_nothing(tmp_path):
    fresh = iter(f"{i:032x}" for i in range(1, 100))
    mode = {"collide": False}

    def source():
        return "0" * 31 + "1" if mode["collide"] else next(fresh)

    tools = _two_page_tools(tmp_path, source)
    cursors = [_ok(tools.search_text(SearchTextArgs("hit", limit=1)))["next_cursor"] for _ in range(64)]
    snapshot = ({k: dict(vars(v)) for k, v in tools._chains._live.items()}, set(tools._chains._issued))
    mode["collide"] = True
    msg = _err(tools.search_text(SearchTextArgs("hit", limit=1)), ErrorCode.TOOL_EXECUTION_FAILED)
    assert msg == "search chain allocation failed"
    assert ({k: dict(vars(v)) for k, v in tools._chains._live.items()}, set(tools._chains._issued)) == snapshot
    for c in cursors:
        assert tools.check_search_cursor(SearchTextArgs("hit", cursor=c, limit=1)) is None


def test_collision_on_the_last_attempt_still_succeeds(tmp_path):
    a = "a" * 32
    tools = _two_page_tools(tmp_path, _nonces(a, *([a] * 7), "d" * 32))
    _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    page = _ok(tools.search_text(SearchTextArgs("hit", limit=1)))
    assert tool_schema.decode_search_cursor(page["next_cursor"])[0] == "d" * 32


def test_single_page_search_allocates_nothing(tmp_path):
    def source():
        raise AssertionError("nonce source called")

    _tree(tmp_path, {"a.py": "hit\n"})
    page = _ok(RepoTools(tmp_path, nonce_source=source).search_text(SearchTextArgs("hit")))
    assert page["search_complete"] and page["next_cursor"] is None


def test_interrupting_nonce_source_installs_nothing(tmp_path):
    def source():
        raise KeyboardInterrupt()

    tools = _two_page_tools(tmp_path, source)
    before = _open_fds()
    with pytest.raises(KeyboardInterrupt):
        tools.search_text(SearchTextArgs("hit", limit=1))
    assert not tools._chains._live and not tools._chains._issued
    assert _open_fds() == before


# --- descriptor ownership and cleanup dominance (R22) ------------------------------


def _calls(tmp_path):
    _tree(tmp_path, {"d/a.py": "needle\n", "d/b.py": "needle\n"})
    tools = RepoTools(tmp_path)
    return tools, [
        lambda stop=repo_tools._never: tools.read_file(ReadFileArgs("d/a.py"), stop),
        lambda stop=repo_tools._never: tools.list_directory(ListDirectoryArgs("d"), stop),
        lambda stop=repo_tools._never: tools.search_text(SearchTextArgs("needle"), stop),
    ]


def test_no_descriptor_survives_any_call(tmp_path):
    tools, calls = _calls(tmp_path)
    before = _open_fds()
    for call in calls:
        call()
    _err(tools.read_file(ReadFileArgs("d/missing.py")), ErrorCode.TOOL_INPUT_INVALID)
    for call in (calls[0], calls[2]):  # read and search consult should_stop on every call
        call(lambda: True)

        def boom():
            raise RuntimeError("unexpected")

        with pytest.raises(RuntimeError):
            call(boom)

        def interrupt():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            call(interrupt)
    assert _open_fds() == before


class _CloseFault:
    """Really closes every descriptor (so nothing leaks) but reports the
    first `fail` closes as failed."""

    def __init__(self, fail: int = 1) -> None:
        self.fail = fail
        self.calls = 0
        self.raised: list[OSError] = []

    def __call__(self, fd: int) -> None:
        self.calls += 1
        os.close(fd)
        if self.fail:
            self.fail -= 1
            exc = OSError("SENTINEL_CLOSE_FAILURE")
            self.raised.append(exc)
            raise exc


def test_close_failure_on_a_normal_result_dominates(tmp_path, monkeypatch):
    tools, calls = _calls(tmp_path)
    for call in calls:
        fault = _CloseFault()
        monkeypatch.setattr(repo_tools, "_close", fault)
        before = _open_fds()
        with pytest.raises(ToolCleanupUnconfirmedError) as info:
            call()
        assert info.value.close_failures == tuple(fault.raised)
        assert info.value.body_failure is None and info.value.__cause__ is None
        assert "SENTINEL" not in str(info.value)
        assert _open_fds() == before


@pytest.mark.parametrize("body_exc", [RuntimeError("body"), KeyboardInterrupt()])
def test_close_failure_with_a_body_failure_keeps_it_as_cause(tmp_path, monkeypatch, body_exc):
    _tree(tmp_path, {"a.py": "x\n"})  # root-level: no in-call release precedes the body failure
    tools = RepoTools(tmp_path)

    def stop():
        raise body_exc

    fault = _CloseFault()
    monkeypatch.setattr(repo_tools, "_close", fault)
    try:
        tools.read_file(ReadFileArgs("a.py"), stop)
    except ToolCleanupUnconfirmedError as exc:
        caught = exc
    except BaseException as exc:  # noqa: BLE001 - an escape must fail, not abort, the session
        pytest.fail(f"cleanup dominance violated: {type(exc).__name__} escaped")
    else:
        pytest.fail("no ToolCleanupUnconfirmedError")
    assert caught.body_failure is body_exc and caught.__cause__ is body_exc


def test_every_close_is_attempted_after_a_failure(tmp_path, monkeypatch):
    _tree(tmp_path, {"a/b/c/x.py": "x\n"})
    tools = RepoTools(tmp_path)
    fault = _CloseFault(fail=10)
    opened = []
    real_open = os.open

    def tracking_open(*a, **k):
        fd = real_open(*a, **k)
        opened.append(fd)
        return fd

    monkeypatch.setattr(repo_tools.os, "open", tracking_open)
    monkeypatch.setattr(repo_tools, "_close", fault)
    with pytest.raises(ToolCleanupUnconfirmedError):
        tools.read_file(ReadFileArgs("a/b/c/x.py"))
    # The first in-call release fails and unwinds; __exit__ still closes
    # every remaining descriptor, so every opened descriptor gets a close.
    assert fault.calls == len(opened)


def test_exit_runs_once(tmp_path, monkeypatch):
    tools = RepoTools(tmp_path)
    fault = _CloseFault(fail=0)
    monkeypatch.setattr(repo_tools, "_close", fault)
    scope = repo_tools._CallDescriptors(tools)
    scope.__enter__()
    assert scope.__exit__(None, None, None) is False
    assert scope.__exit__(None, None, None) is False
    assert fault.calls == 1


def test_root_identity_failure_closes_the_root(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    tools = RepoTools(root)
    root.rename(tmp_path / "old")
    root.mkdir()
    fault = _CloseFault()
    monkeypatch.setattr(repo_tools, "_close", fault)
    with pytest.raises(ToolCleanupUnconfirmedError):
        tools.read_file(ReadFileArgs("a.py"))
    assert fault.calls == 1


# --- module boundaries (R17) ----------------------------------------------------------


def test_repo_tools_imports_no_controller_cancellation_lifecycle_or_subprocess():
    tree = ast.parse((SRC / "repo_tools.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    assert {n for n in names if n.startswith("codeagent")} == {"codeagent.errors", "codeagent.tool_schema"}
    assert "subprocess" not in names


def test_payload_examples_are_valid_canonical_json(tmp_path):
    tools = RepoTools(_multi_tree(tmp_path))
    for outcome in (
        tools.read_file(ReadFileArgs("a/one.py")),
        tools.list_directory(ListDirectoryArgs("a")),
        tools.search_text(SearchTextArgs("hit")),
    ):
        raw = canonical_json_bytes(_ok(outcome))
        assert json.loads(raw) == outcome.payload


def test_fresh_interpreter_imports_succeed_in_both_orders():
    for order in (["repo_tools", "controller"], ["controller", "repo_tools"]):
        code = "; ".join(f"import codeagent.{m}" for m in order + ["tool_schema"])
        subprocess.run([sys.executable, "-c", code], check=True)


# --- review fix 1: root-descriptor ownership in __enter__ ---------------------


class _RootFault:
    """Arms one failure of the first fstat after the root open."""

    def __init__(self, monkeypatch, exc):
        self.exc, self.root, self.armed = exc, None, True
        real_open, real_fstat = os.open, os.fstat

        def tracking_open(path, *a, **k):
            fd = real_open(path, *a, **k)
            if self.root is None and not k.get("dir_fd"):
                self.root = fd
            return fd

        def failing_fstat(fd):
            if self.armed and fd == self.root:
                self.armed = False
                raise self.exc
            return real_fstat(fd)

        monkeypatch.setattr(repo_tools.os, "open", tracking_open)
        monkeypatch.setattr(repo_tools.os, "fstat", failing_fstat)


class _RecordingClose:
    def __init__(self, fail: bool):
        self.fail, self.closed, self.raised = fail, [], []

    def __call__(self, fd):
        self.closed.append(fd)
        os.close(fd)  # really closed: nothing leaks
        if self.fail:
            exc = OSError("SENTINEL_CLOSE")
            self.raised.append(exc)
            raise exc


BODIES = {
    "mapped_oserror": lambda: OSError("inspect failed"),
    "unexpected": lambda: RuntimeError("unexpected"),
    "keyboard_interrupt": KeyboardInterrupt,
    "system_exit": SystemExit,
}


def _call_catching(tools):
    try:
        return tools.read_file(ReadFileArgs("a.py")), None
    except BaseException as exc:  # noqa: BLE001 - an escape must be inspected, not abort the session
        return None, exc


@pytest.mark.parametrize("body", [*BODIES, "identity_mismatch"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_root_descriptor_is_closed_exactly_once_on_every_entry_failure(tmp_path, monkeypatch, body, close_fails):
    _tree(tmp_path, {"a.py": "x\n"})
    tools = RepoTools(tmp_path)
    before = _open_fds()
    fault = None
    if body == "identity_mismatch":
        tools._identity = (-1, -1)
        root = {}
        real_open = os.open

        def tracking_open(path, *a, **k):
            fd = real_open(path, *a, **k)
            root.setdefault("fd", fd)
            return fd

        monkeypatch.setattr(repo_tools.os, "open", tracking_open)
    else:
        fault = _RootFault(monkeypatch, BODIES[body]())
    closer = _RecordingClose(close_fails)
    monkeypatch.setattr(repo_tools, "_close", closer)
    outcome, escaped = _call_catching(tools)
    root_fd = root["fd"] if fault is None else fault.root
    assert closer.closed == [root_fd]
    assert _open_fds() == before
    original = fault.exc if fault is not None else None
    if close_fails:
        assert isinstance(escaped, ToolCleanupUnconfirmedError)
        assert escaped.close_failures == tuple(closer.raised)
        assert escaped.body_failure is not None and escaped.__cause__ is escaped.body_failure
        if body in ("unexpected", "keyboard_interrupt", "system_exit"):
            assert escaped.body_failure is original
        else:  # the mapped inspection or identity failure, never None
            assert isinstance(escaped.body_failure, repo_tools._ToolFailure)
            if body == "mapped_oserror":
                assert escaped.body_failure.__cause__ is None  # mapped `from None`
    elif body in ("unexpected", "keyboard_interrupt", "system_exit"):
        assert escaped is original
    else:
        assert escaped is None
        message = _err(outcome, ErrorCode.TOOL_EXECUTION_FAILED)
        assert message == ("worktree root changed" if body == "identity_mismatch" else "worktree root could not be inspected")


def test_successful_entry_registers_the_root_once(tmp_path, monkeypatch):
    _tree(tmp_path, {"a.py": "x\n"})
    closer = _RecordingClose(False)
    monkeypatch.setattr(repo_tools, "_close", closer)
    _ok(RepoTools(tmp_path).read_file(ReadFileArgs("a.py")))
    assert len(closer.closed) == len(set(closer.closed)) == 2  # root and file, each once


# --- review fix 2: the scan cap counts bytes actually read ---------------------


def _underreport(monkeypatch, name: bytes, size: int):
    real = os.lstat

    def lstat(path, *a, **k):
        st = real(path, *a, **k)
        if path == name:
            fields = list(st[:10])
            fields[6] = size
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(repo_tools.os, "lstat", lstat)


def _paged_reads(tools, monkeypatch, query):
    """Run a full chain, recording the bytes actually read on each page."""
    per_page, current = [], [0]
    real_read = repo_tools._read

    def counting_read(fd, n):
        data = real_read(fd, n)
        current[0] += len(data)
        return data

    monkeypatch.setattr(repo_tools, "_read", counting_read)
    pages, cursor = [], None
    while True:
        assert len(pages) < 50
        current[0] = 0
        page = _ok(tools.search_text(SearchTextArgs(query, cursor=cursor, limit=100)))
        per_page.append(current[0])
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages, per_page


@pytest.mark.parametrize("b_size", [70, 40])  # larger than, and exactly equal to, the remaining budget
def test_a_file_growing_after_enumeration_never_breaks_the_scan_cap(tmp_path, monkeypatch, b_size):
    a = "hit\n" * 15  # 60 bytes
    b = ("hit\n" * 20)[:b_size]
    _tree(tmp_path, {"a.py": a, "b.py": b})
    expected = _ok(RepoTools(tmp_path).search_text(SearchTextArgs("hit", limit=100)))["results"]
    monkeypatch.setattr(repo_tools, "SEARCH_SCAN_CAP_BYTES", 100)
    _underreport(monkeypatch, b"b.py", 1)  # enumeration sees 1 byte; the open file supplies more
    pages, per_page = _paged_reads(RepoTools(tmp_path), monkeypatch, "hit")
    assert all(n <= 100 for n in per_page), per_page
    assert per_page[0] == 100  # 60 + the 40 the budget allowed, not one byte more
    assert {r["path"] for r in pages[0]["results"]} == {"a.py"}  # nothing from the partial file
    flat = [r for p in pages for r in p["results"]]
    assert flat == expected and len({(r["path"], r["line"]) for r in flat}) == len(flat)
    assert pages[-1]["search_complete"]


def test_a_scan_budget_smaller_than_one_file_fails_closed(tmp_path, monkeypatch):
    _tree(tmp_path, {"a.py": "hit\n" * 10})
    monkeypatch.setattr(repo_tools, "SEARCH_SCAN_CAP_BYTES", 10)
    msg = _err(RepoTools(tmp_path).search_text(SearchTextArgs("hit")), ErrorCode.TOOL_EXECUTION_FAILED)
    assert msg == "search scan budget is smaller than one file"


def test_production_scan_cap_always_admits_one_whole_file():
    assert repo_tools.SEARCH_SCAN_CAP_BYTES > repo_tools.SEARCH_FILE_MAX_BYTES + 1


# --- review fix 3: registration stays inside the ownership guard ---------------


class _RaisingAppend(list):
    """`append` raises; with `after=True` it registers the fd first."""

    def __init__(self, exc, after=False):
        super().__init__()
        self.exc, self.after = exc, after

    def append(self, item):
        if self.after:
            super().append(item)
        raise self.exc


class _RootSetterFails(repo_tools._CallDescriptors):
    """Fails after `_open.append` succeeded: assigning `root` raises."""

    exc: BaseException = RuntimeError("root assignment")

    def __setattr__(self, name, value):
        if name == "root" and value != -1 and getattr(self, "_armed", False):
            raise self.exc
        object.__setattr__(self, name, value)


REGISTRATION_FAILURES = {
    "ordinary": lambda: RuntimeError("registration"),
    "memory_error": MemoryError,
    "keyboard_interrupt": KeyboardInterrupt,
    "system_exit": SystemExit,
}


def _enter_catching(scope):
    try:
        scope.__enter__()
    except BaseException as exc:  # noqa: BLE001 - inspected, never allowed to abort the session
        return exc
    pytest.fail("__enter__ did not fail")


def _check_registration_failure(tmp_path, monkeypatch, scope, original, close_fails):
    closer = _RecordingClose(close_fails)
    monkeypatch.setattr(repo_tools, "_close", closer)
    before = _open_fds()
    escaped = _enter_catching(scope)
    assert len(closer.closed) == 1  # the root, exactly once
    assert scope.__exit__(None, None, None) is False  # no later double-close
    assert len(closer.closed) == 1
    assert _open_fds() == before
    if close_fails:
        assert isinstance(escaped, ToolCleanupUnconfirmedError)
        assert escaped.body_failure is original and escaped.__cause__ is original
        assert escaped.close_failures == tuple(closer.raised)
        assert str(escaped) == "repository tool descriptor cleanup unconfirmed"
    else:
        assert escaped is original


@pytest.mark.parametrize("kind", list(REGISTRATION_FAILURES))
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("registered_first", [False, True])
def test_registration_failure_closes_the_root_exactly_once(tmp_path, monkeypatch, kind, close_fails, registered_first):
    tools = RepoTools(tmp_path)
    original = REGISTRATION_FAILURES[kind]()
    scope = repo_tools._CallDescriptors(tools)
    scope._open = _RaisingAppend(original, after=registered_first)
    _check_registration_failure(tmp_path, monkeypatch, scope, original, close_fails)


@pytest.mark.parametrize("kind", list(REGISTRATION_FAILURES))
@pytest.mark.parametrize("close_fails", [False, True])
def test_failure_after_registration_closes_the_root_exactly_once(tmp_path, monkeypatch, kind, close_fails):
    tools = RepoTools(tmp_path)
    original = REGISTRATION_FAILURES[kind]()
    scope = _RootSetterFails(tools)
    scope.exc = original
    scope._armed = True
    _check_registration_failure(tmp_path, monkeypatch, scope, original, close_fails)


def test_registration_failure_through_a_tool_call_leaks_nothing(tmp_path, monkeypatch):
    _tree(tmp_path, {"a.py": "x\n"})
    tools = RepoTools(tmp_path)
    original = MemoryError()
    real_init = repo_tools._CallDescriptors.__init__

    def init(self, t):
        real_init(self, t)
        self._open = _RaisingAppend(original)

    monkeypatch.setattr(repo_tools._CallDescriptors, "__init__", init)
    before = _open_fds()
    outcome, escaped = _call_catching(tools)
    assert outcome is None and escaped is original
    assert _open_fds() == before


# --- ledger D4b (U16): replacements for the retired test_reader.py -----------


def test_read_file_absolute_path_is_refused_before_any_read(tmp_path):
    (tmp_path / "secret").write_text("HOST_SECRET\n")
    with pytest.raises(ToolArgumentError) as info:
        parse_tool_arguments(ToolName.READ_FILE, json.dumps({"path": str(tmp_path / "secret")}).encode())
    assert info.value.reason is ArgumentRejection.PATH_ABSOLUTE
    assert "absolute" in str(info.value) and "HOST_SECRET" not in str(info.value)


def test_read_file_messages_never_contain_the_worktree_path(tmp_path):
    _tree(tmp_path, {"bad.py": b"\xff\xfe", "d/x.py": "x\n"})
    os.symlink(tmp_path / "d/x.py", tmp_path / "l.py")
    tools = RepoTools(tmp_path)
    for rel in ("missing.py", "bad.py", "l.py", "d/x.py/y", "nope/x.py"):
        outcome = tools.read_file(ReadFileArgs(rel))
        assert not outcome.ok
        message = outcome.error.message
        assert str(tmp_path) not in message and str(tmp_path.resolve()) not in message


def test_each_read_failure_gets_a_distinct_error_id(tmp_path):
    tools = RepoTools(tmp_path)
    ids = {tools.read_file(ReadFileArgs("missing.py")).error.error_id for _ in range(5)}
    assert len(ids) == 5
