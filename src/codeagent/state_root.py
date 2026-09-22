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

import os
import secrets
import stat
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
    _nofollow_flag,
    canonical_json_dumps,
    canonical_json_loads_strict,
    canonicalize_directory,
    close_confirmed,
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

    def open_worktrees_repo_dir_if_present(self, repo_key: str) -> int | None:
        """Peek at (never create) `worktrees/<repo_key>/`, for
        `repo_identity.py`'s `repo.json` creation precondition (ADR
        0004 section 4: creation is permitted only when neither
        `repos/<repo-key>/` nor `worktrees/<repo-key>/` holds state).
        Returns `None` if any component of the chain does not exist."""
        validate_hex32(repo_key, field_name="repo_key")
        return open_existing_directory_chain_if_present(self.root_fd, ["worktrees", repo_key])

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
