"""State-root initialization for Milestone 3 Slice 3A-1
(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1, sections 2, 6, 7, 15).

Implements: typed state-root location resolution and bounded
conventional ancestor creation, canonical-root open-or-create,
directional containment validation against a trusted repository
context, the four-state `state-root.json` initialization probe
(`VALID`/`ABSENT`/`RETRYABLE_PARTIAL`/`PERMANENTLY_INVALID`, with the
bounded post-`EEXIST` retry window applying only to the one legitimate
race the ADR describes), and the `StateRoot` object that subsequently
owns the long-lived root directory descriptor for `dir_fd`-relative
access to everything beneath it.

Not implemented here: `repo.json` (`repo_identity.py`), locks
(`state_locks.py`), `lifecycle.json`, or anything from Slice 3A-2
onward.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Callable

from ._lifecycle_fs import (
    STATE_ROOT_JSON_MAX_BYTES,
    LifecycleFsError,
    LifecycleFsFailure,
    StateRootLocation,
    StateRootOrigin,
    _assert_cloexec,
    _cloexec_flag,
    _directory_flag,
    _dominant_cleanup,
    _nofollow_flag,
    canonical_json_dumps,
    canonical_json_loads_strict,
    canonicalize_directory,
    close_confirmed,
    create_exclusive_directory_at,
    ensure_bounded_ancestor,
    fsync_fd,
    is_within_or_equal,
    list_directory_entries,
    open_existing_directory_chain_if_present,
    open_managed_directory_chain,
    open_private_create_exclusive_at,
    read_all_eintr_safe,
    validate_hex32,
    validate_safe_owned_directory_stat,
    write_all_eintr_safe,
)

STATE_ROOT_JSON_FILENAME = "state-root.json"
_RETRY_WINDOW_SECONDS = 2.0
_RETRY_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class TrustedRepositoryContext:
    """The canonical identity of the trusted source repository needed
    for state-root containment validation. Defined here (rather than
    only in `repo_identity.py`) so `state_root.py` has no import-time
    dependency on the repository-identity module; `repo_identity.py`
    constructs and returns this same shape."""

    working_tree_root: str | None
    common_dir: str


def open_or_create_canonical_root(location: StateRootLocation) -> tuple[int, str]:
    """Open (creating if necessary) the state-root directory at
    `location.path`, applying only the precisely bounded conventional
    ancestor creation `location.origin` permits, and return its
    non-inheritable descriptor plus case-canonical path. Never creates
    an arbitrary ancestor chain."""
    if location.conventional_parent_creation_allowed:
        if location.origin is StateRootOrigin.MACOS_DEFAULT:
            # location.path = <home>/Library/Application Support/CodeAgent
            home = os.path.dirname(os.path.dirname(os.path.dirname(location.path)))
            ensure_bounded_ancestor(home, ["Library", "Application Support"])
        elif location.origin is StateRootOrigin.LINUX_HOME_DEFAULT:
            # location.path = <home>/.local/state/codeagent
            home = os.path.dirname(os.path.dirname(os.path.dirname(location.path)))
            ensure_bounded_ancestor(home, [".local", "state"])
        elif location.origin is StateRootOrigin.XDG_DEFAULT:
            xdg_home = os.path.dirname(location.path)
            ensure_bounded_ancestor(os.path.dirname(xdg_home), [os.path.basename(xdg_home)])

    try:
        os.mkdir(location.path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the state-root directory could not be created",
        ) from None

    fd, canonical = canonicalize_directory(location.path)
    try:
        st = os.fstat(fd)
        validate_safe_owned_directory_stat(st)
        return fd, canonical
    except BaseException as exc:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise cleanup_exc from exc
        raise


def validate_state_root_containment(canonical_root: str, context: TrustedRepositoryContext) -> None:
    """The four directional containment checks of ADR 0004 Amendment 1
    section 6, on canonical paths only. A bare repository
    (`working_tree_root=None`) skips the working-tree-side checks."""
    worktrees_dir = str(Path(canonical_root) / "worktrees")

    if is_within_or_equal(canonical_root, context.common_dir):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the state root lies inside the trusted repository's git common directory",
        )
    if context.working_tree_root is not None:
        if is_within_or_equal(canonical_root, context.working_tree_root):
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "the state root lies inside the trusted repository's working tree",
            )
        if is_within_or_equal(context.working_tree_root, worktrees_dir):
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "the trusted repository's working tree lies inside the state root's worktrees directory",
            )
    if is_within_or_equal(context.common_dir, worktrees_dir):
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the trusted repository's git common directory lies inside the state root's worktrees directory",
        )


@unique
class StateRootProbe(str, Enum):
    """The exact four states ADR 0004 Amendment 1 section 15 names."""

    VALID = "valid"
    ABSENT = "absent"
    RETRYABLE_PARTIAL = "retryable_partial"
    PERMANENTLY_INVALID = "permanently_invalid"


@dataclass(frozen=True)
class _ProbeOutcome:
    kind: StateRootProbe
    payload: dict | None = None
    error: LifecycleFsError | None = None


def _probe_once(root_fd: int) -> _ProbeOutcome:
    flags = os.O_RDONLY | _nofollow_flag() | _cloexec_flag()
    try:
        fd = os.open(STATE_ROOT_JSON_FILENAME, flags, dir_fd=root_fd)
    except FileNotFoundError:
        return _ProbeOutcome(StateRootProbe.ABSENT)
    except OSError:
        return _ProbeOutcome(
            StateRootProbe.PERMANENTLY_INVALID,
            error=LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "state-root.json could not be opened"
            ),
        )
    try:
        try:
            _assert_cloexec(fd)
        except LifecycleFsError as exc:
            return _ProbeOutcome(StateRootProbe.PERMANENTLY_INVALID, error=exc)
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return _ProbeOutcome(
                StateRootProbe.PERMANENTLY_INVALID,
                error=LifecycleFsError(
                    LifecycleFsFailure.SYMLINK_REFUSED, "state-root.json is not a regular file"
                ),
            )
        if st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o600:
            return _ProbeOutcome(
                StateRootProbe.PERMANENTLY_INVALID,
                error=LifecycleFsError(
                    LifecycleFsFailure.UNSAFE_PERMISSIONS,
                    "state-root.json is not owned by the current user with the required private mode",
                ),
            )
        if st.st_size > STATE_ROOT_JSON_MAX_BYTES:
            return _ProbeOutcome(
                StateRootProbe.PERMANENTLY_INVALID,
                error=LifecycleFsError(LifecycleFsFailure.OVERSIZED, "state-root.json exceeds its size bound"),
            )
        data = read_all_eintr_safe(fd, STATE_ROOT_JSON_MAX_BYTES)
        if len(data) == 0:
            return _ProbeOutcome(StateRootProbe.RETRYABLE_PARTIAL)
        try:
            payload = canonical_json_loads_strict(data, max_bytes=STATE_ROOT_JSON_MAX_BYTES)
        except LifecycleFsError as exc:
            if exc.reason is LifecycleFsFailure.JSON_SYNTAX_INVALID:
                return _ProbeOutcome(StateRootProbe.RETRYABLE_PARTIAL)
            return _ProbeOutcome(StateRootProbe.PERMANENTLY_INVALID, error=exc)
        if not _valid_schema(payload):
            return _ProbeOutcome(
                StateRootProbe.PERMANENTLY_INVALID,
                error=LifecycleFsError(LifecycleFsFailure.SCHEMA_INVALID, "state-root.json failed schema validation"),
            )
        return _ProbeOutcome(StateRootProbe.VALID, payload=payload)
    finally:
        close_confirmed([fd])


def _valid_schema(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload.keys()) != {"schema_version", "state_root_id"}:
        return False
    schema_version = payload.get("schema_version")
    # bool is a subclass of int in Python (True == 1); a JSON boolean
    # must never be accepted as schema_version.
    if type(schema_version) is not int or schema_version != 1:
        return False
    state_root_id = payload.get("state_root_id")
    if not isinstance(state_root_id, str):
        return False
    try:
        validate_hex32(state_root_id, field_name="state_root_id")
    except LifecycleFsError:
        return False
    return True


@unique
class _CloseState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"


class StateRoot:
    """Owns the long-lived, open, non-inheritable state-root directory
    descriptor for as long as the object is alive. Every managed
    descendant is reached only through `dir_fd`-relative operations
    anchored to this descriptor — never a fresh full-pathname lookup.

    Ownership contract: `init_state_root` transfers ownership of
    `root_fd` to the returned `StateRoot` **only on success**. On any
    failure, `root_fd` remains owned by the caller, which must close it
    itself — `init_state_root` never closes a descriptor it did not
    itself open. `StateRoot` is a context manager: `__exit__` always
    attempts `close()`; an unconfirmed close dominates and is chained
    from an in-flight body exception rather than silently discarded or
    reported as success. Once a `close()` fails, the descriptor's fate
    is unconfirmed and `close()` must never be retried — a second call
    raises immediately without touching the descriptor again.
    """

    def __init__(self, *, root_fd: int, path: str, state_root_id: str) -> None:
        self.root_fd = root_fd
        self.path = path
        self.state_root_id = state_root_id
        self._close_state = _CloseState.OPEN

    def __enter__(self) -> "StateRoot":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except LifecycleFsError as cleanup_exc:
            if exc_value is not None:
                raise cleanup_exc from exc_value
            raise
        return False

    def open_repo_locks_dir(self) -> int:
        return open_managed_directory_chain(self.root_fd, ["repo-locks"])

    def open_repo_dir(self, repo_key: str) -> int:
        validate_hex32(repo_key, field_name="repo_key")
        return open_managed_directory_chain(self.root_fd, ["repos", repo_key])

    def open_repo_locks_dir_if_present(self) -> int | None:
        """Peek at (never create) `repo-locks/` (ADR 0004 Amendment 18)."""
        return open_existing_directory_chain_if_present(self.root_fd, ["repo-locks"])

    def open_repo_dir_if_present(self, repo_key: str) -> int | None:
        """Peek at (never create) `repos/<repo_key>/` (ADR 0004 Amendment 18)."""
        validate_hex32(repo_key, field_name="repo_key")
        return open_existing_directory_chain_if_present(self.root_fd, ["repos", repo_key])

    def open_worktrees_repo_dir_if_present(self, repo_key: str) -> int | None:
        """Peek at (never create) `worktrees/<repo_key>/`, for
        `repo_identity.py`'s `repo.json` creation precondition (ADR
        0004 section 4: creation is permitted only when neither
        `repos/<repo-key>/` nor `worktrees/<repo-key>/` holds state).
        Returns `None` if any component of the chain does not exist."""
        validate_hex32(repo_key, field_name="repo_key")
        return open_existing_directory_chain_if_present(self.root_fd, ["worktrees", repo_key])

    def observe_materialized_worktree_leaf(self, repo_key: str, lifecycle_id: str) -> "MaterializedLeafObservation":
        """ADR 0004 Amendment 13: a fresh, no-follow, fd-relative observation
        of a dead lifecycle's deterministic worktree leaf. Holds no
        descriptor after it returns. `MATERIALIZED` means a real directory,
        owned by the current user, mode exactly 0700, containing a no-follow
        regular `.git` file; it cannot tell the original directory from a
        valid replacement. Raises `LifecycleFsError(CLEANUP_UNCONFIRMED)` on
        an unconfirmed descriptor close (never reported as `UNKNOWN`), or the
        `_assert_cloexec` capability error."""
        validate_hex32(repo_key, field_name="repo_key")
        validate_hex32(lifecycle_id, field_name="lifecycle_id")
        return _observe_materialized_leaf(self, repo_key, lifecycle_id)

    def open_abandoned_worktree_leaf(self, repo_key: str, lifecycle_id: str) -> "AbandonedLeafOpenResult":
        """ADR 0004 Amendment 12: observe (never create) a dead lifecycle's
        deterministic worktree leaf. Returns `EMPTY_PRIVATE_DIRECTORY` with
        an owned handle only for a real, current-user-owned, mode-0700,
        empty directory whose no-follow name lookup and opened descriptor
        are the same inode; otherwise `ABSENT`/`CONFLICT`/`UNKNOWN` with
        every locally opened descriptor already closed exactly once. Raises
        `LifecycleFsError(CLEANUP_UNCONFIRMED)` if that close fails (never
        reported as `UNKNOWN`), or the `_assert_cloexec` capability error."""
        validate_hex32(repo_key, field_name="repo_key")
        validate_hex32(lifecycle_id, field_name="lifecycle_id")
        return _open_abandoned_leaf(self, repo_key, lifecycle_id)

    def reserve_worktree_leaf(self, repo_key: str, lifecycle_id: str) -> "_WorktreeLeafReservation":
        """Milestone 3 Slice 3C-2 (ADR 0004 sections 8/16's already-accepted
        deterministic worktree layout, made real for the first time by this
        method). Opens/creates `worktrees/<repo_key>/` via the same managed,
        idempotently-reopenable directory-chain primitive every other
        managed parent directory in this module already uses (mirrors
        `open_repo_dir`), then exclusively creates the `<lifecycle_id>` leaf
        via `create_exclusive_directory_at` — refusing collision, wrong
        type, or a pre-existing symlink at that exact name; `FileExistsError`
        propagates unmodified on a collision, exactly like every other
        exclusive-creation call site in this codebase (e.g.
        `runs/<lifecycle_id>/`), so a caller can distinguish a genuine
        collision from any other failure. The leaf is never reopened or
        adopted by this method — it is exclusive-create-only, with no
        idempotent variant, since no code path in this system ever needs to
        re-create the same lifecycle's worktree leaf twice (ADR 0003's
        discard-and-recreate model operates within an already-registered
        worktree, never by re-entering this creation primitive).

        Returns a `_WorktreeLeafReservation` — the only sanctioned way for a
        caller to obtain one; see that class's own docstring for the exact,
        honestly-scoped guarantee this provides and the ownership contract
        `GitWorktree(reservation=...)` relies on. This method never closes a
        descriptor it did not itself open, and if the exclusive leaf
        creation fails after the parent chain was successfully opened, the
        parent descriptor is closed before the original failure propagates
        (chained `from` it if that close itself fails — never silently
        discarded, never masking the original failure).
        """
        validate_hex32(repo_key, field_name="repo_key")
        validate_hex32(lifecycle_id, field_name="lifecycle_id")
        parent_fd = open_managed_directory_chain(self.root_fd, ["worktrees", repo_key])
        try:
            leaf_fd = create_exclusive_directory_at(parent_fd, lifecycle_id)
        except BaseException as exc:
            try:
                close_confirmed([parent_fd])
            except LifecycleFsError as cleanup_exc:
                raise cleanup_exc from exc
            raise
        path = Path(self.path) / "worktrees" / repo_key / lifecycle_id
        return _WorktreeLeafReservation(
            path=path,
            parent_fd=parent_fd,
            leaf_fd=leaf_fd,
            leaf_name=lifecycle_id,
            repo_key=repo_key,
            lifecycle_id=lifecycle_id,
            state_root_id=self.state_root_id,
        )

    def close(self) -> None:
        if self._close_state is _CloseState.CLOSED:
            return
        if self._close_state is _CloseState.FAILED:
            raise LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "the state root's descriptor close previously failed and must not be retried",
            )
        try:
            close_confirmed([self.root_fd])
        except LifecycleFsError:
            self._close_state = _CloseState.FAILED
            raise
        self._close_state = _CloseState.CLOSED


@unique
class LeafObservation(str, Enum):
    """`_WorktreeLeafReservation.observe_leaf()`'s result (ADR 0004
    Amendment 11). Only a confirmed `ENOENT` on the leaf name is
    `ABSENT`; every other inspection failure is `UNKNOWN`, never
    `ABSENT`. Link count is never consulted (an unlinked directory's held
    descriptor can still report a nonzero `st_nlink`, e.g. on APFS)."""

    ABSENT = "absent"
    RESERVED_INODE = "reserved_inode"
    OTHER = "other"
    UNKNOWN = "unknown"


@unique
class _ReservationState(str, Enum):
    """`_WorktreeLeafReservation`'s single-consumer state machine (Slice
    3C-2 correction pass). `RESERVED` is the initial state immediately
    after `StateRoot.reserve_worktree_leaf()` returns; `claim()` alone
    transitions `RESERVED -> CLAIMED`; `consume()` alone transitions
    `CLAIMED -> CONSUMED`. No transition ever moves backward."""

    RESERVED = "reserved"
    CLAIMED = "claimed"
    CONSUMED = "consumed"


class _WorktreeLeafReservation:
    """An exclusively-created, currently-empty worktree-leaf directory and
    its two open, non-inheritable fd-relative descriptors — mintable in
    practice only via `StateRoot.reserve_worktree_leaf()` (Milestone 3
    Slice 3C-2).

    Not a public constructor for ordinary use: nothing outside this module
    and `workspace.py`'s consumption of the returned instance should ever
    construct one directly. This is the same leading-underscore, "the only
    sanctioned way to obtain one" idiom `lifecycle_store._LifecycleProjectionWriter`
    already establishes for the identical problem elsewhere in this
    codebase — Python does not and cannot prevent a determined caller in
    the same process from importing this class and misusing it, and this
    class makes no such claim. The actual, honestly-scoped guarantee is
    narrower and different in kind: no string sourced from outside this
    reservation mechanism (a CLI argument, a config value, an externally
    supplied path) can, on its own, ever become the path `GitWorktree`
    treats as this trusted deterministic location — producing a colliding
    `(descriptor, path)` pair that also survives `GitWorktree`'s own
    independent same-inode re-verification requires already holding a
    real, currently-valid descriptor to a real directory at that exact
    path, which is not obtainable from a string alone. Defending against
    hostile code already running inside this same trusted process is out
    of scope everywhere in this codebase (`docs/threat-model.md` A4: the
    host process is part of the trusted computing base).

    Ownership: for the duration of this reservation's own `with` block, it
    owns both `parent_fd` (an open descriptor to `worktrees/<repo_key>/`)
    and `leaf_fd` (an open, `O_NOFOLLOW` descriptor to the exclusively
    created, still-empty `<lifecycle_id>` leaf). `consume()` transfers
    ownership of the directory/worktree *resource* to whatever called it
    (in practice, `GitWorktree.__enter__()`, only after a complete,
    successfully re-verified entry) — it does **not** transfer descriptor-
    close responsibility: `__exit__` always closes both descriptors,
    whether or not `consume()` was ever called. What `consume()` changes
    is only whether `__exit__` *also* attempts to remove the leaf
    directory: once consumed, it must never do so, since a live or
    preserved worktree may occupy that path — see `__exit__`'s own
    docstring for the complete cleanup-ownership state machine.

    Single-consumer state machine (`RESERVED -> CLAIMED -> CONSUMED`,
    Slice 3C-2 correction pass): `verify_identity()` alone cannot prevent
    two `GitWorktree` instances from both being handed the same
    reservation and both reaching `git worktree add` — identity would
    still agree for both, since nothing has mutated the path yet.
    `claim()` closes this: only the first caller succeeds, transitioning
    `RESERVED -> CLAIMED` under an internal lock (not a plain check-then-set
    on an attribute, which is not safe against a genuine thread switch
    between the check and the set); every later claim attempt — from a
    second `GitWorktree` instance, sequential or concurrent — is refused
    *before* any Git mutation is ever attempted, and specifically before
    that second instance could reach its own enter-time failure cleanup,
    which would otherwise identity-verify successfully (the path is still
    the same inode) and remove the *first* instance's real, live worktree.
    `consume()` is legal only from `CLAIMED` (transitioning to `CONSUMED`);
    a failed entry after a successful claim leaves the reservation
    `CLAIMED`, never `CONSUMED` — indistinguishable from "never claimed"
    for `__exit__`'s cleanup purposes (both attempt the same
    identity-reverified removal) but permanently refusing any further
    claim, matching the "a failed entry cannot reset the reservation into
    a reusable state" requirement (an explicit, proven-safe retry
    contract is deliberately not designed in this slice).
    """

    def __init__(
        self,
        *,
        path: Path,
        parent_fd: int,
        leaf_fd: int,
        leaf_name: str,
        repo_key: str,
        lifecycle_id: str,
        state_root_id: str,
    ) -> None:
        self._path = path
        self._parent_fd = parent_fd
        self._leaf_fd = leaf_fd
        self._leaf_name = leaf_name
        self._repo_key = repo_key
        self._lifecycle_id = lifecycle_id
        self._state_root_id = state_root_id
        self._state = _ReservationState.RESERVED
        self._claim_lock = threading.Lock()
        self._descriptors_closed = False
        self.cleanup_error: LifecycleFsError | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ADR 0004 Amendment 11: the identity `GitWorktree` binds an optional
    # worktree publisher against, before any Git call. All three are set
    # once by `StateRoot.reserve_worktree_leaf()` and never change.
    @property
    def repo_key(self) -> str:
        return self._repo_key

    @property
    def lifecycle_id(self) -> str:
        return self._lifecycle_id

    @property
    def state_root_id(self) -> str:
        return self._state_root_id

    def observe_leaf(self) -> LeafObservation:
        """Observational, non-mutating, never raises (ADR 0004 Amendment
        11). The closed flag is checked before any descriptor is touched:
        a closed descriptor number may already have been reused by an
        unrelated open file, so relying on `EBADF` could silently inspect
        the wrong directory. fd-relative, no-follow `stat` of the leaf
        name; only a confirmed `ENOENT` is `ABSENT`."""
        if self._descriptors_closed:
            return LeafObservation.UNKNOWN
        try:
            current = os.stat(self._leaf_name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return LeafObservation.ABSENT
        except OSError:
            return LeafObservation.UNKNOWN
        if not stat.S_ISDIR(current.st_mode):
            return LeafObservation.OTHER
        try:
            reserved = os.fstat(self._leaf_fd)
        except OSError:
            return LeafObservation.UNKNOWN
        if os.path.samestat(current, reserved):
            return LeafObservation.RESERVED_INODE
        return LeafObservation.OTHER

    def fileno(self) -> int:
        """Read-only access to the open leaf descriptor, for an
        fd-relative identity check (`os.fstat`). Raises `LifecycleFsError`
        if the descriptor has already been closed."""
        if self._descriptors_closed:
            raise LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "the worktree-leaf reservation's descriptor is no longer open",
            )
        return self._leaf_fd

    def claim(self) -> None:
        """Atomically transition `RESERVED -> CLAIMED`. Only ever succeeds
        once, across any number of sequential or concurrent callers —
        protected by an internal lock so the check-and-set is a single
        indivisible operation, not two separately-scheduled bytecode
        steps a thread switch could interleave. Must be called by
        `GitWorktree.__enter__()` before any Git mutation is attempted
        against this reservation's path. Raises `LifecycleFsError` on any
        state other than `RESERVED` — including a second claim from the
        same caller — never silently granting or sharing a claim."""
        with self._claim_lock:
            if self._state is not _ReservationState.RESERVED:
                raise LifecycleFsError(
                    LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                    "the worktree-leaf reservation has already been claimed",
                )
            self._state = _ReservationState.CLAIMED

    def verify_identity(self) -> bool:
        """fd-relative re-check: does `<leaf_name>` under `parent_fd` still
        refer to the exact inode this reservation exclusively created?
        Used both pre-Git (before any `git worktree add` is ever invoked)
        and post-Git (immediately after `git worktree add` reports
        success, before `consume()`). Deliberately never follows a
        symlink at the leaf name (`follow_symlinks=False`) — a hostile
        replacement is exactly as untrustworthy whether or not it happens
        to be a symlink. Returns `False` on any stat failure (an
        uninspectable identity is exactly as untrustworthy as a confirmed
        mismatch) — never raises.
        """
        if self._descriptors_closed:
            return False
        try:
            current = os.stat(self._leaf_name, dir_fd=self._parent_fd, follow_symlinks=False)
            reserved = os.fstat(self._leaf_fd)
        except OSError:
            return False
        return os.path.samestat(current, reserved)

    def consume(self) -> None:
        """Called exactly once, only by `GitWorktree.__enter__()`, only
        after a successful `claim()` and only after `git worktree add`
        has succeeded **and** post-Git identity/registration
        re-verification has confirmed the registered path still
        corresponds to this exact reservation. Transitions
        `CLAIMED -> CONSUMED` so `__exit__` will never attempt to remove
        the (now real, Git-registered) directory — descriptor closure is
        unaffected and remains `__exit__`'s own responsibility
        regardless. A descriptor-close failure occurring later must never
        retroactively un-consume this reservation: `consume()`'s effect is
        permanent from the moment it returns. Legal only from `CLAIMED` —
        `consume()` without a prior `claim()`, or a second `consume()`
        call, are both refused categorically."""
        if self._state is not _ReservationState.CLAIMED:
            raise LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "a worktree-leaf reservation can only be consumed from the claimed state",
            )
        self._state = _ReservationState.CONSUMED

    def __enter__(self) -> "_WorktreeLeafReservation":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """Cleanup-ownership state machine (Slice 3C-2):

        - Not in the `CONSUMED` state (whether never claimed, or claimed
          but the entry failed before `consume()`): attempts a safe,
          identity-reverified removal of the still-empty leaf directory
          first (never a plain "it's empty, remove it" — see
          `_remove_unconsumed_leaf_if_safe`).
        - `CONSUMED`: never attempts removal, under any circumstance —
          including a later descriptor-close failure, which must never
          be misread as "this reservation was unused."
        - Both branches always then attempt to close both descriptors,
          regardless of whether removal was attempted or what its outcome
          was.
        - If both removal and descriptor closure fail, that is reported
          as one combined, sanitized `LifecycleFsError` rather than
          silently discarding either failure.
        - Matching `GitWorktree.__exit__`'s own existing convention
          (rather than `LifecycleLease.__exit__`'s "always raise, chained"
          convention — a deliberate choice, since this reservation is
          used nested inside `GitWorktree`'s own `with` block and should
          not clobber a more significant in-flight exception with a
          leftover-cleanup complaint): the combined failure is raised
          only if nothing else is already propagating (`exc_type is
          None`); otherwise it is recorded on `self.cleanup_error` and the
          in-flight exception continues unmasked.
        """
        removal_error: LifecycleFsError | None = None
        if self._state is not _ReservationState.CONSUMED:
            removal_error = self._remove_unconsumed_leaf_if_safe()

        close_error: LifecycleFsError | None = None
        try:
            close_confirmed([self._leaf_fd, self._parent_fd])
        except LifecycleFsError as exc2:
            close_error = exc2
        self._descriptors_closed = True

        combined = self._combine_cleanup_errors(removal_error, close_error)
        if combined is not None:
            self.cleanup_error = combined
            if exc_type is None:
                raise combined

    def _remove_unconsumed_leaf_if_safe(self) -> LifecycleFsError | None:
        """Never a pathname-only "it is empty, therefore remove it" check
        (a same-user process could have replaced the lifecycle pathname
        with a different, also-empty directory since this reservation was
        created — a pathname-only check cannot distinguish the two).
        Instead: (1) confirm the leaf name is either still exactly this
        reservation's own inode or genuinely, confirmedly absent — a
        `GitWorktree` enter-time failure may already have removed the
        entire directory via Git's own exact-registration cleanup before
        this ever runs, and that must be recognized as a clean no-op, not
        conflated with a hostile same-name replacement; anything else
        (present but a different inode, or uninspectable) refuses to
        touch anything; (2) inspect emptiness through the already-open
        leaf descriptor, not a fresh path lookup; (3) remove by name
        relative to the held parent descriptor (`dir_fd=parent_fd`),
        anchoring every ancestor component to an already-open,
        already-verified descriptor rather than a fresh full-pathname
        resolution. This does not, and cannot, eliminate the residual
        window between step (1)'s check and step (3)'s removal — no
        POSIX interface makes directory removal atomic against a prior
        stat — but it closes the much larger, structurally avoidable gap
        a plain path-string `rmdir` would leave open. Returns `None` on
        confirmed removal or confirmed pre-existing absence; a sanitized
        `LifecycleFsError` (never raised directly) on any disagreement or
        failure, with nothing removed."""
        if self._descriptors_closed:
            return LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "the worktree-leaf reservation's identity could not be reconfirmed; "
                "no removal was attempted",
            )
        # ADR 0004 Amendment 12: the identity/emptiness/rmdir/post-
        # observation sequence is shared with the reconciler's abandoned-
        # leaf removal. This mapping keeps every externally visible
        # message exactly as before. An already-absent name (most
        # plausibly GitWorktree's own enter-time cleanup already ran
        # `git worktree remove --force`) is a clean, confirmed no-op.
        result = _remove_empty_leaf_and_confirm(self._parent_fd, self._leaf_name, self._leaf_fd)
        return _reservation_removal_error(result)

    @staticmethod
    def _combine_cleanup_errors(
        removal_error: LifecycleFsError | None,
        close_error: LifecycleFsError | None,
    ) -> LifecycleFsError | None:
        """Combine the two independent `__exit__` cleanup outcomes into at
        most one error — neither is silently discarded in favor of the
        other, matching `workspace.GitWorktree._combine_cleanup_errors`'s
        identical role for the same class of problem."""
        if removal_error is not None and close_error is not None:
            return LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "neither the worktree-leaf reservation's removal nor its descriptor "
                "closure could be confirmed",
            )
        return removal_error or close_error


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 12: shared empty-leaf removal, and the abandoned-leaf
# handle a reconciler uses after the original owner's process (and its
# reservation descriptors) are gone.
# ---------------------------------------------------------------------------


@unique
class RmdirReport(str, Enum):
    NOT_CALLED = "not_called"
    SUCCEEDED = "succeeded"
    ENOTEMPTY = "enotempty"
    OTHER_ERROR = "other_error"


@unique
class LeafRemovalKind(str, Enum):
    ALREADY_ABSENT = "already_absent"
    PRE_CONFLICT = "pre_conflict"
    PRE_INSPECTION_FAILED = "pre_inspection_failed"
    NOT_EMPTY_AT_RMDIR = "not_empty_at_rmdir"
    POST_ABSENT = "post_absent"
    ORIGINAL_STILL_PRESENT = "original_still_present"
    REPLACEMENT_CONFLICT = "replacement_conflict"
    POST_INSPECTION_FAILED = "post_inspection_failed"


@unique
class LeafRemovalStage(str, Enum):
    IDENTITY = "identity"
    LISTING = "listing"
    RMDIR = "rmdir"
    POST_OBSERVATION = "post_observation"


@dataclass(frozen=True)
class _LeafRemovalResult:
    kind: LeafRemovalKind
    stage: LeafRemovalStage
    rmdir: RmdirReport
    # True if the post-removal lstat found an entry; None if not reached.
    post_name_present: bool | None = None


def _directory_has_any_entry(fd: int) -> bool:
    """Bounded emptiness check: stops at the first entry. `os.scandir(fd)`
    duplicates `fd`, so the caller's descriptor stays open. `OSError`
    propagates for the caller to classify."""
    with os.scandir(fd) as it:
        for _ in it:
            return True
    return False


def _remove_empty_leaf_and_confirm(parent_fd: int, name: str, leaf_fd: int) -> _LeafRemovalResult:
    """Remove the empty directory `name` beneath `parent_fd` only while it
    is still the exact inode `leaf_fd` refers to, then confirm by a fresh,
    no-follow, fd-relative observation (ADR 0004 Amendment 12). Never
    closes a descriptor; never raises for an OS failure.

    `rmdir` is non-recursive and refuses a symlink, a non-directory, and a
    non-empty directory, but it still acts on a *name* relative to
    `parent_fd`: it proves neither ownership nor (when it reports success)
    absence. The same-user replacement race between the final re-check and
    `rmdir` remains; a held `leaf_fd` keeps its inode from being reused, so
    the post-removal same-inode comparison is reliable against inode reuse
    — not a guarantee against a hostile same-user process."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _LeafRemovalResult(LeafRemovalKind.ALREADY_ABSENT, LeafRemovalStage.IDENTITY, RmdirReport.NOT_CALLED)
    except OSError:
        return _LeafRemovalResult(
            LeafRemovalKind.PRE_INSPECTION_FAILED, LeafRemovalStage.IDENTITY, RmdirReport.NOT_CALLED
        )
    try:
        held = os.fstat(leaf_fd)
    except OSError:
        return _LeafRemovalResult(
            LeafRemovalKind.PRE_INSPECTION_FAILED, LeafRemovalStage.IDENTITY, RmdirReport.NOT_CALLED
        )
    if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(current, held):
        return _LeafRemovalResult(LeafRemovalKind.PRE_CONFLICT, LeafRemovalStage.IDENTITY, RmdirReport.NOT_CALLED)

    try:
        has_entry = _directory_has_any_entry(leaf_fd)
    except OSError:
        return _LeafRemovalResult(
            LeafRemovalKind.PRE_INSPECTION_FAILED, LeafRemovalStage.LISTING, RmdirReport.NOT_CALLED
        )
    if has_entry:
        return _LeafRemovalResult(LeafRemovalKind.PRE_CONFLICT, LeafRemovalStage.LISTING, RmdirReport.NOT_CALLED)

    try:
        os.rmdir(name, dir_fd=parent_fd)
        report = RmdirReport.SUCCEEDED
    except OSError as exc:
        if exc.errno == errno.ENOTEMPTY:
            # rmdir never removes a non-empty directory: nothing removed.
            return _LeafRemovalResult(
                LeafRemovalKind.NOT_EMPTY_AT_RMDIR, LeafRemovalStage.RMDIR, RmdirReport.ENOTEMPTY
            )
        report = RmdirReport.OTHER_ERROR

    # A successful syscall report is never taken as proof of absence.
    try:
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _LeafRemovalResult(
            LeafRemovalKind.POST_ABSENT, LeafRemovalStage.POST_OBSERVATION, report, post_name_present=False
        )
    except OSError:
        return _LeafRemovalResult(
            LeafRemovalKind.POST_INSPECTION_FAILED, LeafRemovalStage.POST_OBSERVATION, report
        )
    try:
        held_after = os.fstat(leaf_fd)
    except OSError:
        return _LeafRemovalResult(
            LeafRemovalKind.POST_INSPECTION_FAILED,
            LeafRemovalStage.POST_OBSERVATION,
            report,
            post_name_present=True,
        )
    kind = (
        LeafRemovalKind.ORIGINAL_STILL_PRESENT
        if stat.S_ISDIR(after.st_mode) and os.path.samestat(after, held_after)
        else LeafRemovalKind.REPLACEMENT_CONFLICT
    )
    return _LeafRemovalResult(kind, LeafRemovalStage.POST_OBSERVATION, report, post_name_present=True)


def _reservation_removal_error(result: _LeafRemovalResult) -> LifecycleFsError | None:
    """Map a shared-primitive result back to `_WorktreeLeafReservation`'s
    exact pre-Amendment-12 messages (Slice 3C-2)."""
    kind, stage = result.kind, result.stage
    if kind is LeafRemovalKind.ALREADY_ABSENT:
        return None
    if stage is LeafRemovalStage.IDENTITY:
        message = "the worktree-leaf reservation's identity could not be reconfirmed; no removal was attempted"
    elif stage is LeafRemovalStage.LISTING:
        if kind is LeafRemovalKind.PRE_INSPECTION_FAILED:
            message = "the worktree-leaf reservation's contents could not be inspected before removal"
        else:
            message = "the worktree-leaf reservation is not empty and was not removed"
    elif result.rmdir is not RmdirReport.SUCCEEDED:
        # Any rmdir error was always reported, whatever was observed after.
        message = "the worktree-leaf reservation could not be removed"
    elif kind is LeafRemovalKind.POST_ABSENT:
        return None
    elif result.post_name_present:
        message = "the worktree-leaf reservation's removal could not be confirmed; an entry still exists at that name"
    else:
        message = "the worktree-leaf reservation's removal could not be confirmed"
    return LifecycleFsError(LifecycleFsFailure.CLEANUP_UNCONFIRMED, message)


@unique
class AbandonedLeafObservation(str, Enum):
    ABSENT = "absent"
    EMPTY_PRIVATE_DIRECTORY = "empty_private_directory"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"  # inspection failure only, never a close failure


@dataclass(frozen=True)
class AbandonedLeafOpenResult:
    observation: AbandonedLeafObservation
    leaf: "_AbandonedWorktreeLeaf | None"  # non-None iff EMPTY_PRIVATE_DIRECTORY


class _LeafObservationDiagnostic(Exception):
    """Private, categorical-only cause for a setup-cleanup failure: carries
    the observation the open was about to return, never raw OS text."""

    def __init__(self, observation: "AbandonedLeafObservation | MaterializedLeafObservation") -> None:
        super().__init__(observation.value)
        self.observation = observation


@unique
class _LeafCloseState(str, Enum):
    OPEN = "open"
    CLOSED_CONFIRMED = "closed_confirmed"
    CLOSE_FAILED = "close_failed"


class _AbandonedWorktreeLeaf:
    """An empty, private, current-user-owned directory found at a dead
    lifecycle's deterministic worktree leaf (ADR 0004 Amendment 12), with
    the two descriptors it was inspected through. Mintable only by
    `StateRoot.open_abandoned_worktree_leaf()`.

    Close state machine: the first close attempts both descriptors exactly
    once. After an unconfirmed close the descriptor numbers are never
    touched again (either may already have been reused); the one latched
    sanitized error is re-raised unchanged by every later `close()`/
    `__exit__`. Chain on that first failure: latched error ->
    `close_confirmed`'s error -> the in-flight body exception, if any."""

    def __init__(self, *, parent_fd: int, leaf_fd: int, leaf_name: str) -> None:
        self._parent_fd = parent_fd
        self._leaf_fd = leaf_fd
        self._leaf_name = leaf_name
        self._close_state = _LeafCloseState.OPEN
        self._close_error: LifecycleFsError | None = None
        self._removal_attempted = False

    def remove_if_still_empty(self) -> _LeafRemovalResult:
        if self._close_state is not _LeafCloseState.OPEN or self._removal_attempted:
            raise LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "the abandoned worktree leaf is no longer eligible for removal",
            )
        self._removal_attempted = True
        return _remove_empty_leaf_and_confirm(self._parent_fd, self._leaf_name, self._leaf_fd)

    def _close_once(self, in_flight: BaseException | None) -> None:
        if self._close_state is _LeafCloseState.CLOSED_CONFIRMED:
            return
        if self._close_state is _LeafCloseState.CLOSE_FAILED:
            assert self._close_error is not None
            raise self._close_error
        try:
            _dominant_cleanup([self._leaf_fd, self._parent_fd], in_flight)
        except LifecycleFsError as close_exc:
            error = LifecycleFsError(
                LifecycleFsFailure.CLEANUP_UNCONFIRMED,
                "the abandoned worktree-leaf descriptors could not be confirmed closed",
            )
            error.__cause__ = close_exc
            self._close_error = error
            self._close_state = _LeafCloseState.CLOSE_FAILED
            raise error
        self._close_state = _LeafCloseState.CLOSED_CONFIRMED

    def close(self) -> None:
        self._close_once(None)

    def __enter__(self) -> "_AbandonedWorktreeLeaf":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._close_once(exc_value)
        return False


@unique
class MaterializedLeafObservation(str, Enum):
    ABSENT = "absent"
    MATERIALIZED = "materialized"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"  # inspection failure only, never a close failure


def _observe_materialized_leaf(state_root: "StateRoot", repo_key: str, lifecycle_id: str) -> MaterializedLeafObservation:
    try:
        parent_fd = open_existing_directory_chain_if_present(state_root.root_fd, ["worktrees", repo_key])
    except LifecycleFsError as exc:
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            raise
        if exc.reason in (
            LifecycleFsFailure.SYMLINK_REFUSED,
            LifecycleFsFailure.NOT_A_DIRECTORY,
            LifecycleFsFailure.UNSAFE_PERMISSIONS,
        ):
            return MaterializedLeafObservation.CONFLICT
        return MaterializedLeafObservation.UNKNOWN
    if parent_fd is None:
        return MaterializedLeafObservation.ABSENT

    opened = [parent_fd]

    def finish(observation: MaterializedLeafObservation) -> MaterializedLeafObservation:
        # Every locally owned descriptor, attempted exactly once.
        _dominant_cleanup(opened, _LeafObservationDiagnostic(observation))
        return observation

    try:
        try:
            st = os.stat(lifecycle_id, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return finish(MaterializedLeafObservation.ABSENT)
        except OSError:
            return finish(MaterializedLeafObservation.UNKNOWN)
        if not stat.S_ISDIR(st.st_mode):
            return finish(MaterializedLeafObservation.CONFLICT)
        flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
        try:
            leaf_fd = os.open(lifecycle_id, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return finish(MaterializedLeafObservation.CONFLICT)
            return finish(MaterializedLeafObservation.UNKNOWN)
        opened.append(leaf_fd)
        _assert_cloexec(leaf_fd)
        try:
            held = os.fstat(leaf_fd)
        except OSError:
            return finish(MaterializedLeafObservation.UNKNOWN)
        if (
            not os.path.samestat(st, held)
            or held.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) != 0o700
        ):
            return finish(MaterializedLeafObservation.CONFLICT)
        try:
            git_file = os.stat(".git", dir_fd=leaf_fd, follow_symlinks=False)
        except FileNotFoundError:
            return finish(MaterializedLeafObservation.CONFLICT)
        except OSError:
            return finish(MaterializedLeafObservation.UNKNOWN)
        if not stat.S_ISREG(git_file.st_mode):
            return finish(MaterializedLeafObservation.CONFLICT)
        return finish(MaterializedLeafObservation.MATERIALIZED)
    except LifecycleFsError as exc:
        # `_assert_cloexec` failure, or a `finish()` cleanup failure that has
        # already attempted every descriptor -- never a second close attempt.
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            raise
        _dominant_cleanup(opened, exc)
        raise
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise


def _open_abandoned_leaf(state_root: "StateRoot", repo_key: str, lifecycle_id: str) -> AbandonedLeafOpenResult:
    try:
        parent_fd = open_existing_directory_chain_if_present(state_root.root_fd, ["worktrees", repo_key])
    except LifecycleFsError as exc:
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            raise
        if exc.reason in (
            LifecycleFsFailure.SYMLINK_REFUSED,
            LifecycleFsFailure.NOT_A_DIRECTORY,
            LifecycleFsFailure.UNSAFE_PERMISSIONS,
        ):
            return AbandonedLeafOpenResult(AbandonedLeafObservation.CONFLICT, None)
        return AbandonedLeafOpenResult(AbandonedLeafObservation.UNKNOWN, None)
    if parent_fd is None:
        return AbandonedLeafOpenResult(AbandonedLeafObservation.ABSENT, None)

    opened = [parent_fd]

    def finish(observation: AbandonedLeafObservation) -> AbandonedLeafOpenResult:
        # Every locally owned descriptor, attempted exactly once.
        _dominant_cleanup(opened, _LeafObservationDiagnostic(observation))
        return AbandonedLeafOpenResult(observation, None)

    try:
        try:
            st = os.stat(lifecycle_id, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return finish(AbandonedLeafObservation.ABSENT)
        except OSError:
            return finish(AbandonedLeafObservation.UNKNOWN)
        if not stat.S_ISDIR(st.st_mode):
            return finish(AbandonedLeafObservation.CONFLICT)
        flags = os.O_RDONLY | _directory_flag() | _nofollow_flag() | _cloexec_flag()
        try:
            leaf_fd = os.open(lifecycle_id, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return finish(AbandonedLeafObservation.CONFLICT)
            return finish(AbandonedLeafObservation.UNKNOWN)
        opened.append(leaf_fd)
        _assert_cloexec(leaf_fd)
        try:
            held = os.fstat(leaf_fd)
        except OSError:
            return finish(AbandonedLeafObservation.UNKNOWN)
        if (
            not os.path.samestat(st, held)
            or held.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) != 0o700
        ):
            return finish(AbandonedLeafObservation.CONFLICT)
        try:
            has_entry = _directory_has_any_entry(leaf_fd)
        except OSError:
            return finish(AbandonedLeafObservation.UNKNOWN)
        if has_entry:
            return finish(AbandonedLeafObservation.CONFLICT)
    except LifecycleFsError as exc:
        # `_assert_cloexec` failure (a platform-capability failure, raised
        # rather than returned), or a `finish()` cleanup failure that has
        # already closed what it could — re-raise after closing anything
        # not yet attempted.
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            raise
        _dominant_cleanup(opened, exc)
        raise
    except BaseException as exc:
        _dominant_cleanup(opened, exc)
        raise
    return AbandonedLeafOpenResult(
        AbandonedLeafObservation.EMPTY_PRIVATE_DIRECTORY,
        _AbandonedWorktreeLeaf(parent_fd=parent_fd, leaf_fd=leaf_fd, leaf_name=lifecycle_id),
    )


def open_existing_state_root(location: StateRootLocation, context: TrustedRepositoryContext) -> StateRoot | None:
    """The maintenance commands' read-only counterpart of
    `open_or_create_canonical_root` + `init_state_root` (ADR 0004
    Amendment 18, presence rows P1/P2). Never creates anything: no
    ancestor, no state-root directory, no `state-root.json`.

    Returns `None` when there is no recorded state at all -- the
    state-root path is absent, or it is an existing directory with no
    entries. A valid `state-root.json` returns an owned `StateRoot`.
    Anything else (content without a valid identity file, a partial or
    invalid identity file, an unsafe directory, a containment failure)
    raises `LifecycleFsError(SUBSTRATE_UNAVAILABLE)`; it is never
    treated as empty."""
    try:
        os.lstat(location.path)
    except FileNotFoundError:
        return None
    except OSError:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "the state-root path could not be inspected"
        ) from None

    root_fd, canonical = canonicalize_directory(location.path)
    try:
        try:
            root_stat = os.fstat(root_fd)
        except OSError:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, "the state-root directory could not be inspected"
            ) from None
        validate_safe_owned_directory_stat(root_stat)
        validate_state_root_containment(canonical, context)
        outcome = _probe_once(root_fd)
        if outcome.kind is StateRootProbe.VALID:
            return _build_state_root(root_fd, canonical, outcome.payload)
        empty = outcome.kind is StateRootProbe.ABSENT and not list_directory_entries(root_fd)
    except BaseException as exc:
        _dominant_cleanup([root_fd], exc)
        raise
    if empty:
        close_confirmed([root_fd])
        return None
    refusal = LifecycleFsError(
        LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
        "the state root has content but no valid identity file",
    )
    _dominant_cleanup([root_fd], refusal)
    raise refusal


def _build_state_root(root_fd: int, canonical_path: str, payload: dict) -> StateRoot:
    return StateRoot(root_fd=root_fd, path=canonical_path, state_root_id=payload["state_root_id"])


def _retry_after_eexist(
    root_fd: int,
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict:
    """The one legitimate concurrent-creation race (ADR 0004 Amendment
    1 section 15): this process itself observed absence, attempted
    `O_EXCL`, and lost with `EEXIST`. Only `RETRYABLE_PARTIAL` is
    retried, bounded to `_RETRY_WINDOW_SECONDS`; every other outcome
    fails immediately."""
    deadline = clock() + _RETRY_WINDOW_SECONDS
    while True:
        outcome = _probe_once(root_fd)
        if outcome.kind is StateRootProbe.VALID:
            return outcome.payload
        if outcome.kind is StateRootProbe.RETRYABLE_PARTIAL:
            if clock() >= deadline:
                raise LifecycleFsError(
                    LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                    "state-root.json initialization did not resolve within the bounded retry window",
                )
            sleeper(_RETRY_POLL_SECONDS)
            continue
        if outcome.kind is StateRootProbe.ABSENT:
            raise LifecycleFsError(
                LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
                "state-root.json disappeared during initialization",
            )
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "state-root.json is permanently invalid",
        ) from outcome.error


def _create_state_root_json_via_primitive(root_fd: int) -> dict:
    state_root_id = secrets.token_hex(16)
    payload = {"schema_version": 1, "state_root_id": state_root_id}
    data = canonical_json_dumps(payload)
    fd = open_private_create_exclusive_at(root_fd, STATE_ROOT_JSON_FILENAME, 0o600)  # FileExistsError propagates
    try:
        write_all_eintr_safe(fd, data)
        fsync_fd(fd)
    except BaseException as exc:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise cleanup_exc from exc
        raise
    else:
        close_confirmed([fd])
    fsync_fd(root_fd)
    return payload


def init_state_root(
    root_fd: int,
    canonical_root_path: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> StateRoot:
    """Run the four-state initialization probe against `state-
    root.json` beneath `root_fd` and return the resulting `StateRoot`.

    Ordinary startup encountering `RETRYABLE_PARTIAL` or
    `PERMANENTLY_INVALID` content never retries. When absent, the root
    directory is inspected before any creation attempt (ADR 0004
    section 2 step 2): an otherwise-nonempty root gets exactly one
    immediate re-probe (no sleep) and, absent a `VALID` result there,
    fails closed — an identity-less root that already holds other
    content is never adopted. The bounded 2.0s/50ms sleep window
    applies only to the one race this process can itself detect:
    having observed a genuinely empty root, attempted `O_EXCL`
    creation, and lost with `EEXIST`.

    Ownership: on success, `root_fd` becomes owned by the returned
    `StateRoot`. On failure, `root_fd` remains owned by the caller,
    which must close it — this function closes no descriptor it did
    not itself open.
    """
    outcome = _probe_once(root_fd)

    if outcome.kind is StateRootProbe.VALID:
        return _build_state_root(root_fd, canonical_root_path, outcome.payload)

    if outcome.kind is StateRootProbe.RETRYABLE_PARTIAL:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "state-root.json is empty or malformed and is never regenerated automatically",
        )

    if outcome.kind is StateRootProbe.PERMANENTLY_INVALID:
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "state-root.json is invalid and is never regenerated automatically",
        ) from outcome.error

    # ABSENT: inspect the root before ever attempting creation.
    entries = list_directory_entries(root_fd)
    if entries:
        # An otherwise-nonempty root: exactly one immediate re-probe,
        # no sleep — this is not the EEXIST race below, so no bounded
        # window applies. Only a fresh VALID result is adopted; every
        # other outcome (still ABSENT, RETRYABLE_PARTIAL, or
        # PERMANENTLY_INVALID) fails closed per ADR 0004 section 2
        # step 2's "return SUBSTRATE_UNAVAILABLE."
        reprobe = _probe_once(root_fd)
        if reprobe.kind is StateRootProbe.VALID:
            return _build_state_root(root_fd, canonical_root_path, reprobe.payload)
        raise LifecycleFsError(
            LifecycleFsFailure.SUBSTRATE_UNAVAILABLE,
            "the state root already contains other content but no valid identity file",
        )

    # Genuinely empty root: attempt O_EXCL creation via the shared
    # private-file primitive (fchmod/fstat-verified exact mode,
    # non-inheritable descriptor) rather than a raw os.open call.
    try:
        payload = _create_state_root_json_via_primitive(root_fd)
    except FileExistsError:
        payload = _retry_after_eexist(root_fd, clock=clock, sleeper=sleeper)
    return _build_state_root(root_fd, canonical_root_path, payload)
