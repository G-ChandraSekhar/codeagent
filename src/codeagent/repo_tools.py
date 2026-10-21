"""Read-only, descriptor-relative repository toolkit (ledger D4a; D4 plan
revisions 5 and 6, §5).

`RepoTools` serves `list_directory`, `read_file` and `search_text` over one
worktree. It holds no file descriptor: every call opens the worktree root
with `O_NOFOLLOW`, checks it is the directory recorded at construction, and
walks each path component with `openat(..., O_NOFOLLOW)`, so a symlink
anywhere along the path is refused rather than followed and a component
swapped mid-walk cannot redirect the read (this closes the TOCTOU race the
deleted `reader.py` documented). The Git administrative entry is denied by
name (`tool_schema.check_relative_path`) and by inode identity, which also
catches case variants and hard-link aliases.

Every descriptor a call opens is owned by one `_CallDescriptors` scope and
closed on every path. If any close cannot be confirmed, cleanup dominates:
the call raises `tool_schema.ToolCleanupUnconfirmedError` and its result is
discarded (§5.7).

`search_text` continuation is an authenticated chain (`SearchChains`): the
cursor is a single-use capability naming a server-held position, so only an
unbroken chain begun with `cursor=None` can report `search_complete=True`.

No subprocess is launched and no durable state is written.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codeagent.errors import ErrorCode, OperationalError
from codeagent.tool_schema import (
    MAX_CURSOR_BYTES,
    MAX_PATH_DEPTH,
    ListDirectoryArgs,
    ReadFileArgs,
    SearchTextArgs,
    ToolArgumentError,
    ToolCleanupUnconfirmedError,
    ToolOutcome,
    b64url,
    canonical_json_bytes,
    check_relative_path,
    decode_list_cursor,
    decode_search_cursor,
    encode_list_cursor,
    encode_search_cursor,
    is_administrative_name,
    safe_path_fragment,
)

PAYLOAD_CAP_BYTES = 80 * 1024
READ_CONTENT_CAP_BYTES = 64 * 1024
READ_FILE_MAX_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
DIRECTORY_MAX_ENTRIES = 10_000
DIRECTORY_MAX_NAME_BYTES = 1024 * 1024
STOP_CHECK_INTERVAL = 256
SEARCH_FILE_MAX_BYTES = 1024 * 1024
SEARCH_BINARY_PROBE_BYTES = 8 * 1024
SEARCH_SCAN_CAP_BYTES = 16 * 1024 * 1024
SEARCH_ENTRY_CAP = 50_000
SEARCH_STOP_CHUNK_BYTES = 1024 * 1024
SNIPPET_MAX_CHARS = 240
MAX_LIVE_CHAINS = 64
NONCE_ATTEMPTS = 8
_COUNTER_RESERVE = 10**9 - 1
_REQUIRED_FLAGS = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)

# Indirection points for fault-injection and read-size tests.
_close = os.close
_read = os.read

_INTERRUPTED = "tool interrupted by cancellation or deadline"


def _never() -> bool:
    return False


class _ToolFailure(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _input(message: str) -> _ToolFailure:
    return _ToolFailure(ErrorCode.TOOL_INPUT_INVALID, message)


def _execution(message: str) -> _ToolFailure:
    return _ToolFailure(ErrorCode.TOOL_EXECUTION_FAILED, message)


class _CloseFailed(Exception):
    """Internal: an in-call release failed; unwinds to `_CallDescriptors`."""


class _Interrupted(Exception):
    """Internal: `should_stop()` returned true."""


class _StopPage(Exception):
    """Internal: a per-call cap stopped the search page."""


def _fstat(fd: int) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError:
        raise _execution("an open entry could not be inspected") from None


class _CallDescriptors:
    """Owns every descriptor one tool call opens (§5.7). `__exit__` closes
    every still-open descriptor in reverse order, attempting all of them and
    keeping each failure by identity; it runs once. Any unconfirmed close
    raises `ToolCleanupUnconfirmedError`, with the body failure (an ordinary
    exception or an interrupt) kept as `body_failure` and `__cause__`."""

    def __init__(self, tools: RepoTools) -> None:
        self._tools = tools
        self._open: list[int] = []
        self._failures: list[BaseException] = []
        self._exited = False
        self.root = -1

    def __enter__(self) -> _CallDescriptors:
        try:
            fd = os.open(self._tools._path, _DIR_FLAGS)
        except OSError:
            raise _execution("worktree root could not be opened") from None
        # One ownership guard covers inspection, registration, root
        # assignment and the successful return: any failure after `os.open`
        # returns closes the root exactly once. The only unguarded instant is
        # between `os.open` returning and Python entering this `try`, which
        # an asynchronous interrupt could still hit.
        try:
            try:
                st = os.fstat(fd)
            except OSError:
                raise _execution("worktree root could not be inspected") from None
            if (st.st_dev, st.st_ino) != self._tools._identity:
                raise _execution("worktree root changed")
            self._open.append(fd)
            self.root = fd
            return self
        except BaseException as body:
            # Drop any partial registration so no later `__exit__` closes it
            # again; nothing else is registered during entry.
            self._open = []
            self.root = -1
            self._exited = True
            try:
                _close(fd)
            except BaseException as close_exc:  # noqa: BLE001
                raise ToolCleanupUnconfirmedError((close_exc,), body) from body
            raise

    def openat(self, dir_fd: int, name: str | bytes, flags: int) -> int:
        fd = os.open(name, flags, dir_fd=dir_fd)
        self._open.append(fd)
        return fd

    def release(self, fd: int) -> None:
        self._open.remove(fd)
        try:
            _close(fd)
        except BaseException as exc:  # noqa: BLE001 - every close failure is retained
            self._failures.append(exc)
            raise _CloseFailed() from None

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if self._exited:
            return False
        self._exited = True
        for fd in reversed(self._open):
            try:
                _close(fd)
            except BaseException as close_exc:  # noqa: BLE001
                self._failures.append(close_exc)
        self._open.clear()
        if not self._failures:
            return False
        body = None if exc is None or isinstance(exc, _CloseFailed) else exc
        error = ToolCleanupUnconfirmedError(tuple(self._failures), body)
        if body is not None:
            raise error from body
        raise error from None


@dataclass
class _Chain:
    key: str
    page: int
    expected_cursor: str
    position: tuple[tuple[bytes, ...], int | None]
    coverage_ok: bool


class SearchChains:
    """Server-held `search_text` continuation state (§5.6, §5.6a).

    A cursor is accepted only if it is byte-identical to the one cursor the
    chain last issued and the call's normalized arguments match. Nonces are
    unique across every chain this instance ever installed (live, finished
    or evicted) and are allocated before anything is installed or evicted;
    exhaustion leaves the registry untouched."""

    def __init__(self, nonce_source: Callable[[], str]) -> None:
        self._nonce_source = nonce_source
        self._live: OrderedDict[str, _Chain] = OrderedDict()
        self._issued: set[str] = set()

    def rejection(self, args: SearchTextArgs) -> str | None:
        if args.cursor is None:
            return None
        nonce, _page = decode_search_cursor(args.cursor)
        chain = self._live.get(nonce)
        if chain is None:
            return "chain_unknown"
        if chain.key != args.chain_key:
            return "args_mismatch"
        if chain.expected_cursor != args.cursor:
            return "cursor_not_current"
        return None

    def current(self, args: SearchTextArgs) -> tuple[str, _Chain]:
        nonce, _page = decode_search_cursor(args.cursor)
        return nonce, self._live[nonce]

    def finish(self, nonce: str) -> None:
        self._live.pop(nonce, None)

    def allocate(self) -> str | None:
        for _ in range(NONCE_ATTEMPTS):
            try:
                candidate = self._nonce_source()
            except Exception:  # noqa: BLE001 - a failing source counts as an attempt
                continue
            if (
                isinstance(candidate, str)
                and len(candidate) == 32
                and all(ch in "0123456789abcdef" for ch in candidate)
                and candidate not in self._issued
            ):
                return candidate
        return None

    def install(self, nonce: str, chain: _Chain) -> None:
        self._issued.add(nonce)
        if len(self._live) >= MAX_LIVE_CHAINS:
            self._live.popitem(last=False)
        self._live[nonce] = chain


def _default_nonce() -> str:
    return secrets.token_hex(16)


def _size(value: Any) -> int:
    return len(canonical_json_bytes(value))


class _Page:
    """Exact projected-size accounting for one list-valued payload field:
    `reserve` is the canonical size of the envelope with that list empty and
    every scalar (cursor included) at its maximum; n items add their sizes
    plus n-1 separators. An item is added only if the projection still fits."""

    def __init__(self, reserve: int) -> None:
        self.items: list[Any] = []
        self.projected = reserve

    def fits(self, item: Any) -> bool:
        return self.projected + _size(item) + (1 if self.items else 0) <= PAYLOAD_CAP_BYTES

    def add(self, item: Any) -> None:
        self.projected += _size(item) + (1 if self.items else 0)
        self.items.append(item)


_MAX_CURSOR = "x" * MAX_CURSOR_BYTES
_EXCLUDED_KEYS = ("administrative", "non_regular", "symlinks", "vanished")
_UNSEARCHED_KEYS = (
    "binary_files",
    "non_utf8_files",
    "non_utf8_names",
    "oversized_directories",
    "oversized_files",
    "too_deep",
    "unaddressable_names",
    "unreadable",
)
LIST_RESERVE = _size({"entries": [], "next_cursor": _MAX_CURSOR, "page_truncated": False})
READ_RESERVE = _size({"eof": False, "lines": [], "next_start_line": 10**9, "page_truncated": False})
SEARCH_RESERVE = _size(
    {
        "excluded": dict.fromkeys(_EXCLUDED_KEYS, _COUNTER_RESERVE),
        "next_cursor": _MAX_CURSOR,
        "page_coverage_complete": False,
        "page_truncated": False,
        "results": [],
        "search_complete": False,
        "unsearched": dict.fromkeys(_UNSEARCHED_KEYS, _COUNTER_RESERVE),
    }
)

# One enumerated directory entry: raw name, UTF-8 name or None, lstat or
# None (vanished), and whether it is the Git administrative entry.
_Entry = tuple[bytes, "str | None", "os.stat_result | None", bool]


class RepoTools:
    """Read-only repository tools over one worktree directory."""

    def __init__(
        self, worktree_path: str | os.PathLike[str], *, nonce_source: Callable[[], str] | None = None
    ) -> None:
        if not all(hasattr(os, flag) for flag in _REQUIRED_FLAGS):
            raise ValueError("this platform lacks a descriptor flag the repository tools require")
        path = os.fspath(worktree_path)
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            raise ValueError("worktree_path must be an existing directory") from None
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError("worktree_path must be an existing directory, not a symlink or file")
        self._path = path
        self._identity = (st.st_dev, st.st_ino)
        self._chains = SearchChains(nonce_source or _default_nonce)
        self._error_seq = 0

    # -- shared machinery ------------------------------------------------

    def _failure(self, code: ErrorCode, message: str) -> ToolOutcome:
        self._error_seq += 1
        return ToolOutcome(
            ok=False,
            payload=None,
            summary="",
            error=OperationalError(code=code, error_id=f"repo-tools-err-{self._error_seq}", message=message),
        )

    def _run(self, body: Callable[[_CallDescriptors], ToolOutcome]) -> ToolOutcome:
        try:
            with _CallDescriptors(self) as ctx:
                try:
                    return body(ctx)
                except _ToolFailure as failure:
                    outcome_failure = failure
                except _Interrupted:
                    outcome_failure = _execution(_INTERRUPTED)
        except _ToolFailure as failure:  # raised while entering the scope
            outcome_failure = failure
        return self._failure(outcome_failure.code, outcome_failure.message)

    @staticmethod
    def _admin_identity(ctx: _CallDescriptors) -> tuple[int, int] | None:
        try:
            st = os.lstat(".git", dir_fd=ctx.root)
        except FileNotFoundError:
            return None
        except OSError:
            raise _execution("the Git administrative entry could not be inspected") from None
        return (st.st_dev, st.st_ino)

    @staticmethod
    def _classify_open_failure(dir_fd: int, name: str, frag: str, *, want_dir: bool) -> _ToolFailure:
        try:
            st = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return _input(f"path does not exist: {frag!r}")
        except OSError:
            return _execution(f"path could not be inspected: {frag!r}")
        if stat.S_ISLNK(st.st_mode):
            return _input(f"path escapes through a symlink: {frag!r}")
        if want_dir and not stat.S_ISDIR(st.st_mode):
            return _input(f"path component is not a directory: {frag!r}")
        return _execution(f"path could not be opened: {frag!r}")

    def _open_dirs(
        self, ctx: _CallDescriptors, components: tuple[str, ...], admin: tuple[int, int] | None, frag: str
    ) -> int:
        """Open `components` as a directory chain from the root and return
        the last descriptor (the root itself for `()`). Each component is
        opened relative to its parent with O_NOFOLLOW; intermediates are
        released as the walk advances."""
        current = ctx.root
        for component in components:
            try:
                fd = ctx.openat(current, component, _DIR_FLAGS)
            except OSError:
                raise self._classify_open_failure(current, component, frag, want_dir=True) from None
            if current != ctx.root:
                ctx.release(current)
            current = fd
            st = _fstat(fd)
            if admin is not None and (st.st_dev, st.st_ino) == admin:
                raise _input(f"path names the Git administrative entry: {frag!r}")
        return current

    def _open_file(
        self, ctx: _CallDescriptors, components: tuple[str, ...], admin: tuple[int, int] | None, frag: str
    ) -> tuple[int, os.stat_result]:
        parent = self._open_dirs(ctx, components[:-1], admin, frag)
        try:
            fd = ctx.openat(parent, components[-1], _FILE_FLAGS)
        except OSError:
            raise self._classify_open_failure(parent, components[-1], frag, want_dir=False) from None
        if parent != ctx.root:
            ctx.release(parent)
        st = _fstat(fd)
        if admin is not None and (st.st_dev, st.st_ino) == admin:
            raise _input(f"path names the Git administrative entry: {frag!r}")
        if not stat.S_ISREG(st.st_mode):
            raise _input(f"path is not a regular file: {frag!r}")
        return fd, st

    @staticmethod
    def _read_once(fd: int, size: int) -> bytes:
        while True:
            try:
                return _read(fd, size)
            except InterruptedError:
                continue
            except OSError:
                raise _execution("file could not be read") from None

    @staticmethod
    def _enumerate(dir_fd: int, admin: tuple[int, int] | None, should_stop: Callable[[], bool]) -> list[_Entry] | None:
        """Every entry of one directory, sorted by raw name bytes, or None
        when the directory exceeds the per-directory caps. Enumeration
        itself is bounded: it stops as soon as either cap is passed."""
        entries: list[_Entry] = []
        name_bytes = 0
        seen = 0
        try:
            with os.scandir(dir_fd) as it:
                for entry in it:
                    seen += 1
                    if seen % STOP_CHECK_INTERVAL == 0 and should_stop():
                        raise _Interrupted()
                    raw = os.fsencode(entry.name)
                    name_bytes += len(raw)
                    if seen > DIRECTORY_MAX_ENTRIES or name_bytes > DIRECTORY_MAX_NAME_BYTES:
                        return None
                    try:
                        decoded: str | None = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded = None
                    try:
                        st: os.stat_result | None = os.lstat(raw, dir_fd=dir_fd)
                    except FileNotFoundError:
                        st = None
                    except OSError:
                        raise _execution("a directory entry could not be inspected") from None
                    is_admin = (decoded is not None and is_administrative_name(decoded)) or (
                        st is not None and admin is not None and (st.st_dev, st.st_ino) == admin
                    )
                    entries.append((raw, decoded, st, is_admin))
        except OSError:
            raise _execution("directory could not be listed") from None
        entries.sort(key=lambda e: e[0])
        return entries

    @staticmethod
    def _entry_type(st: os.stat_result) -> str:
        if stat.S_ISLNK(st.st_mode):
            return "symlink"
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        return "other"

    # -- read_file -------------------------------------------------------

    def read_file(self, args: ReadFileArgs, should_stop: Callable[[], bool] = _never) -> ToolOutcome:
        if not isinstance(args, ReadFileArgs):
            raise TypeError("read_file requires ReadFileArgs")
        return self._run(lambda ctx: self._read_file(ctx, args, should_stop))

    def _read_file(self, ctx: _CallDescriptors, args: ReadFileArgs, should_stop: Callable[[], bool]) -> ToolOutcome:
        components = check_relative_path(args.path, allow_root=False)
        frag = safe_path_fragment(args.path)
        admin = self._admin_identity(ctx)
        fd, st = self._open_file(ctx, components, admin, frag)
        if st.st_size > READ_FILE_MAX_BYTES:
            raise _execution(f"file exceeds the {READ_FILE_MAX_BYTES}-byte scan limit: {frag!r}")
        too_long = f"line exceeds the {READ_CONTENT_CAP_BYTES}-byte read limit: {frag!r}"
        page = _Page(READ_RESERVE)
        content_bytes = scanned = line_no = 0
        buf = b""
        eof = truncated = False
        next_start: int | None = None
        while True:
            newline = buf.find(b"\n")
            if newline < 0 and not eof:
                if should_stop():
                    raise _Interrupted()
                returning = line_no + 1 >= args.start_line
                want = READ_CHUNK_BYTES
                if returning:
                    want = min(want, READ_CONTENT_CAP_BYTES + 1 - content_bytes - len(buf))
                    if want <= 0:  # the pending line cannot fit in this page
                        if not page.items:
                            raise _execution(too_long)
                        truncated, next_start = True, line_no + 1
                        break
                want = min(want, READ_FILE_MAX_BYTES + 1 - scanned)
                if want <= 0:
                    raise _execution(f"file exceeds the {READ_FILE_MAX_BYTES}-byte scan limit: {frag!r}")
                chunk = self._read_once(fd, want)
                scanned += len(chunk)
                if chunk:
                    buf += chunk
                else:
                    eof = True
                continue
            if newline < 0:
                if not buf:
                    break
                raw_line, buf = buf, b""
            else:
                raw_line, buf = buf[: newline + 1], buf[newline + 1 :]
            line_no += 1
            if line_no < args.start_line:
                continue
            if line_no > args.last_line:
                next_start = line_no
                break
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                raise _execution(f"file is not valid UTF-8: {frag!r}") from None
            item = [line_no, text]
            if content_bytes + len(raw_line) > READ_CONTENT_CAP_BYTES or not page.fits(item):
                if not page.items:
                    raise _execution(too_long)
                truncated, next_start = True, line_no
                break
            page.add(item)
            content_bytes += len(raw_line)
        payload = {
            "eof": next_start is None,
            "lines": page.items,
            "next_start_line": next_start,
            "page_truncated": truncated,
        }
        summary = f"{len(page.items)} line(s), {content_bytes} byte(s), page_truncated={str(truncated).lower()}"
        return ToolOutcome(ok=True, payload=payload, summary=summary)

    # -- list_directory -----------------------------------------------------

    def list_directory(self, args: ListDirectoryArgs, should_stop: Callable[[], bool] = _never) -> ToolOutcome:
        if not isinstance(args, ListDirectoryArgs):
            raise TypeError("list_directory requires ListDirectoryArgs")
        return self._run(lambda ctx: self._list_directory(ctx, args, should_stop))

    def _list_directory(
        self, ctx: _CallDescriptors, args: ListDirectoryArgs, should_stop: Callable[[], bool]
    ) -> ToolOutcome:
        components = check_relative_path(args.path, allow_root=True)
        frag = safe_path_fragment(args.path)
        after = decode_list_cursor(args.cursor, args.path) if args.cursor is not None else None
        admin = self._admin_identity(ctx)
        dir_fd = self._open_dirs(ctx, components, admin, frag)
        entries = self._enumerate(dir_fd, admin, should_stop)
        if entries is None:
            raise _execution(f"directory exceeds the enumeration limit: {frag!r}")
        page = _Page(LIST_RESERVE)
        last_raw: bytes | None = None
        truncated = False
        for raw, decoded, st, is_admin in entries:
            if is_admin or st is None or (after is not None and raw <= after):
                continue
            kind = self._entry_type(st)
            item: dict[str, Any] = {"name": decoded, "type": kind}
            if decoded is None:
                item["name_b64"] = b64url(raw)
            if kind == "file":
                item["size"] = st.st_size
            if len(page.items) >= args.limit or not page.fits(item):
                truncated = True
                break
            page.add(item)
            last_raw = raw
        if truncated and last_raw is None:  # unreachable: one entry always fits (pinned by a test)
            raise _execution("directory entry exceeds the payload limit")
        next_cursor = encode_list_cursor(args.path, last_raw) if truncated else None
        payload = {"entries": page.items, "next_cursor": next_cursor, "page_truncated": truncated}
        summary = f"{len(page.items)} entr(ies), page_truncated={str(truncated).lower()}"
        return ToolOutcome(ok=True, payload=payload, summary=summary)

    # -- search_text --------------------------------------------------------

    def check_search_cursor(self, args: SearchTextArgs) -> str | None:
        """Policy-stage chain check with no filesystem access: None if the
        cursor is acceptable, else `chain_unknown`, `args_mismatch` or
        `cursor_not_current`."""
        if not isinstance(args, SearchTextArgs):
            raise TypeError("check_search_cursor requires SearchTextArgs")
        return self._chains.rejection(args)

    def search_text(self, args: SearchTextArgs, should_stop: Callable[[], bool] = _never) -> ToolOutcome:
        if not isinstance(args, SearchTextArgs):
            raise TypeError("search_text requires SearchTextArgs")
        rejection = self._chains.rejection(args)
        if rejection is not None:  # defensive re-check of the policy stage
            return self._failure(ErrorCode.TOOL_INPUT_INVALID, f"search cursor rejected: {rejection}")
        return self._run(lambda ctx: self._search_text(ctx, args, should_stop))

    def _search_text(self, ctx: _CallDescriptors, args: SearchTextArgs, should_stop: Callable[[], bool]) -> ToolOutcome:
        base = check_relative_path(args.path, allow_root=True)
        frag = safe_path_fragment(args.path)
        admin = self._admin_identity(ctx)
        nonce: str | None = None
        chain: _Chain | None = None
        if args.cursor is not None:
            nonce, chain = self._chains.current(args)
        start_fd = self._open_dirs(ctx, base, admin, frag)
        walk = _SearchWalk(self, ctx, args, admin, should_stop, chain.position if chain else None, base)
        finished = walk.run(start_fd)
        coverage = all(v == 0 for v in walk.unsearched.values())
        next_cursor: str | None = None
        search_complete = False
        if finished:
            search_complete = (chain.coverage_ok if chain else True) and coverage
            if nonce is not None:
                self._chains.finish(nonce)
        elif chain is not None and nonce is not None:
            chain.page += 1
            chain.position = walk.position
            chain.coverage_ok = chain.coverage_ok and coverage
            chain.expected_cursor = next_cursor = encode_search_cursor(nonce, chain.page)
        else:
            new_nonce = self._chains.allocate()
            if new_nonce is None:
                raise _execution("search chain allocation failed")
            next_cursor = encode_search_cursor(new_nonce, 1)
            self._chains.install(
                new_nonce,
                _Chain(
                    key=args.chain_key, page=1, expected_cursor=next_cursor, position=walk.position, coverage_ok=coverage
                ),
            )
        payload = {
            "excluded": dict(walk.excluded),
            "next_cursor": next_cursor,
            "page_coverage_complete": coverage,
            "page_truncated": not finished,
            "results": walk.page.items,
            "search_complete": search_complete,
            "unsearched": dict(walk.unsearched),
        }
        summary = f"{len(walk.page.items)} result(s), page_truncated={str(not finished).lower()}"
        return ToolOutcome(ok=True, payload=payload, summary=summary)


class _SearchWalk:
    """One `search_text` page: a deterministic depth-first, pre-order walk
    (entries sorted by raw name bytes) that resumes strictly after the
    server-held position and records the last fully processed position
    `(path components, line or None)` when a cap stops the page. `None`
    for the line means the entry and its whole subtree are done."""

    def __init__(
        self,
        tools: RepoTools,
        ctx: _CallDescriptors,
        args: SearchTextArgs,
        admin: tuple[int, int] | None,
        should_stop: Callable[[], bool],
        resume: tuple[tuple[bytes, ...], int | None] | None,
        base: tuple[str, ...],
    ) -> None:
        self.tools = tools
        self.ctx = ctx
        self.args = args
        self.admin = admin
        self.should_stop = should_stop
        self.resume = resume
        self.base = tuple(c.encode("utf-8") for c in base)
        self.page = _Page(SEARCH_RESERVE)
        self.excluded = dict.fromkeys(_EXCLUDED_KEYS, 0)
        self.unsearched = dict.fromkeys(_UNSEARCHED_KEYS, 0)
        self.position: tuple[tuple[bytes, ...], int | None] = resume if resume is not None else (self.base, 0)
        self.examined = 0
        self.scanned = 0
        self.progress = False

    def run(self, start_fd: int) -> bool:
        entries = self.tools._enumerate(start_fd, self.admin, self.should_stop)
        if entries is None:
            self.unsearched["oversized_directories"] += 1
            return True
        try:
            self._walk(start_fd, self.base, entries)
        except _StopPage:
            return False
        return True

    def _done(self, path: tuple[bytes, ...], line: int | None) -> None:
        self.position = (path, line)
        self.progress = True

    def _relation(self, path: tuple[bytes, ...]) -> str:
        if self.resume is None:
            return "new"
        pos, line = self.resume
        if path == pos:
            return "done" if line is None else "at"
        if pos[: len(path)] == path:
            return "ancestor"
        return "done" if path < pos else "new"

    def _walk(self, dir_fd: int, prefix: tuple[bytes, ...], entries: list[_Entry]) -> None:
        for raw, decoded, st, is_admin in entries:
            path = prefix + (raw,)
            relation = self._relation(path)
            if relation == "done":
                continue
            if relation != "ancestor":
                if self.progress and self.examined >= SEARCH_ENTRY_CAP:
                    raise _StopPage()
                self.examined += 1
                if self.examined % STOP_CHECK_INTERVAL == 0 and self.should_stop():
                    raise _Interrupted()
            self._visit(dir_fd, path, raw, decoded, st, is_admin, relation)

    def _visit(
        self,
        dir_fd: int,
        path: tuple[bytes, ...],
        raw: bytes,
        decoded: str | None,
        st: os.stat_result | None,
        is_admin: bool,
        relation: str,
    ) -> None:
        if is_admin:
            self._count("excluded", "administrative", path)
        elif st is None:
            self._count("excluded", "vanished", path)
        elif stat.S_ISLNK(st.st_mode):
            self._count("excluded", "symlinks", path)
        elif not stat.S_ISDIR(st.st_mode) and not stat.S_ISREG(st.st_mode):
            self._count("excluded", "non_regular", path)
        elif decoded is None:
            self._count("unsearched", "non_utf8_names", path)
        else:
            text_path = "/".join(c.decode("utf-8") for c in path)
            try:
                check_relative_path(text_path, allow_root=False)
            except ToolArgumentError:
                # Not expressible as a model path: too long, control
                # characters, a backslash, too deep (depth > 32 cannot be
                # reached: such directories are never entered).
                self._count("unsearched", "unaddressable_names", path)
                return
            if stat.S_ISDIR(st.st_mode):
                self._enter_dir(dir_fd, path, raw)
            else:
                self._scan_file(dir_fd, path, raw, text_path, st, relation)

    def _count(self, group: str, key: str, path: tuple[bytes, ...]) -> None:
        getattr(self, group)[key] += 1
        self._done(path, None)

    def _enter_dir(self, dir_fd: int, path: tuple[bytes, ...], raw: bytes) -> None:
        try:
            child_fd = self.ctx.openat(dir_fd, raw, _DIR_FLAGS)
        except FileNotFoundError:
            self._count("excluded", "vanished", path)
            return
        except OSError:
            self._count("unsearched", "unreadable", path)
            return
        try:
            child_st = _fstat(child_fd)
            if self.admin is not None and (child_st.st_dev, child_st.st_ino) == self.admin:
                self._count("excluded", "administrative", path)
                return
            children = self.tools._enumerate(child_fd, self.admin, self.should_stop)
            if children is None:
                self._count("unsearched", "oversized_directories", path)
                return
            if len(path) >= MAX_PATH_DEPTH:
                # Children would be deeper than the limit from the worktree root.
                if children:
                    self.unsearched["too_deep"] += 1
                self._done(path, None)
                return
            self._walk(child_fd, path, children)
        finally:
            self.ctx.release(child_fd)
        self._done(path, None)

    def _scan_file(
        self,
        dir_fd: int,
        path: tuple[bytes, ...],
        raw: bytes,
        text_path: str,
        st: os.stat_result,
        relation: str,
    ) -> None:
        if st.st_size > SEARCH_FILE_MAX_BYTES:
            self._count("unsearched", "oversized_files", path)
            return
        if self.progress and self.scanned + st.st_size > SEARCH_SCAN_CAP_BYTES:
            raise _StopPage()
        if self.should_stop():
            raise _Interrupted()
        try:
            fd = self.ctx.openat(dir_fd, raw, _FILE_FLAGS)
        except FileNotFoundError:
            self._count("excluded", "vanished", path)
            return
        except OSError:
            self._count("unsearched", "unreadable", path)
            return
        data = b""
        eof = False
        # The per-page scan cap is enforced on bytes actually read, never on
        # the (possibly stale) enumerated size: a file may have grown.
        limit = min(SEARCH_FILE_MAX_BYTES + 1, SEARCH_SCAN_CAP_BYTES - self.scanned)
        try:
            fst = _fstat(fd)
            if self.admin is not None and (fst.st_dev, fst.st_ino) == self.admin:
                self._count("excluded", "administrative", path)
                return
            if not stat.S_ISREG(fst.st_mode):
                self._count("excluded", "non_regular", path)
                return
            while len(data) < limit:
                if data and self.should_stop():
                    raise _Interrupted()
                try:
                    chunk = _read(fd, min(SEARCH_STOP_CHUNK_BYTES, limit - len(data)))
                except OSError:
                    self._count("unsearched", "unreadable", path)
                    return
                self.scanned += len(chunk)
                if not chunk:
                    eof = True
                    break
                data += chunk
        finally:
            self.ctx.release(fd)
        if not eof:
            if len(data) > SEARCH_FILE_MAX_BYTES:
                self._count("unsearched", "oversized_files", path)
                return
            # The page's scan budget ran out inside this file: emit nothing
            # from it and leave it unmarked, so the next page retries it.
            if self.progress:
                raise _StopPage()
            raise _execution("search scan budget is smaller than one file")
        if b"\x00" in data[:SEARCH_BINARY_PROBE_BYTES]:
            self._count("unsearched", "binary_files", path)
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._count("unsearched", "non_utf8_files", path)
            return
        resume_after = self.resume[1] if (relation == "at" and self.resume is not None) else 0
        query = self.args.query
        for index, line in enumerate(text.split("\n"), start=1):
            if index <= (resume_after or 0):
                continue
            column = line.find(query)
            if column >= 0:
                if len(self.page.items) >= self.args.limit:
                    raise _StopPage()
                start = max(0, min(column - (SNIPPET_MAX_CHARS - len(query)) // 2, len(line) - SNIPPET_MAX_CHARS))
                item = {
                    "column": column + 1,
                    "line": index,
                    "occurrences": line.count(query),
                    "path": text_path,
                    "snippet": line[start : start + SNIPPET_MAX_CHARS],
                }
                if not self.page.fits(item):
                    raise _StopPage()
                self.page.add(item)
            self._done(path, index)
        self._done(path, None)
