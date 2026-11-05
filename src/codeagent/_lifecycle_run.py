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

**Cancellation (ADR 0005 Amendment 1).** `cancellation` is a read-only
token passed to the `DockerVerifier` and the `RunController`. This module
installs no signal handler: `cli.owned_cancellation()` is the only
production owner, entered around a run dispatch starting with D6 (`solve`);
in D3 only a test-only driver enters it around this function. A
cancellation requested during `prepare_lifecycle()` or composition setup
only sets the flag and is first observed at the controller's first check,
so the worktree may be materialized and then disposed by ordinary teardown.

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

**Deferred interrupt (ADR 0005 Amendment 1 §12).** When the controller
recorded a deferred interrupt, every outcome preserves that exact instance
without changing the precedence above: `LifecycleRunResult.deferred_interrupt`
and `LifecycleRunCleanupError.deferred_interrupt` (never in a `repr`, equality
or message), or, on an exception that surfaces raw, the best-effort
`codeagent_deferred_interrupt` attribute (an exception type that refuses
attributes cannot carry it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from pathlib import Path

from . import domain, events
from ._lifecycle_fs import LifecycleFsError
from .cancellation import DEFERRED_INTERRUPT_ATTRIBUTE, NEVER_CANCELLED, CancellationToken, set_evidence_attribute
from .checkpoint_ref import CheckpointRef
from .checkpoint_session import CheckpointIntent, CheckpointSession
from .container_lifecycle import ContainerIntent
from .controller import ApprovalProvider, Clock, EventLog, ModelClient, RecreationResult, RunConfig, RunController
from .errors import ErrorCode, OperationalError
from .evidence import FilesystemEvidenceSink, _validate_containment, _validate_no_symlink_ancestors
from .executor import (
    DEFAULT_COMMAND,
    DEFAULT_IMAGE,
    DEFAULT_TIMEOUT_SECONDS,
    DockerVerifier,
    DockerVerifierLifecycleContext,
)
from .lifecycle_store import LifecycleState, LifecycleStoreError, create_shared_lifecycle_publishers, prepare_lifecycle
from .patch import GitPatchApplier
from .worktree_absence import Absence, observe_worktree_absent
from .repo_tools import RepoTools
from .state_root import MaterializedLeafObservation
from .workspace import GitWorktree
from .worktree_lifecycle import ABSENT_WORKTREE_TRANSITION, WorktreeIntent, WorktreePublicationFailure

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
    # ADR 0005 Amendment 1 §12: an exit-style interrupt the controller
    # recorded as deferred evidence (`RunController.deferred_interrupt`). The
    # exact instance; never in `repr` or equality (its text is never
    # exposed); a non-`Exception` only.
    deferred_interrupt: BaseException | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_deferred_interrupt(self.deferred_interrupt)

    @property
    def release_confirmed(self) -> bool:
        return not self.unconfirmed_stages


def _require_deferred_interrupt(value) -> None:
    if value is not None and (not isinstance(value, BaseException) or isinstance(value, Exception)):
        raise ValueError("deferred_interrupt must be a BaseException that is not an Exception")


def _carry_deferred(exc: BaseException, deferred: BaseException | None) -> None:
    """Preserve the controller's deferred interrupt on an exception that
    surfaces raw (Amendment 14 keeps it primary): best effort, through the
    stable `codeagent_deferred_interrupt` attribute, never through text or
    the cause chain. Skipped when `exc` is that interrupt itself."""
    if deferred is not None and exc is not deferred:
        set_evidence_attribute(exc, DEFERRED_INTERRUPT_ATTRIBUTE, deferred)


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
        deferred_interrupt: BaseException | None = None,
    ) -> None:
        _require_deferred_interrupt(deferred_interrupt)
        message = _MESSAGE_PREFIX + ", ".join(stage.value for stage in failed_stages)
        super().__init__(message)
        self.message = message
        self.finished = finished
        self.original = original
        self.failures = failures
        self.failed_stages = failed_stages
        # The controller's deferred interrupt (ADR 0005 Amendment 1 §12), by
        # identity; never part of the message.
        self.deferred_interrupt = deferred_interrupt


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


def _raise_path(original, worktree, worktree_entered, reservation, lease, deferred_interrupt=None, earlier=()):
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

    # Earlier reservations closed inside a recreation (Amendment 20): their
    # close failures are reported once here and never retried.
    for exc in earlier:
        candidates.append(_Candidate(CleanupStage.RESERVATION, exc, isinstance(exc, LifecycleFsError), True))

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
        _carry_deferred(original, deferred_interrupt)
        raise original
    raise LifecycleRunCleanupError(
        finished=None,
        original=original,
        failures=_dedup(candidates, original),
        failed_stages=failed_stages,
        deferred_interrupt=deferred_interrupt,
    ) from original


def _returned_path(finished, reservation, lease, deferred_interrupt=None, earlier=()) -> LifecycleRunResult:
    """R4: release the current reservation's descriptors (if a reservation is
    current), then the lease, each exactly once. The worktree is never
    exited (R1). Earlier reservations' close failures, already attempted
    once inside a recreation (Amendment 20), are reported under
    RESERVATION and never retried."""
    candidates: list[_Candidate] = [
        _Candidate(CleanupStage.RESERVATION, exc, isinstance(exc, LifecycleFsError), True) for exc in earlier
    ]
    releases = []
    if reservation is not None:
        releases.append((CleanupStage.RESERVATION, lambda: reservation.__exit__(None, None, None), LifecycleFsError))
    releases.append((CleanupStage.LEASE, lease.close, LifecycleStoreError))
    for stage, fn, declared_type in releases:
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
            deferred_interrupt=deferred_interrupt,
        )
    primary = unexpected[0].exception
    if len(candidates) == 1 and not isinstance(primary, Exception):
        # Amendment 14: a lone release-time interrupt stays primary; the
        # earlier deferred interrupt is carried on it, never erased.
        _carry_deferred(primary, deferred_interrupt)
        raise primary
    raise LifecycleRunCleanupError(
        finished=finished, original=None, failures=failures, failed_stages=stages,
        deferred_interrupt=deferred_interrupt,
    ) from primary


# ---------------------------------------------------------------------------
# ADR 0004 Amendment 20: the composition-owned workspace recreator (D5/C-1
# Step 7). The controller decides and records; this object performs the
# attempt and tracks which incarnation and reservation are current, so the
# Amendment 14 R1/R2 cleanup always acts on the objects current when
# `run()` returns or raises. `RunController._recreation_can_continue()` is
# still False in production (N4), so a real run never reaches it yet.
# ---------------------------------------------------------------------------

RECREATION_FORMAT_BOUND = 99
_RECREATION_FAILED_MESSAGE = "the worktree could not be recreated at the last accepted checkpoint"
_EVIDENCE_CAPTURE_FAILED_MESSAGE = "the recreation evidence capture failed unexpectedly"
_O = events.RecreationOutcome


def _reserve_worktree_leaf(lease):
    """The one production reservation call: every incarnation's leaf, at
    the same deterministic path, carrying the lease's validated
    `canonical_common_dir` (ADR 0004 Amendment 20 D-2(a), Phase R)."""
    return lease.state_root.reserve_worktree_leaf(
        lease.repo_key,
        lease.lifecycle_id,
        canonical_common_dir=lease.identity.canonical_common_dir,
    )


def _call(fn):
    """Call `fn` once through `_attempt` (so this module keeps T39/H1's single
    handler shape). Returns `(value, exception)`; an ordinary exception is
    returned, and a non-`Exception` `BaseException` is re-raised here,
    outside any handler, so an interrupt is never swallowed."""
    box: list = []
    _, exc = _attempt(lambda: box.append(fn()), ())
    if exc is not None and not isinstance(exc, Exception):
        raise exc
    return (box[0] if box else None), exc


class _CompositionRecreator:
    """Implements `controller.WorkspaceRecreator` for one composed run."""

    def __init__(
        self,
        *,
        lease,
        publishers,
        sink: FilesystemEvidenceSink,
        session: CheckpointSession,
        checkpoint_ref: CheckpointRef,
        source: Path,
        run_id: str,
        worktree: GitWorktree,
        reservation,
        max_recreations: int,
    ) -> None:
        self._lease = lease
        self._publisher = publishers.worktree_publisher
        self._sink = sink
        self._session = session
        self._ref = checkpoint_ref
        self._source = source
        self._run_id = run_id
        self._budget = max_recreations
        # The current objects (Amendment 14 R2, revised by Amendment 20).
        self.worktree: GitWorktree | None = worktree
        self.worktree_entered = True
        self.reservation = reservation
        # Earlier reservations' close failures: retained, reported once under
        # RESERVATION, never retried.
        self.earlier_reservation_failures: list[BaseException] = []
        self._minted = 0
        self._pending: tuple[int, str] | None = None

    # -- begin(): Q2-Q6, no mutation; mint only once every gate holds ------

    def begin(self, *, trigger_code: ErrorCode, checkpoint_id: str | None) -> int | None:
        if self._pending is not None:
            raise RuntimeError("a minted recreation sequence has not been used")
        commit = self._gates(checkpoint_id)
        if commit is None:
            return None
        self._minted += 1
        self._pending = (self._minted, commit)
        return self._minted

    def _gates(self, checkpoint_id: str | None) -> str | None:
        # Q2: the session is PRESENT; C is its accepted SHA and the controller's.
        if self._session.intent is not CheckpointIntent.PRESENT:
            return None
        commit = self._session.accepted_sha
        if not isinstance(commit, str) or checkpoint_id != commit:
            return None
        worktree, reservation = self.worktree, self.reservation
        if worktree is None or reservation is None or not self.worktree_entered:
            return None
        # Q3 (the verifier part is the controller's): the cursor's projection.
        projection = self._publisher.current
        if (
            projection.state is not LifecycleState.ACTIVE
            or projection.failure is not None
            or any(
                record.intent is not ContainerIntent.ABSENT or record.id is not None
                for record in (projection.baseline, projection.verification)
            )
            or projection.checkpoint_ref.intent is not CheckpointIntent.PRESENT
            or projection.checkpoint_ref.accepted_sha != commit
            or projection.worktree.intent is not WorktreeIntent.PRESENT
            or projection.worktree.expected_head != worktree.initial_commit
        ):
            return None
        # Q4: the current incarnation is active and its reservation consumed and open.
        if (
            worktree.disposition != "active"
            or worktree.lifecycle_error is not None
            or worktree.path is None
            or not reservation.consumed_and_open
        ):
            return None
        # Q5: a fresh no-deref observation of the owned ref equals C; a failed
        # observation refuses.
        observed, exc = _call(self._ref.observe)
        if exc is not None or not observed.present or observed.oid != commit:
            return None
        # Q6: the format bound and the configured budget; checked, not minted.
        if self._minted >= RECREATION_FORMAT_BOUND or self._minted >= self._budget:
            return None
        return commit

    # -- recreate(k): Phases E, D, R, C ------------------------------------

    def recreate(self, sequence: int) -> RecreationResult:
        if type(sequence) is not int or self._pending is None or self._pending[0] != sequence:
            raise RuntimeError("recreate() accepts only the sequence begin() just minted")
        _, commit = self._pending
        self._pending = None
        old = self.worktree
        assert old is not None

        # Phase E: a confirmed complete artifact before any lifecycle mutation.
        receipt, exc = _call(
            lambda: self._sink.capture(
                worktree_path=old.path,
                source_repo_path=self._source,
                initial_commit=commit,
                lifecycle_id=self._lease.lifecycle_id,
                recreation_sequence=sequence,
                object_format=self._lease.object_format,
            )
        )
        if exc is not None:
            return RecreationResult(
                outcome=_O.EVIDENCE_NOT_CONFIRMED,
                error=OperationalError(
                    code=ErrorCode.EVIDENCE_CAPTURE_FAILED,
                    error_id=f"{self._run_id}-recreation-{sequence}-evidence",
                    message=_EVIDENCE_CAPTURE_FAILED_MESSAGE,
                ),
            )
        artifact = {}
        if receipt.artifact_id is not None:
            artifact = dict(
                artifact_id=receipt.artifact_id,
                sha256_payload=receipt.sha256_payload,
                bytes_written=receipt.bytes_written,
                evidence_complete=receipt.complete,
            )
        if not (receipt.success and receipt.complete and receipt.error is None):
            return RecreationResult(outcome=_O.EVIDENCE_NOT_CONFIRMED, error=receipt.error, **artifact)

        def failed(outcome, workspace=None) -> RecreationResult:
            return RecreationResult(
                outcome=outcome,
                workspace=workspace,
                error=OperationalError(
                    code=ErrorCode.WORKSPACE_RECREATION_FAILED,
                    error_id=f"{self._run_id}-recreation-{sequence}",
                    message=_RECREATION_FAILED_MESSAGE,
                ),
                **artifact,
            )

        # Phase D: dispose incarnation n (disposing -> removal -> registration,
        # admin entries and leaf confirmed absent -> absent), then close its
        # reservation exactly once. A disposal failure leaves n current.
        _, exc = _call(old.dispose)
        if exc is not None:
            return failed(_O.DISPOSAL_FAILED)
        if not self._close_current_reservation():
            return failed(_O.FAILED_NOTHING_OWNED)

        # Phase R: one fresh reservation at the same deterministic path.
        fresh, exc = _call(
            lambda: _reserve_worktree_leaf(self._lease)
        )
        if isinstance(exc, FileExistsError):
            return failed(_O.FAILED_NOTHING_OWNED)  # a foreign entry, never touched
        if exc is not None:
            return failed(_O.FAILED_NOTHING_OWNED if self._leaf_absent() else _O.UNCONFIRMED)
        self.reservation = fresh

        # Phase C: create incarnation n+1 at C.
        candidate, exc = _call(
            lambda: GitWorktree(
                self._source,
                self._run_id,
                reservation=fresh,
                worktree_publisher=self._publisher,
                materialize_commit=commit,
            )
        )
        if exc is not None:
            return self._unwind_unentered(None, failed)
        self.worktree, self.worktree_entered = candidate, False
        _, exc = _call(candidate.__enter__)
        if exc is not None:
            return self._unwind_unentered(candidate, failed)
        self.worktree_entered = True
        _, exc = _call(lambda: candidate.entry_gate(commit))
        if exc is not None:
            return failed(_O.ADOPTED_THEN_FAILED, candidate)  # fully owned; teardown disposes it
        return RecreationResult(outcome=_O.RECREATED, workspace=candidate, **artifact)

    def _unwind_unentered(self, candidate: GitWorktree | None, failed) -> RecreationResult:
        """A Phase C failure before n+1 was entered (section 7)."""
        error = candidate.lifecycle_error if candidate is not None else None
        if error is not None and error.transition is WorktreeIntent.PRESENT:
            # A registered, materialized worktree is retained under a consumed
            # reservation; both stay current for the final cleanup.
            return failed(_O.UNCONFIRMED)
        if error is not None and not (
            error.transition is WorktreeIntent.CREATING and error.reason is WorktreePublicationFailure.NOT_INSTALLED
        ):
            self._close_current_reservation()
            return failed(_O.UNCONFIRMED)  # a `creating` record may be installed
        creating_installed = self._publisher.current.worktree.intent is WorktreeIntent.CREATING
        if candidate is not None and candidate.cleanup_error is not None:
            self._close_current_reservation()
            return failed(_O.UNCONFIRMED)
        if not self._close_current_reservation():
            return failed(_O.UNCONFIRMED)
        if not creating_installed:
            return failed(_O.FAILED_NOTHING_OWNED)
        # `creating(C)` is installed: publish the caller-confirmed
        # `creating -> absent` only after a fresh registration, admin-entry and
        # leaf absence confirmation, never because Git reported a failure.
        confirmation, exc = _call(
            lambda: observe_worktree_absent(
                lifecycle_id=self._lease.lifecycle_id,
                state_root=self._lease.state_root,
                identity=self._lease.identity,
                context=self._lease.context,
            )
        )
        if exc is not None or confirmation.result is not Absence.CONFIRMED:
            return failed(_O.UNCONFIRMED)
        _, exc = _call(lambda: self._publisher.publish(ABSENT_WORKTREE_TRANSITION))
        if exc is not None:
            return failed(_O.UNCONFIRMED)
        return failed(_O.FAILED_NOTHING_OWNED)

    def _close_current_reservation(self) -> bool:
        """Close the current reservation exactly once. It stops being current
        before the call, so nothing can close it again. Every failure,
        including a non-`Exception` `BaseException`, is retained as the exact
        instance for the composition's R2 accounting and never retried or
        re-raised here: like any other cleanup stage it goes through
        `_attempt`, not `_call`."""
        reservation, self.reservation = self.reservation, None
        if reservation is None:
            return True
        _, exc = _attempt(lambda: reservation.__exit__(None, None, None), ())
        if exc is not None:
            self.earlier_reservation_failures.append(exc)
            return False
        return True

    def _leaf_absent(self) -> bool:
        observation, exc = _call(
            lambda: self._lease.state_root.observe_materialized_worktree_leaf(
                self._lease.repo_key, self._lease.lifecycle_id
            )
        )
        return exc is None and observation is MaterializedLeafObservation.ABSENT

def run_lifecycle_aware(
    source_repo_path: Path | str,
    *,
    run_id: str,
    task_statement: str,
    approval_mode: domain.ApprovalMode,
    evidence_root: Path | str,
    model: ModelClient,
    approval: ApprovalProvider,
    verify_command: tuple[str, ...] = DEFAULT_COMMAND,
    image: str = DEFAULT_IMAGE,
    verify_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_repair_iterations: int = 3,
    max_plan_revisions: int = 2,
    clock: Clock | None = None,
    event_log: EventLog | None = None,
    cancellation: CancellationToken = NEVER_CANCELLED,
    max_recreations: int = 3,
) -> LifecycleRunResult:
    """Run one lifecycle-aware run end to end. See the module docstring and
    ADR 0004 Amendment 14 for the boundary and the full cleanup contract.
    `max_recreations` is the recreation budget (ADR 0004 Amendment 20 Q6),
    an integer from 0 to 99, independent of the 99-member format bound."""
    if type(max_recreations) is not int or not 0 <= max_recreations <= RECREATION_FORMAT_BOUND:
        raise ValueError("max_recreations must be an integer from 0 to 99")
    lease = prepare_lifecycle(source_repo_path, run_id=run_id)  # self-cleans on failure

    original: BaseException | None = None
    controller = None
    recreator = None
    reservation = None
    worktree = None
    worktree_entered = False
    finished = None
    try:
        writer, initial = lease.open_projection_writer()
        publishers = create_shared_lifecycle_publishers(writer, initial)
        source = Path(initial.source_repo_path)
        _validate_evidence_root(evidence_root, source, lease.state_root.path)
        reservation = _reserve_worktree_leaf(lease)
        reservation.__enter__()
        worktree = GitWorktree(
            source,
            run_id,
            reservation=reservation,
            worktree_publisher=publishers.worktree_publisher,
        )
        path = worktree.__enter__()
        worktree_entered = True
        sink = FilesystemEvidenceSink(evidence_root)
        checkpoint_ref = CheckpointRef(source, lease.lifecycle_id)
        session = CheckpointSession(checkpoint_ref, transition_publisher=publishers.checkpoint_ref_publisher)
        # From here the recreator tracks the current incarnation and
        # reservation (ADR 0004 Amendment 20; Amendment 14 R2 as revised).
        recreator = _CompositionRecreator(
            lease=lease,
            publishers=publishers,
            sink=sink,
            session=session,
            checkpoint_ref=checkpoint_ref,
            source=source,
            run_id=run_id,
            worktree=worktree,
            reservation=reservation,
            max_recreations=max_recreations,
        )
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
                cancellation=cancellation,
            ),
            GitPatchApplier(path),
            RepoTools(path),
            worktree,
            session,
            sink,
            clock=clock,
            event_log=event_log,
            lifecycle_owner=publishers.owner_publisher,
            cancellation=cancellation,
            workspace_recreator=recreator,
        )
        finished = controller.run()
    except BaseException as exc:
        original = exc

    earlier: tuple[BaseException, ...] = ()
    if recreator is not None:
        worktree, worktree_entered = recreator.worktree, recreator.worktree_entered
        reservation, earlier = recreator.reservation, tuple(recreator.earlier_reservation_failures)
    deferred = controller.deferred_interrupt if controller is not None else None
    if original is not None:
        _raise_path(original, worktree, worktree_entered, reservation, lease, deferred, earlier)
    return _returned_path(finished, reservation, lease, deferred, earlier)
