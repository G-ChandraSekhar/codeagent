"""Internal lifecycle-aware run composition (ADR 0004 Amendment 14,
Milestone 3 Slice 3C-4).

`run_lifecycle_aware()` is the first code that composes the lifecycle
substrate into one real run: `prepare_lifecycle()`'s lease, one shared
projection cursor and its four publishers, the deterministic worktree
reservation, a publisher-mode `GitWorktree`, a lifecycle-aware
`DockerVerifier`, a publishing `CheckpointSession`, and a `RunController`
with its owner-state publisher.

**Internal boundary.** This is an underscore module that
`codeagent/__init__.py` does not export, and no bundled CLI or production
module imports or calls it (AST-pinned by its tests). Python does not
enforce privacy: a consumer that imports it leaves the supported boundary
and accepts crash windows that can block the repository, because
reconciliation still refuses several shapes a crashed real run leaves
behind and abandonment does not exist yet. No operator entry point may
call it until a checkpoint-ref reconciliation row, a worktree-plus-
container row, abandonment (ADR 0004 section 11), and ADR 0005
cancellation exist.

**Cleanup contract (Amendment 14 R1-R8, D).** Once `RunController.run()`
returns, its `_terminate()` was the only disposal attempt: the worktree is
never exited again, so nothing is mutated after `RunFinished`. Only the
reservation's descriptors and the lease are released, each exactly once
through `_attempt`, outside any exception handler. Declared release
failures are returned in `LifecycleRunResult`; every other combination,
and every raise-path cleanup failure, is either the exact in-flight
exception (when every stage is confirmed) or one sanitized
`LifecycleRunCleanupError` raised `from` its primary. Stage confirmation
and the outcome are decided from pre-dedup attribution (R8); the
cycle-safe identity dedup (D) only chooses which exception instances are
reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

from . import domain, events
from ._lifecycle_fs import LifecycleFsError
from .checkpoint_ref import CheckpointRef
from .checkpoint_session import CheckpointSession
from .controller import ApprovalProvider, Clock, EventLog, ModelClient, RunConfig, RunController
from .evidence import FilesystemEvidenceSink, _validate_containment, _validate_no_symlink_ancestors
from .executor import (
    DEFAULT_COMMAND,
    DEFAULT_IMAGE,
    DEFAULT_TIMEOUT_SECONDS,
    DockerVerifier,
    DockerVerifierLifecycleContext,
)
from .lifecycle_store import LifecycleStoreError, create_shared_lifecycle_publishers, prepare_lifecycle
from .patch import GitPatchApplier, PatchOperation
from .reader import WorktreeFileReader
from .workspace import GitWorktree

_MESSAGE_PREFIX = "lifecycle run cleanup could not be confirmed: "


@unique
class CleanupStage(str, Enum):
    """The composition-owned cleanup stages, in execution order. An
    unexpected exception escaping `GitWorktree.__exit__` is attributed to
    `WORKTREE_PHYSICAL` (the stage whose call raised it); the composition
    cannot attribute it more finely from outside that call.
    `WORKTREE_PUBLICATION` is only ever the latched `lifecycle_error`."""

    WORKTREE_PHYSICAL = "worktree_physical"
    WORKTREE_PUBLICATION = "worktree_publication"
    RESERVATION = "reservation"
    LEASE = "lease"


@dataclass(frozen=True)
class CleanupFailure:
    """One reported cleanup failure: its real stage and the exact
    exception instance (never mutated, re-chained, or wrapped).
    `declared` is True for the stage's declared failure type."""

    stage: CleanupStage
    exception: BaseException
    declared: bool


@dataclass(frozen=True)
class LifecycleRunResult:
    """The returned-path result: a `RunFinished` exists and every release
    failure was a declared one. `unconfirmed_stages` is authoritative; the
    two error fields hold only the post-dedup reported instances, so a
    `None` field never by itself means that stage was confirmed."""

    finished: events.RunFinished
    unconfirmed_stages: tuple[CleanupStage, ...]
    reservation_release_error: LifecycleFsError | None
    lease_release_error: LifecycleStoreError | None

    @property
    def release_confirmed(self) -> bool:
        return not self.unconfirmed_stages


class LifecycleRunCleanupError(Exception):
    """Sanitized cleanup-dominance error, always raised `from` its primary:
    `original` on the raise path, or the first unexpected release failure
    after `RunFinished`. The message names only `failed_stages` values;
    no retained exception's text, no path, and no Git output is ever
    included. `failures` may omit a failed stage whose exception was
    already reported under an earlier stage or is part of `original`'s
    chain; `failed_stages` never does."""

    def __init__(
        self,
        *,
        finished: events.RunFinished | None,
        original: BaseException | None,
        failures: tuple[CleanupFailure, ...],
        failed_stages: tuple[CleanupStage, ...],
    ) -> None:
        message = _MESSAGE_PREFIX + ", ".join(stage.value for stage in failed_stages)
        super().__init__(message)
        self.message = message
        self.finished = finished
        self.original = original
        self.failures = failures
        self.failed_stages = failed_stages


@dataclass(frozen=True)
class _Candidate:
    stage: CleanupStage
    exception: BaseException
    declared: bool
    # False only for an O-owned recorded field (R8): unchanged by identity
    # from the pre-call snapshot (or its stage never ran) and reachable
    # from the original exception.
    attributed: bool


def _attempt(fn, declared) -> tuple[BaseException | None, BaseException | None]:
    """Run one cleanup stage exactly once. Returns (declared failure,
    unexpected failure); never raises. Deliberately broad: the caller
    decides every outcome after all stages have run."""
    try:
        fn()
        return None, None
    except declared as exc:
        return exc, None
    except BaseException as exc:  # noqa: BLE001 - decided by R4/R5 after every stage
        return None, exc


def _reachable(root: BaseException | None, out: set[int]) -> None:
    """Add the identity of `root` and of everything reachable from it via
    `__cause__`/`__context__` to `out`. Iterative and cycle-safe: each
    object is expanded at most once."""
    stack = [root]
    while stack:
        exc = stack.pop()
        if exc is None or id(exc) in out:
            continue
        out.add(id(exc))
        stack.append(exc.__cause__)
        stack.append(exc.__context__)


def _dedup(candidates: list[_Candidate], original: BaseException | None) -> tuple[CleanupFailure, ...]:
    """D: report each distinct failure once, first occurrence in candidate
    order; exclude anything reachable from `original` or from an earlier
    reported failure. Every object stays alive in `candidates`/`original`
    for the whole call, so identities cannot be reused."""
    seen: set[int] = set()
    _reachable(original, seen)
    retained = []
    for candidate in candidates:
        if id(candidate.exception) in seen:
            continue
        retained.append(CleanupFailure(candidate.stage, candidate.exception, candidate.declared))
        _reachable(candidate.exception, seen)
    return tuple(retained)


def _unique_stages(candidates: list[_Candidate]) -> tuple[CleanupStage, ...]:
    return tuple(dict.fromkeys(c.stage for c in candidates if c.attributed))


def _validate_evidence_root(evidence_root: Path | str, source: Path, state_root_path: str) -> None:
    """The evidence sink's own checks, applied before any reservation:
    no symlink component, then bidirectional containment against the
    canonical source and the whole state root (a superset of the
    deterministic worktree location, and it keeps the artifact out of
    `runs/<id>/`). The sink repeats its own validation at capture."""
    root = Path(evidence_root)
    _validate_no_symlink_ancestors(root)
    resolved = root.resolve()
    _validate_containment(resolved, source.resolve())
    _validate_containment(resolved, Path(state_root_path).resolve())


def _field_candidate(
    candidates: list[_Candidate],
    stage: CleanupStage,
    value: BaseException | None,
    snapshot: BaseException | None,
    executed: bool,
    original_reach: set[int],
) -> None:
    if value is None:
        return
    unchanged = (not executed) or value is snapshot
    owned = unchanged and id(value) in original_reach
    candidates.append(_Candidate(stage, value, True, not owned))


def _raise_path(original, worktree, worktree_entered, reservation, lease):
    """R5: run every raise-path stage exactly once (none inside a handler),
    then attribute (R8), dedup (D), and re-raise or dominate."""
    original_reach: set[int] = set()
    _reachable(original, original_reach)
    candidates: list[_Candidate] = []

    if worktree is not None:
        snap_cleanup, snap_lifecycle = worktree.cleanup_error, worktree.lifecycle_error
        unexpected = None
        if worktree_entered:
            _, unexpected = _attempt(
                lambda: worktree.__exit__(type(original), original, original.__traceback__), ()
            )
        if unexpected is not None:
            candidates.append(_Candidate(CleanupStage.WORKTREE_PHYSICAL, unexpected, False, True))
        _field_candidate(
            candidates, CleanupStage.WORKTREE_PHYSICAL, worktree.cleanup_error, snap_cleanup,
            worktree_entered, original_reach,
        )
        _field_candidate(
            candidates, CleanupStage.WORKTREE_PUBLICATION, worktree.lifecycle_error, snap_lifecycle,
            worktree_entered, original_reach,
        )

    if reservation is not None:
        snap_reservation = reservation.cleanup_error
        _, unexpected = _attempt(
            lambda: reservation.__exit__(type(original), original, original.__traceback__), ()
        )
        if unexpected is not None:
            candidates.append(_Candidate(CleanupStage.RESERVATION, unexpected, False, True))
        _field_candidate(
            candidates, CleanupStage.RESERVATION, reservation.cleanup_error, snap_reservation,
            True, original_reach,
        )

    declared, unexpected = _attempt(lease.close, LifecycleStoreError)
    for exc, is_declared in ((declared, True), (unexpected, False)):
        if exc is not None:
            candidates.append(_Candidate(CleanupStage.LEASE, exc, is_declared, True))

    failed_stages = _unique_stages(candidates)
    if not failed_stages:
        raise original
    raise LifecycleRunCleanupError(
        finished=None,
        original=original,
        failures=_dedup(candidates, original),
        failed_stages=failed_stages,
    ) from original


def _returned_path(finished, reservation, lease) -> LifecycleRunResult:
    """R4: release the reservation's descriptors, then the lease, each
    exactly once. The worktree is never exited (R1)."""
    candidates: list[_Candidate] = []
    for stage, fn, declared_type in (
        (CleanupStage.RESERVATION, lambda: reservation.__exit__(None, None, None), LifecycleFsError),
        (CleanupStage.LEASE, lease.close, LifecycleStoreError),
    ):
        declared, unexpected = _attempt(fn, declared_type)
        for exc, is_declared in ((declared, True), (unexpected, False)):
            if exc is not None:
                candidates.append(_Candidate(stage, exc, is_declared, True))

    stages = _unique_stages(candidates)
    failures = _dedup(candidates, None)
    unexpected = [c for c in candidates if not c.declared]
    if not unexpected:
        by_stage = {f.stage: f.exception for f in failures}
        return LifecycleRunResult(
            finished=finished,
            unconfirmed_stages=stages,
            reservation_release_error=by_stage.get(CleanupStage.RESERVATION),
            lease_release_error=by_stage.get(CleanupStage.LEASE),
        )
    primary = unexpected[0].exception
    if len(candidates) == 1 and not isinstance(primary, Exception):
        raise primary
    raise LifecycleRunCleanupError(
        finished=finished, original=None, failures=failures, failed_stages=stages
    ) from primary


def run_lifecycle_aware(
    source_repo_path: Path | str,
    *,
    run_id: str,
    task_statement: str,
    approval_mode: domain.ApprovalMode,
    evidence_root: Path | str,
    patch_operations: tuple[PatchOperation, ...],
    model: ModelClient,
    approval: ApprovalProvider,
    verify_command: tuple[str, ...] = DEFAULT_COMMAND,
    image: str = DEFAULT_IMAGE,
    verify_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_repair_iterations: int = 3,
    max_plan_revisions: int = 2,
    clock: Clock | None = None,
    event_log: EventLog | None = None,
) -> LifecycleRunResult:
    """Run one lifecycle-aware run end to end. See the module docstring and
    ADR 0004 Amendment 14 for the boundary and the full cleanup contract."""
    lease = prepare_lifecycle(source_repo_path, run_id=run_id)  # self-cleans on failure

    original: BaseException | None = None
    reservation = None
    worktree = None
    worktree_entered = False
    finished = None
    try:
        writer, initial = lease.open_projection_writer()
        publishers = create_shared_lifecycle_publishers(writer, initial)
        source = Path(initial.source_repo_path)
        _validate_evidence_root(evidence_root, source, lease.state_root.path)
        reservation = lease.state_root.reserve_worktree_leaf(lease.repo_key, lease.lifecycle_id)
        reservation.__enter__()
        worktree = GitWorktree(
            source,
            run_id,
            reservation=reservation,
            worktree_publisher=publishers.worktree_publisher,
        )
        path = worktree.__enter__()
        worktree_entered = True
        controller = RunController(
            RunConfig(
                run_id=run_id,
                task_statement=task_statement,
                approval_mode=approval_mode,
                lifecycle_id=lease.lifecycle_id,
                repository_path=str(path),
                max_repair_iterations=max_repair_iterations,
                max_plan_revisions=max_plan_revisions,
            ),
            model,
            approval,
            DockerVerifier(
                path,
                image=image,
                command=verify_command,
                timeout_seconds=verify_timeout_seconds,
                clock=clock,
                lifecycle_context=DockerVerifierLifecycleContext(publisher=publishers.container_publisher),
            ),
            GitPatchApplier(path, patch_operations),
            WorktreeFileReader(path),
            worktree,
            CheckpointSession(
                CheckpointRef(source, lease.lifecycle_id),
                transition_publisher=publishers.checkpoint_ref_publisher,
            ),
            FilesystemEvidenceSink(evidence_root),
            clock=clock,
            event_log=event_log,
            lifecycle_owner=publishers.owner_publisher,
        )
        finished = controller.run()
    except BaseException as exc:
        original = exc

    if original is not None:
        _raise_path(original, worktree, worktree_entered, reservation, lease)
    return _returned_path(finished, reservation, lease)
