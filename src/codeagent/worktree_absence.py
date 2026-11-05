"""Read-only confirmation that one lifecycle's deterministic worktree is
absent (ADR 0004 Amendments 13, 16 and 20).

Shared by the live owner's recreation (`_lifecycle_run`) and by
reconciliation, with no maintenance-routing semantics: the result is one of
three fixed categories, never a reconciliation outcome. It confirms exactly:

- the exact deterministic path has no Git worktree registration (a bounded,
  hardened, structurally parsed `git worktree list --porcelain -z`);
- the bounded admin scan finds zero `<lifecycle_id>[0-9]*` entries;
- the leaf name, observed descriptor-relative and no-follow, is absent.

An inspection failure is never absence. Nothing here removes anything.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, unique

from ._git_safety import GIT_TIMEOUT_SECONDS, GitSafetyError, run_git_bounded
from ._lifecycle_fs import LifecycleFsError, LifecycleFsFailure
from .state_root import AdminScanResult, MaterializedLeafObservation, scan_worktree_admin_entries

WORKTREE_LISTING_MAX_BYTES = 1_048_576

# LifecycleFsError reasons that mean "could not inspect", never "present".
SUBSTRATE_FAILURE_REASONS = (LifecycleFsFailure.SUBSTRATE_UNAVAILABLE, LifecycleFsFailure.CLEANUP_UNCONFIRMED)


class GitWorktreeListingError(Exception):
    pass


class WorktreeListingError(GitWorktreeListingError):
    """The bounded listing failed or timed out, or its output is malformed.
    Both are inspection failures."""


_HEX_RE = re.compile(rb"^[0-9a-f]+$")


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    head: str | None  # None only for a bare record
    kind: str  # "branch" | "detached" | "bare"
    branch: str | None
    locked: bool
    prunable: bool


def run_worktree_listing(
    working_tree_root: str, *, run=run_git_bounded, limit: int = WORKTREE_LISTING_MAX_BYTES
) -> bytes:
    """One bounded, timeout-controlled, no-shell, no-hooks
    `git worktree list --porcelain -z` through the shared hardened
    `run_git_bounded` seam. `-z` NUL-delimits every field, so a registered
    path containing a newline cannot be misparsed."""
    try:
        result = run(
            working_tree_root,
            "worktree",
            "list",
            "--porcelain",
            "-z",
            limit=limit,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except GitSafetyError as exc:
        raise WorktreeListingError("git worktree listing failed, timed out, or exceeded its output bound") from exc
    return result.stdout


def parse_worktree_listing(stdout: bytes, *, oid_hex_len: int | None) -> tuple[WorktreeRecord, ...]:
    """Structural parser for `git worktree list --porcelain -z` (ADR 0004
    Amendment 13). Each field is `key` or `key SP value`, NUL-terminated; a
    record ends with an empty field. Validates every record and field but
    keeps structurally valid duplicate paths: target-aware duplicate
    analysis belongs to the caller. `oid_hex_len=None` accepts either
    object-format length (legacy callers). Any violation raises
    `WorktreeListingError` -- the listing is never partially trusted."""

    def malformed() -> WorktreeListingError:
        return WorktreeListingError("git worktree listing is malformed")

    if not stdout or not stdout.endswith(b"\x00"):
        raise malformed()
    fields = stdout[:-1].split(b"\x00")
    if not fields or fields[-1] != b"":
        raise malformed()

    records: list[WorktreeRecord] = []
    current: list[bytes] = []
    for field in fields:
        if field != b"":
            current.append(field)
            continue
        if not current:
            raise malformed()  # a stray empty field
        records.append(_parse_worktree_record(current, oid_hex_len=oid_hex_len, malformed=malformed))
        current = []
    if current or not records:
        raise malformed()
    return tuple(records)


def _parse_worktree_record(fields: list[bytes], *, oid_hex_len: int | None, malformed) -> WorktreeRecord:
    seen: dict[bytes, bytes | None] = {}
    for index, field in enumerate(fields):
        key, sep, value = field.partition(b" ")
        if key in seen:
            raise malformed()
        if key == b"worktree":
            if index != 0 or not sep or not value.startswith(b"/"):
                raise malformed()
        elif key in (b"HEAD", b"branch"):
            if not sep or not value:
                raise malformed()
        elif key in (b"detached", b"bare"):
            if sep:
                raise malformed()
        elif key in (b"locked", b"prunable"):
            pass  # with or without a reason; the reason is never retained
        else:
            raise malformed()  # unknown key
        seen[key] = value if sep else None
    if b"worktree" not in seen:
        raise malformed()
    kinds = [k for k in (b"branch", b"detached", b"bare") if k in seen]
    if len(kinds) != 1:
        raise malformed()
    kind = kinds[0].decode()
    head = seen.get(b"HEAD")
    if kind == "bare":
        if head is not None:
            raise malformed()
    else:
        if head is None or not _HEX_RE.fullmatch(head):
            raise malformed()
        if oid_hex_len is None:
            if len(head) not in (40, 64):
                raise malformed()
        elif len(head) != oid_hex_len:
            raise malformed()
    branch = seen.get(b"branch")
    return WorktreeRecord(
        path=os.path.normpath(os.fsdecode(seen[b"worktree"])),
        head=head.decode() if head is not None else None,
        kind=kind,
        branch=os.fsdecode(branch) if branch is not None else None,
        locked=b"locked" in seen,
        prunable=b"prunable" in seen,
    )


def registered_paths_from_listing(stdout: bytes) -> set[str]:
    """Every registered path in one listing. With no authorized target
    exception, ANY duplicate normalized path makes the listing malformed."""
    paths: set[str] = set()
    for record in parse_worktree_listing(stdout, oid_hex_len=None):
        if record.path in paths:
            raise WorktreeListingError("git worktree listing repeats a path")
        paths.add(record.path)
    return paths


def worktree_registered_paths(working_tree_root: str) -> set[str]:
    return registered_paths_from_listing(run_worktree_listing(working_tree_root))


@unique
class Absence(str, Enum):
    """`CONFIRMED` only on positive proof; `PRESENT` when something was
    positively observed; `UNAVAILABLE` when an inspection could not complete."""

    CONFIRMED = "confirmed"
    PRESENT = "present"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AbsenceObservation:
    result: Absence
    detail: str | None  # a fixed categorical sentence; None only when CONFIRMED


_CONFIRMED = AbsenceObservation(Absence.CONFIRMED, None)

ADMIN_SCAN_OBSERVATIONS = {
    AdminScanResult.MATCH_FOUND: AbsenceObservation(
        Absence.PRESENT, "a git worktree admin entry exists for this lifecycle"
    ),
    AdminScanResult.UNEXPECTED_SUBSTRATE: AbsenceObservation(
        Absence.PRESENT, "the git worktree admin directory is not a real directory"
    ),
    AdminScanResult.LIMIT_EXCEEDED: AbsenceObservation(
        Absence.UNAVAILABLE, "the git worktree admin directory exceeded its scan bound"
    ),
    AdminScanResult.INSPECTION_FAILED: AbsenceObservation(
        Absence.UNAVAILABLE, "the git worktree admin directory could not be inspected"
    ),
}


def _fs_category(exc: LifecycleFsError) -> Absence:
    return Absence.UNAVAILABLE if exc.reason in SUBSTRATE_FAILURE_REASONS else Absence.PRESENT


def observe_unregistered(
    *,
    lifecycle_id: str,
    expected_path: str,
    canonical_common_dir: str,
    working_tree_root: str,
    registered_paths: Callable[[str], set[str]] | None = None,
    admin_scan: Callable[[str, str], AdminScanResult] | None = None,
) -> AbsenceObservation:
    """A fresh bounded listing plus a fresh bounded admin scan. `CONFIRMED`
    only when the exact path is unregistered and no admin entry exists. The
    two injectable primitives default to this module's own; reconciliation
    passes its long-standing seams."""
    registered_paths = registered_paths or worktree_registered_paths
    admin_scan = admin_scan or scan_worktree_admin_entries
    try:
        paths = registered_paths(working_tree_root)
    except GitWorktreeListingError:
        return AbsenceObservation(Absence.UNAVAILABLE, "worktree listing failed")
    if expected_path in paths:
        return AbsenceObservation(Absence.PRESENT, "the recomputed worktree is registered")
    try:
        scan = admin_scan(canonical_common_dir, lifecycle_id)
    except LifecycleFsError as exc:
        return AbsenceObservation(_fs_category(exc), "the git worktree admin scan could not be completed")
    if scan is AdminScanResult.NONE_FOUND:
        return _CONFIRMED
    return ADMIN_SCAN_OBSERVATIONS[scan]


def observe_worktree_absent(
    *,
    lifecycle_id: str,
    state_root,
    identity,
    context,
    registered_paths: Callable[[str], set[str]] | None = None,
    admin_scan: Callable[[str, str], AdminScanResult] | None = None,
) -> AbsenceObservation:
    """The full three-part confirmation: unregistered, zero admin entries,
    and the leaf name absent by a descriptor-relative no-follow observation.
    `os.path.lexists` is never used: it reports any `OSError` (for example an
    unreadable parent) as absence."""
    expected_path = os.path.normpath(os.path.join(state_root.path, "worktrees", identity.repo_key, lifecycle_id))
    observation = observe_unregistered(
        lifecycle_id=lifecycle_id,
        expected_path=expected_path,
        canonical_common_dir=identity.canonical_common_dir,
        working_tree_root=context.working_tree_root,
        registered_paths=registered_paths,
        admin_scan=admin_scan,
    )
    if observation.result is not Absence.CONFIRMED:
        return observation
    try:
        leaf = state_root.observe_materialized_worktree_leaf(identity.repo_key, lifecycle_id)
    except LifecycleFsError as exc:
        return AbsenceObservation(_fs_category(exc), "the worktree leaf could not be inspected")
    if leaf is MaterializedLeafObservation.UNKNOWN:
        return AbsenceObservation(Absence.UNAVAILABLE, "the worktree leaf could not be inspected")
    if leaf is not MaterializedLeafObservation.ABSENT:
        return AbsenceObservation(Absence.PRESENT, "the recomputed worktree leaf is present")
    return _CONFIRMED
