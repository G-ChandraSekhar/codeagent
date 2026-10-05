"""Maintenance compositions for `codeagent reconcile` (ADR 0004 sections
10-12, Amendment 18): explicit reconciliation, the read-only dry run, and
abandonment.

These never run a task, never mint a lifecycle id or create a run
directory, and never initialize CodeAgent state for a repository that
has none. Abandonment records an administrative marker only: it never
deletes, adopts, rebinds, renames, or mutates a container, worktree,
registration, ref, or projection.

Presence decision (Amendment 18 rows P1-P7), made with no-create opens
before any lock could be created:
- no state root (absent or empty) -> no recorded state;
- valid root, no repository lock file, and no repository-owned state ->
  no recorded state;
- no repository lock file beside any repository-owned state -> blocked;
- lock present -> acquired with `create=False`; no namespace state -> no
  recorded state; missing/corrupt/mismatched `repo.json` beside state ->
  blocked;
- otherwise proceed.

Every acquired resource is released exactly once, in reverse order of
acquisition, outside any exception handler; a release that cannot be
confirmed is reported by stage name and makes the result blocked. No
report field carries a host path, raw exception text, Git or Docker
output, or the operator's reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum, unique

from ._git_safety import GitSafetyError, check_git_preflight
from ._lifecycle_fs import (
    LifecycleFsError,
    close_confirmed,
    list_directory_entries,
    open_existing_directory_chain_if_present,
    resolve_state_root_path,
    validate_hex32,
)
from .abandonment import (
    ABANDONMENT_TEMP_MAX,
    MARKER_FILENAME,
    AbandonmentDisposition,
    AbandonmentMarker,
    AbandonmentMarkerError,
    AbandonmentMarkerFailure,
    count_stale_marker_temps,
    load_abandonment_marker,
    publish_abandonment_marker,
    validate_reason,
)
from .lifecycle_store import (
    RUNS_DIRNAME,
    LifecycleState,
    LifecycleStoreError,
    LifecycleStoreFailure,
    is_projection_fully_absent_shape,
    load_lifecycle_projection,
)
from .reconciliation import (
    MaintenanceTrigger,
    PlanEntry,
    PlanEntryOutcome,
    ReconciliationEntryOutcome,
    ReconciliationEntryResult,
    ReconciliationError,
    RemainingResources,
    _enumerate_runs,
    inspect_remaining_resources,
    open_maintenance_trace,
    plan_repository,
    reconcile_repository,
)
from .repo_identity import (
    RepoIdentityError,
    RepoIdentityFailure,
    discover_repository_identity_and_context,
    load_existing_repo_json,
)
from .state_locks import LockError, LockFailure, acquire_lifecycle_lock, acquire_repository_lock
from .state_root import open_existing_state_root

_INVALID_REPOSITORY_REASONS = frozenset(
    {
        RepoIdentityFailure.GIT_DISCOVERY_UNAVAILABLE,
        RepoIdentityFailure.BARE_REPOSITORY_UNSUPPORTED,
        RepoIdentityFailure.LINKED_WORKTREE_UNSUPPORTED,
    }
)


@unique
class ReconcileResult(str, Enum):
    CLEAN = "CLEAN"
    UNRESOLVED_ACKNOWLEDGED = "UNRESOLVED_ACKNOWLEDGED"
    BLOCKED = "BLOCKED"


@unique
class AbandonOutcome(str, Enum):
    RECORDED_ABANDONED = "RECORDED_ABANDONED"
    RECORDED_UNRESOLVED = "RECORDED_UNRESOLVED"
    # Before any trace file: nothing recorded; at most a lifecycle.lock
    # created by the lock primitive (Amendment 18, U10).
    REFUSED = "REFUSED"
    # A trace file was created but the marker was never linked: no
    # disposition recorded; an empty trace file remains.
    NOT_RECORDED = "NOT_RECORDED"


class InvalidRepositoryError(Exception):
    """`--repo` does not name a supported repository. `reason` is the
    categorical discovery reason; the path is never included."""

    def __init__(self, reason: str) -> None:
        super().__init__("the repository argument does not name a supported Git repository")
        self.reason = reason


@dataclass(frozen=True)
class ReconcileReport:
    result: ReconcileResult
    dry_run: bool
    no_recorded_state: bool = False
    repo_key: str | None = None
    # None for a dry run (no trace, no maintenance identity) and whenever no
    # trace file was created. Set, with `trace_incomplete`, when a trace file
    # exists but the pass failed after creating it.
    maintenance_id: str | None = None
    trace_incomplete: bool = False
    entries: tuple[ReconciliationEntryResult | PlanEntry, ...] = ()
    blocked_reason: str | None = None
    mismatched_fields: frozenset[str] = frozenset()
    unconfirmed_stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class AbandonReport:
    outcome: AbandonOutcome
    lifecycle_id: str
    refusal: str | None = None
    remaining: RemainingResources | None = None
    maintenance_id: str | None = None  # set iff a trace file was created
    marker_publication: str | None = None
    unconfirmed_stages: tuple[str, ...] = ()
    mismatched_fields: frozenset[str] = frozenset()
    abandonment_temp_leftovers: int = 0


class _Releases:
    """Resources in acquisition order; released exactly once each, in
    reverse, by `release_all()`.

    Every release is attempted even if an earlier one raised anything at
    all. Any `Exception` from a release is recorded as that stage's
    categorical name (never its message). A `BaseException` that is not an
    `Exception` (an interrupt) is recorded too and handed back; the caller
    re-raises it after every release was attempted unless an exception was
    already propagating, which stays primary while the interrupted release
    is named by stage (first failure primary, later failures named; signal
    ownership is ADR 0005 / D3)."""

    def __init__(self) -> None:
        self._stack: list[tuple[str, Callable[[], None]]] = []

    def push(self, stage: str, release: Callable[[], None]) -> None:
        self._stack.append((stage, release))

    def pop_named(self, stage: str) -> list[str]:
        """Release one resource early (it must be on top of the stack). An
        interrupt raised by that release propagates after it was recorded;
        the caller's own boundary still releases everything else."""
        name, release = self._stack.pop()
        assert name == stage
        failed, interrupt = _attempt(name, release)
        if interrupt is not None:
            _attach_cleanup(interrupt, failed)
            raise interrupt
        return failed

    def release_all(self) -> tuple[list[str], BaseException | None]:
        failed: list[str] = []
        first_interrupt: BaseException | None = None
        while self._stack:
            name, release = self._stack.pop()
            stage_failed, interrupt = _attempt(name, release)
            failed.extend(stage_failed)
            if first_interrupt is None:
                first_interrupt = interrupt
        return failed, first_interrupt


def _attempt(name: str, release: Callable[[], None]) -> tuple[list[str], BaseException | None]:
    try:
        release()
    except Exception:  # noqa: BLE001 - recorded categorically; the message is never kept
        return [name], None
    except BaseException as interrupt:  # noqa: BLE001 - recorded, then re-raised by the caller
        return [name], interrupt
    return [], None


def _attach_cleanup(exc: BaseException, stages) -> None:
    """Record unconfirmed release stages on an exception that is about to
    propagate, without replacing it: categorical stage names only, as an
    attribute the CLI can read and as a PEP 678 note."""
    stages = tuple(stages)
    if not stages:
        return
    previous = getattr(exc, "codeagent_unconfirmed_stages", ())
    try:
        exc.codeagent_unconfirmed_stages = tuple(previous) + stages
    except (AttributeError, TypeError):
        pass
    exc.add_note("codeagent: cleanup unconfirmed at: " + ", ".join(stages))


def _release_and_finish(releases: _Releases, primary: BaseException | None) -> list[str]:
    """Attempt every remaining release exactly once, then re-raise the
    primary exception (preferred) or a release-time interrupt, each
    carrying the unconfirmed stage names. Otherwise return those names."""
    failed, interrupt = releases.release_all()
    if primary is not None:
        _attach_cleanup(primary, failed)
        raise primary
    if interrupt is not None:
        _attach_cleanup(interrupt, failed)
        raise interrupt
    return failed


@dataclass(frozen=True)
class _Session:
    state_root: object
    identity: object
    context: object
    repository_lock: object
    releases: _Releases


@dataclass(frozen=True)
class _Stop:
    """A presence-phase decision that ends the command before any work."""

    no_recorded_state: bool = False
    blocked_reason: str | None = None
    repo_key: str | None = None
    mismatched_fields: frozenset[str] = frozenset()
    unconfirmed_stages: tuple[str, ...] = ()


def _namespace_has_state(state_root, repo_key: str) -> bool:
    """P3/P4: does `repos/<key>/` or `worktrees/<key>/` hold any entry?
    Listing-only; creates nothing."""
    for opener in (state_root.open_repo_dir_if_present, state_root.open_worktrees_repo_dir_if_present):
        fd = opener(repo_key)
        if fd is None:
            continue
        try:
            has_entries = bool(list_directory_entries(fd))
        finally:
            close_confirmed([fd])
        if has_entries:
            return True
    return False


def _open_session(repo_path: str) -> _Session | _Stop:
    try:
        check_git_preflight()
    except GitSafetyError:
        return _Stop(blocked_reason="git_unavailable")
    try:
        identity, context = discover_repository_identity_and_context(repo_path)
    except RepoIdentityError as exc:
        if exc.reason in _INVALID_REPOSITORY_REASONS:
            raise InvalidRepositoryError(exc.reason.value) from None
        return _Stop(blocked_reason=f"repository_{exc.reason.value}")
    except (LifecycleFsError, GitSafetyError):
        return _Stop(blocked_reason="repository_substrate_unavailable")
    if context.working_tree_root is None:
        raise InvalidRepositoryError(RepoIdentityFailure.BARE_REPOSITORY_UNSUPPORTED.value)

    try:
        location = resolve_state_root_path()
        state_root = open_existing_state_root(location, context)
    except LifecycleFsError:
        return _Stop(blocked_reason="state_root_unavailable", repo_key=identity.repo_key)
    if state_root is None:
        return _Stop(no_recorded_state=True, repo_key=identity.repo_key)

    releases = _Releases()
    releases.push("state_root", state_root.close)
    # From the first acquired resource on, one boundary owns setup: every
    # exit -- a decision to stop, an expected refusal, or an unexpected
    # exception -- releases everything acquired so far exactly once.
    primary: BaseException | None = None
    decision: _Session | _Stop | None = None
    try:
        decision = _acquire_session(state_root, identity, context, releases)
    except BaseException as exc:  # noqa: BLE001 - re-raised by _release_and_finish
        primary = exc
    if primary is None and isinstance(decision, _Session):
        return decision
    stages = _release_and_finish(releases, primary)
    return replace(decision, unconfirmed_stages=tuple(stages))


def _acquire_session(state_root, identity, context, releases: _Releases) -> _Session | _Stop:
    def stop(**kwargs) -> _Stop:
        return _Stop(repo_key=identity.repo_key, **kwargs)

    try:
        repository_lock = acquire_repository_lock(state_root, identity.repo_key, create=False)
    except LockError as exc:
        if exc.reason is LockFailure.BUSY:
            return stop(blocked_reason="repository_active")
        if exc.reason is not LockFailure.ABSENT:
            return stop(blocked_reason="repository_lock_unavailable")
        try:
            has_state = _namespace_has_state(state_root, identity.repo_key)
        except LifecycleFsError:
            return stop(blocked_reason="state_root_unavailable")
        if has_state:
            return stop(blocked_reason="repository_lock_missing_with_state")
        return stop(no_recorded_state=True)
    except LifecycleFsError:
        # e.g. an unsafe `repo-locks/` (symlink, wrong owner or mode).
        return stop(blocked_reason="repository_lock_unavailable")
    releases.push("repository_lock", repository_lock.release)

    try:
        validated = load_existing_repo_json(state_root, identity, repository_lock)
    except RepoIdentityError as exc:
        if exc.reason is RepoIdentityFailure.IDENTITY_MISMATCH:
            return stop(blocked_reason="namespace_refused", mismatched_fields=exc.mismatched_fields)
        return stop(blocked_reason="namespace_refused")
    except LifecycleFsError:
        return stop(blocked_reason="namespace_refused")
    if validated is None:
        return stop(no_recorded_state=True)
    return _Session(
        state_root=state_root, identity=validated, context=context, repository_lock=repository_lock, releases=releases
    )


def _overall(entries, *, blocked: bool) -> ReconcileResult:
    if blocked:
        return ReconcileResult.BLOCKED
    unresolved = ("skipped_abandoned_unresolved",)
    if any(entry.outcome.value in unresolved for entry in entries):
        return ReconcileResult.UNRESOLVED_ACKNOWLEDGED
    return ReconcileResult.CLEAN


def run_reconcile(repo_path: str, *, dry_run: bool) -> ReconcileReport:
    """Explicit reconciliation (trigger `explicit`) or, with `dry_run`, the
    read-only planner. Raises only `InvalidRepositoryError` (or an
    unexpected exception, after every acquired resource was released)."""
    session = _open_session(repo_path)
    if isinstance(session, _Stop):
        return _report_from_stop(session, dry_run=dry_run)

    entries: tuple = ()
    inspection_stages: list[str] = []
    blocked = False
    blocked_reason = None
    maintenance_id = None
    trace_incomplete = False
    unexpected: BaseException | None = None
    try:
        kwargs = dict(
            state_root=session.state_root,
            identity=session.identity,
            context=session.context,
            repository_lock=session.repository_lock,
        )
        try:
            if dry_run:
                plan = plan_repository(**kwargs)
                entries, blocked = plan.entries, plan.blocked
                for entry in entries:
                    if entry.remaining is not None:
                        for stage in _inspection_stages(entry.remaining):
                            if stage not in inspection_stages:
                                inspection_stages.append(stage)
            else:
                result = reconcile_repository(**kwargs, trigger=MaintenanceTrigger.EXPLICIT)
                entries, blocked, maintenance_id = result.entries, result.blocked, result.maintenance_id
        except ReconciliationError as exc:
            blocked, blocked_reason = True, f"reconciliation_{exc.reason.value}"
            # Truthful trace presence: the pass's trace file may already exist.
            maintenance_id = exc.maintenance_id
            trace_incomplete = exc.maintenance_id is not None
    except BaseException as exc:  # noqa: BLE001 - every acquired resource is still released below
        unexpected = exc
    unconfirmed = tuple(inspection_stages) + tuple(_release_and_finish(session.releases, unexpected))
    result = _overall(entries, blocked=blocked or bool(unconfirmed))
    return ReconcileReport(
        result=result,
        dry_run=dry_run,
        repo_key=session.identity.repo_key,
        maintenance_id=maintenance_id,
        trace_incomplete=trace_incomplete,
        entries=tuple(entries),
        blocked_reason=blocked_reason,
        unconfirmed_stages=unconfirmed,
    )


def _report_from_stop(stop: _Stop, *, dry_run: bool) -> ReconcileReport:
    blocked = stop.blocked_reason is not None or bool(stop.unconfirmed_stages)
    return ReconcileReport(
        result=ReconcileResult.BLOCKED if blocked else ReconcileResult.CLEAN,
        dry_run=dry_run,
        no_recorded_state=stop.no_recorded_state,
        repo_key=stop.repo_key,
        blocked_reason=stop.blocked_reason,
        mismatched_fields=stop.mismatched_fields,
        unconfirmed_stages=stop.unconfirmed_stages,
    )


def _inspection_stages(remaining: RemainingResources) -> tuple[str, ...]:
    return tuple(f"inspection_{name}" for name in remaining.cleanup_unconfirmed)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_abandon(
    repo_path: str, lifecycle_id: str, *, acknowledge_unresolved: bool, reason: str | None
) -> AbandonReport:
    """Normal or forced abandonment of one lifecycle (ADR 0004 section 11,
    Amendment 18). The reason, when forced, is passed to the marker only;
    no report or trace field carries it. Raises only for an invalid
    argument, an `InvalidRepositoryError`, or an unexpected exception
    (after every acquired resource was released)."""
    validate_hex32(lifecycle_id, field_name="lifecycle_id")
    if acknowledge_unresolved:
        validate_reason(reason)
    elif reason is not None:
        raise ValueError("a reason is accepted only with acknowledge_unresolved")

    session = _open_session(repo_path)
    if isinstance(session, _Stop):
        refusal = "no_recorded_state" if session.no_recorded_state else session.blocked_reason
        if refusal is None:
            refusal = "release_unconfirmed"
        return AbandonReport(
            outcome=AbandonOutcome.REFUSED,
            lifecycle_id=lifecycle_id,
            refusal=refusal,
            unconfirmed_stages=session.unconfirmed_stages,
            mismatched_fields=session.mismatched_fields,
        )

    report: AbandonReport | None = None
    unexpected: BaseException | None = None
    try:
        report = _abandon_locked(session, lifecycle_id, acknowledge_unresolved=acknowledge_unresolved, reason=reason)
    except BaseException as exc:  # noqa: BLE001 - every acquired resource is still released below
        unexpected = exc
    unconfirmed = tuple(_release_and_finish(session.releases, unexpected))
    assert report is not None
    return _with_unconfirmed(report, unconfirmed)


def _with_unconfirmed(report: AbandonReport, stages) -> AbandonReport:
    if not stages:
        return report
    return replace(report, unconfirmed_stages=tuple(report.unconfirmed_stages) + tuple(stages))


def _abandon_locked(session: _Session, lifecycle_id: str, *, acknowledge_unresolved: bool, reason: str | None) -> AbandonReport:
    state_root, identity, releases = session.state_root, session.identity, session.releases

    def refuse(refusal: str, **extra) -> AbandonReport:
        return AbandonReport(outcome=AbandonOutcome.REFUSED, lifecycle_id=lifecycle_id, refusal=refusal, **extra)

    # Rows 1-3: the runs/ trust boundary and the target run directory.
    try:
        runs_fd = open_existing_directory_chain_if_present(state_root.root_fd, ["repos", identity.repo_key, RUNS_DIRNAME])
    except LifecycleFsError:
        return refuse("runs_unavailable")
    if runs_fd is None:
        return refuse("unknown_lifecycle")
    releases.push("runs_directory", lambda: close_confirmed([runs_fd]))
    try:
        lifecycle_ids = _enumerate_runs(runs_fd)
    except ReconciliationError as exc:
        return refuse(f"runs_{exc.reason.value}")
    if lifecycle_id not in lifecycle_ids:
        return refuse("unknown_lifecycle")
    try:
        run_dir_fd = open_existing_directory_chain_if_present(runs_fd, [lifecycle_id])
    except LifecycleFsError:
        return refuse("run_directory_unsafe")
    if run_dir_fd is None:
        return refuse("unknown_lifecycle")
    releases.push("run_directory", lambda: close_confirmed([run_dir_fd]))

    # Rows 4-7: only a final marker decides; stale temps alone never do.
    try:
        names = list_directory_entries(run_dir_fd)
    except LifecycleFsError:
        return refuse("substrate_unavailable")
    if MARKER_FILENAME in names:
        try:
            load_abandonment_marker(
                run_dir_fd,
                names=names,
                expected_lifecycle_id=lifecycle_id,
                expected_repo_key=identity.repo_key,
                expected_state_root_id=state_root.state_root_id,
            )
        except AbandonmentMarkerError:
            return refuse("marker_invalid")
        return refuse("already_abandoned")
    try:
        temps = count_stale_marker_temps(run_dir_fd, names)
    except AbandonmentMarkerError as exc:
        return refuse(
            "substrate_unavailable" if exc.reason is AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE else "marker_temp_invalid"
        )
    if temps >= ABANDONMENT_TEMP_MAX:
        return refuse("too_many_stale_abandonment_temps", abandonment_temp_leftovers=temps)

    # Row 8: the target's own lifecycle lock (an active run is never
    # abandoned). A missing lock file is created by the established safe
    # primitive -- real abandonment only, never the dry run (U10).
    try:
        lifecycle_lock = acquire_lifecycle_lock(
            run_dir_fd,
            repo_key=identity.repo_key,
            lifecycle_id=lifecycle_id,
            diagnostic_path=f"<state-root>/repos/{identity.repo_key}/{RUNS_DIRNAME}/{lifecycle_id}/lifecycle.lock",
            create=True,
        )
    except LockError as exc:
        return refuse("lifecycle_active" if exc.reason is LockFailure.BUSY else "lifecycle_lock_unavailable")
    releases.push("lifecycle_lock", lifecycle_lock.release)

    # Row 9: already clean-final.
    projection = None
    projection_status = "valid"
    try:
        projection = load_lifecycle_projection(
            run_dir_fd,
            object_format=identity.object_format,
            expected_lifecycle_id=lifecycle_id,
            expected_repo_key=identity.repo_key,
            expected_state_root_id=state_root.state_root_id,
        )
    except LifecycleStoreError as exc:
        projection_status = "unavailable" if exc.reason is LifecycleStoreFailure.SUBSTRATE_UNAVAILABLE else "invalid"
    if (
        projection is not None
        and projection.state in (LifecycleState.COMPLETE, LifecycleState.RECONCILED)
        and is_projection_fully_absent_shape(projection)
    ):
        return refuse("already_clean_final")

    # Row 10 / 10f: fresh exact inspection.
    remaining = inspect_remaining_resources(
        state_root=state_root, identity=identity, context=session.context, lifecycle_id=lifecycle_id, projection=projection
    )
    summary = remaining.to_summary()
    if remaining.cleanup_unconfirmed:
        # CodeAgent's own process/descriptor cleanup during inspection could
        # not be confirmed. That is never an "unknown resource" the operator
        # can acknowledge: both forms refuse before any trace or marker is
        # written, naming the stage (exit 4). A retry re-inspects.
        return refuse(
            "inspection_cleanup_unconfirmed",
            remaining=remaining,
            abandonment_temp_leftovers=temps,
            unconfirmed_stages=_inspection_stages(remaining),
        )
    if acknowledge_unresolved:
        if remaining.all_absent:
            return refuse("nothing_remains_use_normal_abandonment", remaining=remaining, abandonment_temp_leftovers=temps)
        disposition = AbandonmentDisposition.ABANDONED_UNRESOLVED
    else:
        if not remaining.all_absent:
            refusal = "inspection_failed" if "unknown" in summary.values() else "resources_remain"
            return refuse(refusal, remaining=remaining, abandonment_temp_leftovers=temps)
        disposition = AbandonmentDisposition.ABANDONED

    # Row 11: the trace file first, so a trace that cannot be created
    # records nothing at all.
    try:
        repo_dir_fd = state_root.open_repo_dir_if_present(identity.repo_key)
    except LifecycleFsError:
        return refuse("maintenance_trace_unavailable", remaining=remaining)
    if repo_dir_fd is None:
        return refuse("maintenance_trace_unavailable", remaining=remaining)
    releases.push("repository_directory", lambda: close_confirmed([repo_dir_fd]))
    try:
        trace = open_maintenance_trace(state_root, repo_dir_fd, identity.repo_key, trigger=MaintenanceTrigger.EXPLICIT)
    except ReconciliationError as exc:
        early = releases.pop_named("repository_directory")
        if exc.maintenance_id is None:
            # No trace file was created: nothing recorded, nothing written.
            return refuse("maintenance_trace_unavailable", remaining=remaining, unconfirmed_stages=tuple(early))
        # A trace file exists but its setup was not confirmed: no marker was
        # attempted, so no disposition is recorded (C5).
        return AbandonReport(
            outcome=AbandonOutcome.NOT_RECORDED,
            lifecycle_id=lifecycle_id,
            refusal="maintenance_trace_unconfirmed",
            remaining=remaining,
            maintenance_id=exc.maintenance_id,
            unconfirmed_stages=tuple(early),
            abandonment_temp_leftovers=temps,
        )
    early = releases.pop_named("repository_directory")
    releases.push("maintenance_trace", trace.close)

    marker = AbandonmentMarker(
        lifecycle_id=lifecycle_id,
        repo_key=identity.repo_key,
        state_root_id=state_root.state_root_id,
        disposition=disposition,
        maintenance_id=trace.maintenance_id,
        timestamp=_timestamp(),
        reason=reason if acknowledge_unresolved else None,
        remaining=summary if acknowledge_unresolved else None,
    )
    try:
        publication = publish_abandonment_marker(run_dir_fd, marker)
    except AbandonmentMarkerError as exc:
        stages = list(early)
        if exc.reason is AbandonmentMarkerFailure.TEMP_CLEANUP_UNCONFIRMED:
            stages.append("marker_temp_cleanup")
        return AbandonReport(
            outcome=AbandonOutcome.NOT_RECORDED,
            lifecycle_id=lifecycle_id,
            refusal="already_abandoned" if exc.reason is AbandonmentMarkerFailure.ALREADY_EXISTS else "marker_not_installed",
            remaining=remaining,
            maintenance_id=trace.maintenance_id,
            unconfirmed_stages=tuple(stages),
            abandonment_temp_leftovers=temps,
        )

    # The marker is installed from here on; nothing below un-records it.
    stages = list(early)
    if not publication.durable:
        stages.append("marker_directory_fsync")
    if not publication.temp_removed:
        stages.append("marker_temp_unlink")
    marker_publication = (
        "durability_unconfirmed"
        if not publication.durable
        else "temp_cleanup_unconfirmed"
        if not publication.temp_removed
        else "confirmed"
    )
    try:
        trace.abandonment_recorded(
            lifecycle_id=lifecycle_id,
            disposition=disposition,
            run_id=projection.run_id if projection is not None else None,
            projection_status=projection_status,
            remaining=summary,
            reason_recorded=acknowledge_unresolved,
            marker_publication=marker_publication,
            abandonment_temp_leftovers=temps,
        )
    except ReconciliationError:
        stages.append("maintenance_event")
    return AbandonReport(
        outcome=(
            AbandonOutcome.RECORDED_ABANDONED
            if disposition is AbandonmentDisposition.ABANDONED
            else AbandonOutcome.RECORDED_UNRESOLVED
        ),
        lifecycle_id=lifecycle_id,
        remaining=remaining,
        maintenance_id=trace.maintenance_id,
        marker_publication=marker_publication,
        unconfirmed_stages=tuple(stages),
        abandonment_temp_leftovers=temps,
    )


# Re-exported for the CLI's precedence and rendering.
__all__ = [
    "AbandonOutcome",
    "AbandonReport",
    "InvalidRepositoryError",
    "PlanEntryOutcome",
    "ReconcileReport",
    "ReconcileResult",
    "ReconciliationEntryOutcome",
    "run_abandon",
    "run_reconcile",
]
