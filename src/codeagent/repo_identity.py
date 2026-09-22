"""Trusted repository identity discovery and `repo.json` for Milestone
3 Slice 3A-1
(`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`,
Amendment 1, sections 1, 13, 14).

Implements: canonical trusted repository discovery (bare/linked-
worktree refusal via descriptor-derived identity comparison, never
`os.path.realpath` string comparison; hostile `GIT_*`-redirection-proof
via `_git_safety.run_git`; the pinned `repo_key` derivation; and the
capability-stack descriptor-cleanup pattern for the git-dir,
common-dir, and working-tree descriptors this module itself opens),
and `repo.json` creation/validation while the correct repository
`LockScope` is held, enforcing the accepted precondition across both
`repos/<repo-key>/` and `worktrees/<repo-key>/`.

Not implemented here: `lifecycle.json`, worktree/container/checkpoint-
ref attribution, or anything from Slice 3A-2 onward.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

from ._git_safety import GitSafetyError, ObjectFormat, detect_object_format, run_git
from ._lifecycle_fs import (
    REPO_JSON_MAX_BYTES,
    STORED_PATH_MAX_FS_BYTES,
    LifecycleFsError,
    LifecycleFsFailure,
    _assert_cloexec,
    _cloexec_flag,
    _nofollow_flag,
    canonical_json_dumps,
    canonical_json_loads_strict,
    canonicalize_directory,
    close_confirmed,
    fsync_fd,
    list_directory_entries,
    open_private_create_exclusive_at,
    read_all_eintr_safe,
    write_all_eintr_safe,
)
from .state_locks import LockHandle, LockKind, LockScope
from .state_root import TrustedRepositoryContext

REPO_JSON_FILENAME = "repo.json"
_REPO_KEY_DOMAIN_PREFIX = b"codeagent.repo-key.v1\x00"
_MISMATCHABLE_FIELDS = ("repo_key", "canonical_common_dir", "st_dev", "st_ino", "object_format")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


@unique
class RepoIdentityFailure(str, Enum):
    GIT_DISCOVERY_UNAVAILABLE = "git_discovery_unavailable"
    BARE_REPOSITORY_UNSUPPORTED = "bare_repository_unsupported"
    LINKED_WORKTREE_UNSUPPORTED = "linked_worktree_unsupported"
    OBJECT_FORMAT_UNAVAILABLE = "object_format_unavailable"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    WRONG_LOCK_SCOPE = "wrong_lock_scope"
    IDENTITY_MISMATCH = "identity_mismatch"
    ANOMALOUS_EXISTING_FILE = "anomalous_existing_file"
    SCHEMA_INVALID = "schema_invalid"
    NAMESPACE_NOT_EMPTY = "namespace_not_empty"


class RepoIdentityError(Exception):
    """`reason` is the stable, matchable identifier. `mismatched_fields`
    is populated only for `IDENTITY_MISMATCH`, and carries field NAMES
    only (from `_MISMATCHABLE_FIELDS`) — never the recorded or observed
    values, per ADR 0004 Amendment 1 section 13. `message` is
    sanitized, fixed categorical text only."""

    def __init__(
        self,
        reason: RepoIdentityFailure,
        message: str,
        *,
        mismatched_fields: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.mismatched_fields = mismatched_fields


@dataclass(frozen=True)
class RepositoryIdentity:
    repo_key: str
    canonical_common_dir: str
    st_dev: int
    st_ino: int
    object_format: str


def compute_repo_key(canonical_common_dir: str) -> str:
    """The pinned `repo_key` derivation (ADR 0004 Amendment 1 section
    1): `sha256("codeagent.repo-key.v1\\0" +
    os.fsencode(canonical_common_dir)).hexdigest()[:32]` — a same-host
    identity only, deliberately."""
    digest_input = _REPO_KEY_DOMAIN_PREFIX + os.fsencode(canonical_common_dir)
    return hashlib.sha256(digest_input).hexdigest()[:32]


def _run_rev_parse(repo_path, *args: str) -> str:
    try:
        result = run_git(repo_path, "rev-parse", *args)
    except GitSafetyError as exc:
        raise RepoIdentityError(
            RepoIdentityFailure.GIT_DISCOVERY_UNAVAILABLE,
            "git repository discovery could not be completed",
        ) from exc
    if result.returncode != 0:
        raise RepoIdentityError(
            RepoIdentityFailure.GIT_DISCOVERY_UNAVAILABLE,
            "git repository discovery could not be completed",
        )
    return result.stdout.strip()


def discover_repository_identity_and_context(
    repo_path: Path | str,
) -> tuple[RepositoryIdentity, TrustedRepositoryContext]:
    """Discover `repo_path`'s trusted identity and context. Every Git
    command runs through `_git_safety.run_git` (hostile `GIT_*`
    variables cannot redirect discovery). Refuses a bare repository.
    Refuses a linked-worktree input by opening and canonicalizing
    **both** the git-dir and the common-dir and comparing their
    descriptor-derived `(st_dev, st_ino)` identity — never a
    `os.path.realpath` string comparison, which is sensitive to
    symlink forms `F_GETPATH`/`fstat` are not.

    Every descriptor this function itself opens (git-dir, common-dir,
    and — for a non-bare repository — the working-tree root) is
    tracked on an explicit owned-descriptor list and closed exactly
    once, on every path — a cleanup failure dominates and chains from
    whatever discovery failure was already active, or is itself the
    primary error on the success path.
    """
    opened_fds: list[int] = []
    try:
        is_bare = _run_rev_parse(repo_path, "--is-bare-repository")
        if is_bare == "true":
            raise RepoIdentityError(
                RepoIdentityFailure.BARE_REPOSITORY_UNSUPPORTED,
                "a bare repository is not supported",
            )

        common_raw = _run_rev_parse(repo_path, "--git-common-dir")
        gitdir_raw = _run_rev_parse(repo_path, "--git-dir")
        toplevel_raw = _run_rev_parse(repo_path, "--show-toplevel")

        base = Path(repo_path)
        common_abs = common_raw if os.path.isabs(common_raw) else str((base / common_raw).resolve())
        gitdir_abs = gitdir_raw if os.path.isabs(gitdir_raw) else str((base / gitdir_raw).resolve())

        common_fd, canonical_common = canonicalize_directory(common_abs)
        opened_fds.append(common_fd)
        gitdir_fd, _canonical_gitdir = canonicalize_directory(gitdir_abs)
        opened_fds.append(gitdir_fd)

        try:
            common_st = os.fstat(common_fd)
            gitdir_st = os.fstat(gitdir_fd)
        except OSError:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "the trusted repository's git directories could not be inspected",
            ) from None

        same_directory = (common_st.st_dev, common_st.st_ino) == (gitdir_st.st_dev, gitdir_st.st_ino)
        if not same_directory:
            raise RepoIdentityError(
                RepoIdentityFailure.LINKED_WORKTREE_UNSUPPORTED,
                "a linked worktree is not supported as the trusted repository input",
            )

        canonical_working_tree: str | None = None
        if toplevel_raw:
            wt_fd, canonical_wt = canonicalize_directory(toplevel_raw)
            opened_fds.append(wt_fd)
            canonical_working_tree = canonical_wt

        try:
            object_format: ObjectFormat = detect_object_format(repo_path)
        except GitSafetyError as exc:
            raise RepoIdentityError(
                RepoIdentityFailure.OBJECT_FORMAT_UNAVAILABLE,
                "the trusted repository's git object format could not be determined",
            ) from exc

        identity = RepositoryIdentity(
            repo_key=compute_repo_key(canonical_common),
            canonical_common_dir=canonical_common,
            st_dev=common_st.st_dev,
            st_ino=common_st.st_ino,
            object_format=object_format.value,
        )
        context = TrustedRepositoryContext(
            working_tree_root=canonical_working_tree, common_dir=canonical_common
        )
    except BaseException as exc:
        if opened_fds:
            try:
                close_confirmed(opened_fds)
            except LifecycleFsError as cleanup_exc:
                raise RepoIdentityError(
                    RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                    "repository-discovery descriptors could not be confirmed closed",
                ) from exc
        raise
    else:
        if opened_fds:
            try:
                close_confirmed(opened_fds)
            except LifecycleFsError as cleanup_exc:
                raise RepoIdentityError(
                    RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                    "repository-discovery descriptors could not be confirmed closed",
                ) from cleanup_exc
        return identity, context


def _require_repository_lock(lock: LockHandle, repo_key: str) -> None:
    expected = LockScope(kind=LockKind.REPOSITORY, repo_key=repo_key)
    if not lock.is_held or lock.scope != expected:
        raise RepoIdentityError(
            RepoIdentityFailure.WRONG_LOCK_SCOPE,
            "repo.json may only be created or validated while the exact matching repository lock is held",
        )


def _is_strict_nonnegative_int(value: object) -> bool:
    # bool is a subclass of int; True/False must never be accepted as
    # a st_dev/st_ino value.
    return type(value) is int and value >= 0


def _validate_repo_json_schema(payload: object) -> dict:
    invalid = RepoIdentityError(RepoIdentityFailure.SCHEMA_INVALID, "repo.json failed schema validation")

    if not isinstance(payload, dict) or set(payload.keys()) != {
        "schema_version",
        "repo_key",
        "canonical_common_dir",
        "st_dev",
        "st_ino",
        "object_format",
    }:
        raise invalid
    schema_version = payload.get("schema_version")
    # bool is a subclass of int (True == 1); a JSON boolean must never
    # be accepted as schema_version.
    if type(schema_version) is not int or schema_version != 1:
        raise invalid

    repo_key = payload.get("repo_key")
    if not isinstance(repo_key, str) or not _HEX32_RE.match(repo_key):
        raise invalid

    common_dir = payload.get("canonical_common_dir")
    if not isinstance(common_dir, str) or not common_dir or not os.path.isabs(common_dir):
        raise invalid
    if len(os.fsencode(common_dir)) > STORED_PATH_MAX_FS_BYTES:
        raise invalid

    if not _is_strict_nonnegative_int(payload.get("st_dev")):
        raise invalid
    if not _is_strict_nonnegative_int(payload.get("st_ino")):
        raise invalid

    if payload.get("object_format") not in (ObjectFormat.SHA1.value, ObjectFormat.SHA256.value):
        raise invalid

    return payload


def _validate_existing_repo_json(fd: int, identity: RepositoryIdentity) -> RepositoryIdentity:
    try:
        _assert_cloexec(fd)
    except LifecycleFsError as exc:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json's descriptor is not non-inheritable"
        ) from exc

    try:
        st = os.fstat(fd)
    except OSError:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json could not be inspected"
        ) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json is not a regular file"
        )
    if st.st_uid != os.getuid():
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json is not owned by the current user"
        )
    if st.st_mode & 0o077:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json has unsafe permissions"
        )
    if st.st_size > REPO_JSON_MAX_BYTES:
        raise RepoIdentityError(RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json exceeds its size bound")

    try:
        data = read_all_eintr_safe(fd, REPO_JSON_MAX_BYTES)
    except LifecycleFsError as exc:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json could not be read"
        ) from exc
    try:
        payload = canonical_json_loads_strict(data, max_bytes=REPO_JSON_MAX_BYTES)
    except LifecycleFsError:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json is corrupt and is never regenerated"
        ) from None

    payload = _validate_repo_json_schema(payload)

    recorded = RepositoryIdentity(
        repo_key=payload["repo_key"],
        canonical_common_dir=payload["canonical_common_dir"],
        st_dev=payload["st_dev"],
        st_ino=payload["st_ino"],
        object_format=payload["object_format"],
    )
    mismatched = {
        field
        for field in _MISMATCHABLE_FIELDS
        if getattr(recorded, field) != getattr(identity, field)
    }
    if mismatched:
        raise RepoIdentityError(
            RepoIdentityFailure.IDENTITY_MISMATCH,
            "the repository's recorded identity does not match its freshly observed identity",
            mismatched_fields=frozenset(mismatched),
        )
    return identity


def _namespace_has_other_state(state_root, dir_fd: int, identity: RepositoryIdentity) -> bool:
    """ADR 0004 section 4's creation precondition, enforced across
    **both** `repos/<repo-key>/` and `worktrees/<repo-key>/` — the
    latter does not exist as a concept to create in 3A-1, but its
    presence must still be checked (never created) before `repo.json`
    is ever written."""
    if list_directory_entries(dir_fd):
        return True
    worktrees_fd = state_root.open_worktrees_repo_dir_if_present(identity.repo_key)
    if worktrees_fd is None:
        return False
    try:
        result = bool(list_directory_entries(worktrees_fd))
    except BaseException as exc:
        try:
            close_confirmed([worktrees_fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "the worktrees namespace descriptor could not be confirmed closed",
            ) from exc
        raise
    else:
        try:
            close_confirmed([worktrees_fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "the worktrees namespace descriptor could not be confirmed closed",
            ) from cleanup_exc
        return result


def _create_repo_json(state_root, dir_fd: int, identity: RepositoryIdentity) -> RepositoryIdentity:
    if _namespace_has_other_state(state_root, dir_fd, identity):
        raise RepoIdentityError(
            RepoIdentityFailure.NAMESPACE_NOT_EMPTY,
            "repo.json may only be created when neither repos/<repo-key>/ nor "
            "worktrees/<repo-key>/ holds any other state",
        )

    payload = {
        "schema_version": 1,
        "repo_key": identity.repo_key,
        "canonical_common_dir": identity.canonical_common_dir,
        "st_dev": identity.st_dev,
        "st_ino": identity.st_ino,
        "object_format": identity.object_format,
    }
    data = canonical_json_dumps(payload)
    if len(data) > REPO_JSON_MAX_BYTES:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json content exceeds its size bound"
        )
    try:
        fd = open_private_create_exclusive_at(dir_fd, REPO_JSON_FILENAME, 0o600)
    except FileExistsError:
        raise RepoIdentityError(
            RepoIdentityFailure.ANOMALOUS_EXISTING_FILE,
            "repo.json appeared between locked absence observation and creation, and is never adopted",
        ) from None
    except LifecycleFsError as exc:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json could not be created"
        ) from exc
    try:
        write_all_eintr_safe(fd, data)
        fsync_fd(fd)
    except LifecycleFsError as exc:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json descriptor could not be confirmed closed",
            ) from cleanup_exc
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json could not be written"
        ) from exc
    else:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json descriptor could not be confirmed closed",
            ) from cleanup_exc
    try:
        fsync_fd(dir_fd)
    except LifecycleFsError as exc:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
            "repo.json's parent directory could not be confirmed durable",
        ) from exc
    return identity


def load_or_create_repo_json(
    state_root, identity: RepositoryIdentity, repo_lock: LockHandle
) -> RepositoryIdentity:
    """Create-and-validate, or purely validate, `repo.json` while
    holding exactly the matching repository `LockScope`. A missing file
    is created only when neither `repos/<repo-key>/` nor
    `worktrees/<repo-key>/` holds other state; an existing file is
    validated against `identity`, with any mismatch raising
    `IDENTITY_MISMATCH` naming only the mismatched field names. A file
    that appears between the locked absence observation and `O_EXCL`
    creation is `ANOMALOUS_EXISTING_FILE` and is never adopted.
    """
    _require_repository_lock(repo_lock, identity.repo_key)

    dir_fd = state_root.open_repo_dir(identity.repo_key)
    try:
        result = _load_or_create_repo_json_using_dir_fd(state_root, dir_fd, identity)
    except BaseException as exc:
        try:
            close_confirmed([dir_fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json's parent directory descriptor could not be confirmed closed",
            ) from exc
        raise
    else:
        try:
            close_confirmed([dir_fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json's parent directory descriptor could not be confirmed closed",
            ) from cleanup_exc
        return result


def _load_or_create_repo_json_using_dir_fd(
    state_root, dir_fd: int, identity: RepositoryIdentity
) -> RepositoryIdentity:
    flags = os.O_RDONLY | _nofollow_flag() | _cloexec_flag()
    try:
        fd = os.open(REPO_JSON_FILENAME, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return _create_repo_json(state_root, dir_fd, identity)
    except OSError:
        raise RepoIdentityError(
            RepoIdentityFailure.SUBSTRATE_UNAVAILABLE, "repo.json could not be opened"
        ) from None

    try:
        result = _validate_existing_repo_json(fd, identity)
    except BaseException as exc:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json's descriptor could not be confirmed closed",
            ) from exc
        raise
    else:
        try:
            close_confirmed([fd])
        except LifecycleFsError as cleanup_exc:
            raise RepoIdentityError(
                RepoIdentityFailure.SUBSTRATE_UNAVAILABLE,
                "repo.json's descriptor could not be confirmed closed",
            ) from cleanup_exc
        return result
