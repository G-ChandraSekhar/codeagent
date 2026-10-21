"""Milestone 1: a deterministic run controller.

This is production orchestration code — it depends only on narrow
Protocols (`ModelClient`, `ApprovalProvider`, `Verifier`, `PatchApplier`,
`Clock`), never on any concrete test double. Deterministic fakes
implementing those Protocols live in `tests/support/fakes.py`, not
here. `codeagent.patch.GitPatchApplier` and `codeagent.workspace.GitWorktree`
are real (non-fake) implementations of part of this seam, added in
slice B.

Scope so far:
- Slice A: exercises domain.py's state machine, events.py's schemas,
  and errors.py's taxonomy together, end-to-end, through one real
  callable code path — with every collaborator faked.
- Slice B: `PatchApplier` can now be a real implementation
  (`codeagent.patch.GitPatchApplier`) that validates and applies one
  controlled patch operation inside a real, disposable `git worktree`
  (`codeagent.workspace.GitWorktree`) and commits it as the checkpoint.
  `RunController` itself is unchanged by this — it already only ever
  used `PatchResult`'s fields, never invented data.

This still isn't full Milestone 1 completion: no Docker verification,
no live model provider, no report generation (slice C).

Not implemented here: a process executor, a model provider adapter,
redaction, persistence (the `EventLog` below is in-memory only, not the
real EventSink), or a *considered* retry/abort/escalation policy for
operational errors — a controller now exists, but it has no such policy
yet; this controller's placeholder is documented at the one place it
applies (`_dispatch_apply_patch`).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from codeagent import domain, events
from codeagent.cancellation import NEVER_CANCELLED, CancellationToken, OperationCancelled, attach_cleanup_failures
from codeagent.checkpoint_ref import (
    LIFECYCLE_ID_RE,
    CheckpointRefError,
    CheckpointRefFailure,
    MutationOutcome,
)
from codeagent.checkpoint_session import CheckpointIntent, CheckpointPublicationError, CheckpointSessionError
from codeagent.errors import ErrorCode, OperationalError
from codeagent.evidence import EvidenceReceipt, EvidenceSink
from codeagent.lifecycle_owner import OwnerStatePublicationError
from codeagent.model_protocol import (
    BASELINE_MAX_BYTES,
    BASELINE_TAIL_BYTES,
    CONTEXT_CAP_BYTES,
    CONTEXT_ENVELOPE_MAX_BYTES,
    DROPPABLE_KINDS,
    MALFORMED_CHARGE_CAP,
    REVISION_ITEM_MAX_BYTES,
    TOOL_RESULT_ITEM_MAX_BYTES,
    VERIFICATION_ITEM_MAX_BYTES,
    VERIFICATION_TAIL_BYTES,
    Classified,
    FeedbackItem,
    FeedbackKind,
    ModelClient,
    ModelContext,
    ParsedCall,
    classify_turn,
    encoded_tail,
    valid_model_name,
)
from codeagent.tool_schema import (
    MAX_NOTE_BYTES,
    ArgumentRejection,
    ListDirectoryArgs,
    PatchOperation,
    PlanDigests,
    ProposedChange,
    ProposePlanArgs,
    ReadFileArgs,
    SearchTextArgs,
    ToolCleanupUnconfirmedError,
    ToolOutcome,
    canonical_json_bytes,
    compute_digests,
    is_meaningful_note,
    redact_arguments,
    redact_invalid_arguments,
    sanitize_recorded_text,
)


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_nonnegative_int(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


# Ledger D4b (readiness addendum R4-J2): a plan is exactly the strictly
# parsed, immutable `propose_plan` arguments; `tool_schema.compute_digests`
# is the only digest implementation.
PlanProposal = ProposePlanArgs


@dataclass(frozen=True)
class PatchResult:
    """What actually happened when a PatchApplier ran — RunController
    only ever reports these fields; it never invents a checkpoint id,
    diff size, or changed-file list itself.

    Invariants (enforced in __post_init__, not just documented): a
    successful result has a real commit hash, a positive operation
    count, at least one changed path, a positive diff size, and no
    error. A failed result has none of that success-shaped metadata —
    only an error.
    """

    success: bool
    operation_count: int
    changed_paths: tuple[str, ...]
    diff_bytes: int
    commit_hash: str | None = None
    error: OperationalError | None = None

    def __post_init__(self) -> None:
        if self.success:
            if not self.commit_hash:
                raise ValueError("a successful PatchResult must have a nonempty commit_hash")
            if self.operation_count <= 0:
                raise ValueError("a successful PatchResult must have operation_count > 0")
            if not self.changed_paths:
                raise ValueError("a successful PatchResult must have nonempty changed_paths")
            if self.diff_bytes <= 0:
                raise ValueError("a successful PatchResult must have diff_bytes > 0")
            if self.error is not None:
                raise ValueError("a successful PatchResult must not carry an error")
        else:
            if self.operation_count != 0:
                raise ValueError("a failed PatchResult must have operation_count == 0")
            if self.changed_paths != ():
                raise ValueError("a failed PatchResult must have changed_paths == ()")
            if self.diff_bytes != 0:
                raise ValueError("a failed PatchResult must have diff_bytes == 0")
            if self.commit_hash is not None:
                raise ValueError("a failed PatchResult must have commit_hash is None")
            if self.error is None:
                raise ValueError("a failed PatchResult must carry an error")


_MAX_OUTPUT_BYTES = 64 * 1024
# Slack above the hard 64 KiB collection limit, to accommodate a
# truncation marker appended after the limit is reached — this is a
# courtesy sanity check on the *result*, not the enforcement mechanism
# itself (that lives in whatever collected the output, e.g.
# executor._BoundedCollector).
_MAX_OUTPUT_BYTES_WITH_MARKER = _MAX_OUTPUT_BYTES + 256


@dataclass(frozen=True)
class VerificationResult:
    """What actually happened running the baseline/verification command
    — RunController reports these fields verbatim, same discipline as
    PatchResult.

    Its exit-code/outcome/error shape is validated with the same public
    function events.py exposes for BaselineRecorded/VerificationCompleted
    (`events.validate_verification_outcome_shape`) rather than a
    separately maintained copy of that matrix — see
    tests/unit/test_verification_result_parity.py, which exists
    specifically to catch the day these two are ever validated by
    different rules. This is the one place production code outside
    events.py is allowed to reach into its validation logic, and it
    does so through that module's public function, never its
    underscore-prefixed internals.
    """

    outcome: events.VerificationOutcome
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    cleanup_status: events.ContainerCleanupStatus
    error: OperationalError | None = None
    # ADR 0005 Amendment 1 §12 (fourth and fifth corrections): a
    # non-`Exception` `BaseException` (for example `KeyboardInterrupt`)
    # retained during an attempt when it is not re-raised -- because cleanup
    # could not be fully confirmed (cleanup dominance), or because an
    # ordinary attach-wait failure stays primary. The sanitized failure
    # above is the result; this exact instance is carried alongside it,
    # never raised and never in `repr`, equality or any event. Otherwise the
    # verifier re-raises it instead of returning.
    deferred_interrupt: BaseException | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.deferred_interrupt is not None and (
            not isinstance(self.deferred_interrupt, BaseException)
            or isinstance(self.deferred_interrupt, Exception)
        ):
            raise ValueError("deferred_interrupt must be a BaseException that is not an Exception")
        events.validate_verification_outcome_shape(
            self.outcome, self.exit_code, self.error, self.cleanup_status
        )
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError(
                f"duration_seconds must be finite and >= 0, got {self.duration_seconds!r}"
            )
        if len(self.stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES_WITH_MARKER:
            raise ValueError("stdout exceeds the bounded output limit")
        if len(self.stderr.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES_WITH_MARKER:
            raise ValueError("stderr exceeds the bounded output limit")


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    """Production default: real wall-clock time (`now`) and a real
    monotonic clock (`monotonic`, for measuring actual operation
    durations). Tests inject a deterministic Clock instead (see
    tests/support/fakes.SteppingClock) so traces are reproducible
    without sleeping or patching datetime/time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class RepositoryTools(Protocol):
    """The read-only repository toolkit the turn loop dispatches to (ledger
    D4a/D4b): `codeagent.repo_tools.RepoTools`, or a fake. Each call returns
    a `ToolOutcome`; an unconfirmed descriptor cleanup raises
    `tool_schema.ToolCleanupUnconfirmedError` (D4 plan revision 5 §7.4).
    `check_search_cursor` is the policy-stage, filesystem-free chain check:
    None, or a categorical rejection string."""

    def read_file(self, args: ReadFileArgs, should_stop: Callable[[], bool]) -> ToolOutcome: ...
    def list_directory(self, args: ListDirectoryArgs, should_stop: Callable[[], bool]) -> ToolOutcome: ...
    def search_text(self, args: SearchTextArgs, should_stop: Callable[[], bool]) -> ToolOutcome: ...
    def check_search_cursor(self, args: SearchTextArgs) -> str | None: ...


@dataclass(frozen=True)
class ApprovalRequest:
    """What an approval provider is asked to approve: one plan revision and
    the digests that bind the approval to exactly that proposal."""

    plan_revision: int
    plan_digest: str
    change_digest: str
    material_change: bool
    args: PlanProposal


@dataclass(frozen=True)
class ApprovalResponse:
    """An approval decision and its optional operator note (D4 plan
    revision 5 §8.4). Meaning is judged on the *original* note: it must hold
    at least one letter, mark, number, punctuation or symbol character.
    `REVISION_REQUESTED` requires such a note; for `APPROVED`/`REJECTED` a
    meaningless note is normalised to None. Notes over 2 KiB or carrying a
    lone surrogate are rejected outright. `recorded_note` is the sanitized
    form written to events and feedback."""

    decision: domain.ApprovalDecision
    operator_note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, domain.ApprovalDecision):
            raise ValueError("decision must be an ApprovalDecision")
        note = self.operator_note
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("operator_note must be a string")
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in note):
                raise ValueError("operator_note must not contain a lone surrogate")
            if len(note.encode("utf-8")) > MAX_NOTE_BYTES:
                raise ValueError("operator_note is too long")
        meaningful = note is not None and is_meaningful_note(note)
        if self.decision is domain.ApprovalDecision.REVISION_REQUESTED and not meaningful:
            raise ValueError("a revision request requires a meaningful operator note")
        if note is not None and not meaningful:
            object.__setattr__(self, "operator_note", None)

    @property
    def recorded_note(self) -> str | None:
        return None if self.operator_note is None else sanitize_recorded_text(self.operator_note)


@dataclass(frozen=True)
class ApprovedPlan:
    """The complete immutable proposal that was approved, with its digests.
    Dispatch recomputes both digests from `args` before applying."""

    revision: int
    args: PlanProposal
    digests: PlanDigests


class ApprovalProvider(Protocol):
    """A cancellable provider that stops waiting because the run's token
    was cancelled raises `codeagent.cancellation.OperationCancelled`; the
    controller maps that to `CANCELLED` only around this call (ADR 0005
    Amendment 1 section 4). Any other exception, or a return that is not an
    `ApprovalResponse`, is an unexpected failure (Slice 3C-3)."""

    def decide(self, request: ApprovalRequest) -> ApprovalResponse: ...


class Verifier(Protocol):
    """`run_baseline` and `run` both return a `VerificationResult` —
    RunController trusts every field verbatim (outcome, exit_code,
    duration, output, error), it never derives or invents any of them.
    A real implementation (codeagent.executor.DockerVerifier) may
    return any VerificationOutcome; a fake may choose to support only
    a subset (see tests/support/fakes.FakeVerifier) — that's the
    collaborator's choice, not something this Protocol restricts.

    `command` is the single source of truth for what verification
    actually runs: RunController records this value on RunStarted,
    BaselineRecorded, and VerificationCompleted rather than a
    separately configured `RunConfig.verify_command` that could
    disagree with what the Verifier itself executes.
    """

    @property
    def command(self) -> tuple[str, ...]: ...
    def run_baseline(self) -> VerificationResult: ...
    def run(self, attempt_index: int) -> VerificationResult: ...


class Workspace(Protocol):
    """The narrow slice of `codeagent.workspace.GitWorktree` the
    controller needs (Milestone 2 slice 2B-2): the two real paths
    evidence capture needs, the sole trusted initial-checkpoint SHA, the
    entry gate, and the dispose/preserve ownership-transfer pair. A
    fake implementing this Protocol never touches a filesystem or Git
    at all — see `tests.support.fakes.FakeWorkspace`.
    """

    @property
    def path(self) -> Path: ...
    @property
    def source_repo_path(self) -> Path: ...
    @property
    def initial_commit(self) -> str: ...
    def entry_gate(self, expected_commit: str) -> None: ...
    def dispose(self) -> None: ...
    def preserve(self) -> None: ...


class CheckpointSessionLike(Protocol):
    """The narrow slice of `codeagent.checkpoint_session.CheckpointSession`
    the controller needs. Implementations — real or fake — must raise
    `codeagent.checkpoint_ref.CheckpointRefError` for a Git-level
    checkpoint-ref failure and `codeagent.checkpoint_session.
    CheckpointSessionError` for a session-state misuse, exactly like
    the real `CheckpointSession`, so the controller's exact
    reason/outcome mapping (`_map_checkpoint_error`) works uniformly
    for both — see `tests.support.fakes.FakeCheckpointSession`.
    """

    @property
    def intent(self) -> CheckpointIntent: ...
    @property
    def accepted_sha(self) -> str | None: ...
    def establish(self, initial_sha: str) -> None: ...
    def advance(self, new_sha: str) -> None: ...
    def delete(self) -> None: ...


class LifecycleOwnerPublisher(Protocol):
    """Optional collaborator (Milestone 3 Slice 3C-1, ADR 0004
    Amendment 8). Publishes this run's owner-state lifecycle
    transitions at the exact moments this controller already tracks
    internally: `activate()` once, immediately after `RunStarted`,
    before any EXPLORE/model/tool work begins; `begin_cleanup()` and
    `complete()` inside `_terminate()`. Raises only
    `codeagent.lifecycle_owner.OwnerStatePublicationError` — never
    exposes `lifecycle_store.LifecycleState`, `LifecycleProjection`, or
    any other persistence-stack type. A fake implementing this Protocol
    never touches a filesystem or Git ref at all — see
    `tests.support.fakes.FakeLifecycleOwnerPublisher`."""

    def activate(self) -> None: ...
    def begin_cleanup(self) -> None: ...
    def complete(self) -> None: ...


class PatchApplier(Protocol):
    """Applies exactly the given operations and reports what actually
    happened. In a run, `operations` is built only from the approved,
    digest-bound model proposal (ledger D4b); no caller-supplied patch
    operations or patch content remain. The applier is constructed with
    the trusted worktree path only.

    `approved_paths` *is* passed, and is the primary, preventive
    authorization boundary: an implementation must reject an operation
    whose target isn't in this set before making any change at all —
    see `codeagent.patch.GitPatchApplier.apply`'s docstring. It is not
    merely advisory data for a postcondition check.
    """

    def apply(
        self,
        run_id: str,
        iteration: int,
        approved_paths: frozenset[str],
        operations: tuple[PatchOperation, ...],
    ) -> PatchResult: ...


@dataclass(frozen=True)
class RunConfig:
    """General run parameters — not fixture-scripting data. What the
    fake (or, later, real) collaborators decide dynamically is not part
    of this config."""

    run_id: str
    task_statement: str
    approval_mode: domain.ApprovalMode
    # ADR 0004's internal, validated 128-bit lowercase-hex lifecycle
    # identity — the sole owner of this run's checkpoint ref namespace
    # (Milestone 2 slice 2B-2). Required: every run has exactly one
    # lifecycle. Minted only via codeagent.checkpoint_ref.new_lifecycle_id()
    # by the trusted composition root — never derived from run_id or any
    # other caller-supplied text.
    lifecycle_id: str = ""
    # Real worktree path when one exists (slice B); a placeholder URI
    # when the run has no real repository at all (slice A, fully faked).
    repository_path: str = "fixture://synthetic"
    # Repairs allowed *after* the initial verification attempt: max=0
    # permits the initial attempt but no repair; max=2 permits the
    # initial attempt plus two repairs (3 verification attempts total).
    max_repair_iterations: int = 3
    # Plan revisions allowed before a further REVISION_REQUESTED
    # decision ends the run via BudgetExceeded(kind=PLAN_REVISIONS)
    # instead of looping again. Same "count already used vs. max"
    # semantics as max_repair_iterations.
    max_plan_revisions: int = 2
    # Ledger D4b. Model-requested tool calls per exploration pass, charged
    # one whole turn at a time; and the run's elapsed wall-clock budget, a
    # soft deadline checked at safe points (an approval wait counts).
    max_tool_calls_per_pass: int = 25
    wall_clock_budget_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must be a nonempty string")
        if not self.task_statement:
            raise ValueError("task_statement must be a nonempty string")
        if not isinstance(self.task_statement, str) or len(self.task_statement.encode("utf-8", "surrogatepass")) > 8 * 1024:
            raise ValueError("task_statement must be at most 8 KiB of UTF-8")
        if "\x00" in self.task_statement or any(0xD800 <= ord(ch) <= 0xDFFF for ch in self.task_statement):
            raise ValueError("task_statement must not contain NUL or a lone surrogate")
        if (
            isinstance(self.max_tool_calls_per_pass, bool)
            or not isinstance(self.max_tool_calls_per_pass, int)
            or not 1 <= self.max_tool_calls_per_pass <= 1000
        ):
            raise ValueError("max_tool_calls_per_pass must be an integer from 1 to 1000")
        budget = self.wall_clock_budget_seconds
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or not 0 < budget <= 86_400
        ):
            raise ValueError("wall_clock_budget_seconds must be finite, above 0 and at most 86,400")
        if not self.repository_path:
            raise ValueError("repository_path must be a nonempty string")
        if not LIFECYCLE_ID_RE.fullmatch(self.lifecycle_id or ""):
            raise ValueError("lifecycle_id must be exactly 32 lowercase hexadecimal characters")
        _require_nonnegative_int("max_repair_iterations", self.max_repair_iterations)
        _require_nonnegative_int("max_plan_revisions", self.max_plan_revisions)
        # Static context-fit proof (D4 plan revision 5 §10): everything that
        # is never dropped, plus one maximal tool result, fits the cap.
        never_dropped = (
            len(canonical_json_bytes(self.task_statement))
            + BASELINE_MAX_BYTES
            + (self.max_repair_iterations + 1) * VERIFICATION_ITEM_MAX_BYTES
            + (self.max_plan_revisions + 1) * REVISION_ITEM_MAX_BYTES
        )
        if never_dropped + TOOL_RESULT_ITEM_MAX_BYTES + CONTEXT_ENVELOPE_MAX_BYTES > CONTEXT_CAP_BYTES:
            raise ValueError("the configured repair and revision budgets cannot fit the model context cap")


class _CancellationObserved(Exception):
    """Raised only by `RunController._cancellation_check` and caught only
    by `run()`, before its ordinary-exception boundary."""


class _DeadlineObserved(Exception):
    """Raised by `RunController._cancellation_check` after the cancellation
    check, when the soft wall-clock deadline has passed; caught only by
    `run()`, which records `BudgetExceeded(WALL_CLOCK)` (ledger D4b)."""


# ADR 0005 Amendment 1 section 3: the check sites that live inside
# `_transition()` and `_dispatch_tool()`, named as in the D3 plan's table.
# Every other non-abort transition or dispatch is a missing entry and fails
# closed (KeyError) rather than skipping its check.
_TRANSITION_CHECK_SITES: dict[domain.Trigger, str] = {
    domain.Trigger.BASELINE_RECORDED: "C4",
    domain.Trigger.PLAN_PROPOSED: "C12",
    domain.Trigger.PLAN_RECORDED: "C13",
    domain.Trigger.PLAN_REVISION_REQUESTED: "C16",
    domain.Trigger.PLAN_APPROVED: "C17",
    domain.Trigger.PATCH_APPLIED: "C20",
    domain.Trigger.VERIFICATION_FAILED: "C23",
}
_DISPATCH_CHECK_SITES: dict[domain.ToolName, str] = {
    domain.ToolName.APPLY_PATCH: "C18",
}

# Ledger D4b (readiness addendum J2): the turn loop's own sites. C5 is the
# first model request of each pass (ADR 0005 Amendment 1 §8 counts passes
# begun before it); C24 every later request in the pass; C25 the decision
# point after a returned model outcome; C26 before every model-requested
# tool triple; C27 the decision point after a returned tool outcome.
_FIRST_REQUEST_SITE = "C5"
_LATER_REQUEST_SITE = "C24"
_MODEL_DECISION_SITE = "C25"
_TOOL_DISPATCH_SITE = "C26"
_TOOL_DECISION_SITE = "C27"
_REPOSITORY_TOOLS = frozenset(
    {domain.ToolName.LIST_DIRECTORY, domain.ToolName.READ_FILE, domain.ToolName.SEARCH_TEXT}
)
_DECISION_CHECKED = "checked"
_DECISION_OPERATIONAL = "operational_failure_precedence"


class EventLog:
    """In-memory stand-in for the real EventSink (unbuilt). Its entire
    job is assigning and enforcing monotonic sequence numbers — real
    persistence is separate, later work."""

    def __init__(self) -> None:
        self.events: list[events.Event] = []
        self._next_sequence = 0

    def next_sequence(self) -> int:
        return self._next_sequence

    def append(self, event: events.Event) -> None:
        if event.sequence != self._next_sequence:
            raise AssertionError(
                f"event sequence out of order: expected {self._next_sequence}, "
                f"got {event.sequence} ({type(event).__name__})"
            )
        self.events.append(event)
        self._next_sequence += 1


class RunController:
    """Drives one run through domain.py's state machine, emitting
    events.py-typed events for every step.

    Four counters are kept deliberately distinct — conflating them was
    the source of a real bug in the first version of this controller:
    - `pass_index` (== each event's `iteration` field): which EXPLORE
      pass an event belongs to. An index, not a count — incremented on
      every return to EXPLORE, whether caused by a plan revision or a
      failed verification.
    - `_plan_revision`: the run-wide plan revision (1-based), bound into
      every `ApprovalRequest` and `PlanProposed` by digest (ledger D4b).
    - `_verification_attempt`: which verification attempt this is (for
      Verifier.run) — only advances when EXECUTE/VERIFY is actually
      reached, so a plan revision (which never reaches VERIFY) cannot
      skip a verifier outcome the way it did before.
    - `_repair_iterations_used` / `_plan_revisions_used`: real counts
      compared against RunConfig's budgets — a plan revision no longer
      consumes repair budget, and vice versa.

    `RunFinished.iterations_used` is the number of exploration passes
    begun — 0 for a run that ends before its first EXPLORE pass — see the
    docstring on `_finish`.

    Cancellation (ADR 0005 Amendment 1 section 3): after `RunStarted`, the
    token is checked at the entry of every non-abort transition except
    `RUN_STARTED`, every tool dispatch, every model request and every
    verifier call, and immediately after every blocking collaborator
    returns successfully and its completion event is emitted. Once a check
    observes cancellation nothing new begins; the run ends through
    `_terminate(CANCELLED)`. `activate()` and `RUN_STARTED` are exempt, and
    the patch critical section (from the `APPLY_PATCH` dispatch check to
    `PatchApplied` or its failure `ToolCompleted`) contains no check. A
    returned operational failure keeps its existing path.
    """

    def __init__(
        self,
        config: RunConfig,
        model: ModelClient,
        approval: ApprovalProvider,
        verifier: Verifier,
        patch_applier: PatchApplier,
        tools: RepositoryTools,
        workspace: Workspace,
        session: CheckpointSessionLike,
        evidence_sink: EvidenceSink,
        clock: Clock | None = None,
        event_log: EventLog | None = None,
        lifecycle_owner: LifecycleOwnerPublisher | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> None:
        self._c = config
        self._model = model
        self._approval = approval
        self._verifier = verifier
        self._patch_applier = patch_applier
        self._tools = tools
        self._workspace = workspace
        self._session = session
        self._evidence_sink = evidence_sink
        self._clock = clock or SystemClock()
        self.log = event_log or EventLog()
        self.state = domain.RunState.INIT
        self._total_duration = 0.0
        # The first checkpoint's parent is the workspace's pinned
        # starting commit — the sole trusted initial-checkpoint
        # authority (ADR 0003 Amendment 2), replacing the removed
        # caller-controlled RunConfig.initial_checkpoint_id.
        self._last_checkpoint_id: str | None = workspace.initial_commit
        # Tracked across the whole run (Milestone 2 slice 2B-2): any
        # single verifier cleanup-unconfirmed occurrence — baseline or
        # any verification attempt — forces the workspace/ref to be
        # preserved at terminal teardown, per ADR 0003 Amendment 2.
        self._any_verifier_cleanup_unconfirmed = False
        # Milestone 3 Slice 3C-1 (ADR 0004 Amendment 8): an optional
        # owner-state lifecycle-projection publisher. `None` (the
        # default) preserves every existing caller's behavior exactly —
        # no activate()/begin_cleanup()/complete() call is ever made.
        self._lifecycle_owner = lifecycle_owner
        # True only once activate() has been confirmed to succeed —
        # begin_cleanup()/complete() are never attempted otherwise,
        # since PREPARING -> CLEANING is not a legal owner-state edge.
        self._lifecycle_owner_active = False
        # Milestone 3 Slice 3C-3 (ADR 0004 Amendment 9): a one-shot guard
        # against _terminate() ever running twice for one run, and the
        # last-known pass index for run()'s ordinary-exception fallback —
        # see _run_after_start() and _terminate() below.
        self._termination_started = False
        self._pass_index = 0
        # ADR 0005 Amendment 1. `_cancellation_sites` records each check as
        # (site, next event sequence) for the exhaustive sweep test only.
        self._cancellation = cancellation
        self._cancellation_sites: list[tuple[str, int]] = []
        # The first `VerificationResult.deferred_interrupt` seen (cleanup
        # unconfirmed dominated it); exposed, never raised, never persisted.
        self._deferred_interrupt: BaseException | None = None
        # Exploration passes begun (ADR 0005 Amendment 1 section 8).
        self._passes_begun = 0
        # Every repository-tool descriptor-cleanup failure, by identity
        # (ledger D4a; D4 plan revision 5 §7.4). Never persisted.
        self._tool_cleanup_failures: list[ToolCleanupUnconfirmedError] = []
        # Ledger D4b. The model name is validated once, here; an invalid one
        # is a configuration error, not a run outcome.
        model_name = getattr(model, "model_name", None)
        if not valid_model_name(model_name):
            raise ValueError("model.model_name must match [A-Za-z0-9._:-]{1,128}")
        self._model_name: str = model_name
        # The in-memory model-visible transcript (never persisted), the count
        # of droppable items removed to fit the context cap, the baseline
        # summary, and plan-revision state.
        self._transcript: list[FeedbackItem] = []
        self._dropped_count = 0
        self._baseline_summary: dict[str, object] = {}
        self._plan_revision = 0
        self._last_approved_change_digest: str | None = None
        self._run_started_mono: float | None = None
        # Every returned model/tool outcome's decision point (C25/C27), as
        # (site, next event sequence, kind); calls that raised get none.
        self._post_return_decisions: list[tuple[str, int, str]] = []

    @property
    def tool_cleanup_failures(self) -> tuple[ToolCleanupUnconfirmedError, ...]:
        """Retained `ToolCleanupUnconfirmedError` instances, in order."""
        return tuple(self._tool_cleanup_failures)

    @property
    def deferred_interrupt(self) -> BaseException | None:
        """An exit-style interrupt retained while cleanup stayed
        unconfirmed: by the verifier (ADR 0005 Amendment 1 §12) or a
        repository tool's descriptor cleanup (D4 plan revision 5 §7.4).
        The first one seen wins. The run still ends through the ordinary
        failure path; this keeps the second fact observable to the caller."""
        return self._deferred_interrupt

    def _note_deferred_interrupt(self, result: VerificationResult) -> None:
        if result.deferred_interrupt is not None and self._deferred_interrupt is None:
            self._deferred_interrupt = result.deferred_interrupt

    def _now(self) -> datetime:
        return self._clock.now()

    def _emit(self, event: events.Event) -> None:
        self.log.append(event)

    def _cancellation_check(self, site: str) -> None:
        """One ordinary safe point: cancellation first, then the soft
        wall-clock deadline (ledger D4b), then the caller's own work."""
        self._cancellation_sites.append((site, self.log.next_sequence()))
        if self._cancellation.is_cancelled():
            raise _CancellationObserved(site)
        if self._deadline_exceeded():
            raise _DeadlineObserved(site)

    def _elapsed(self) -> float:
        if self._run_started_mono is None:
            return 0.0
        return max(0.0, self._clock.monotonic() - self._run_started_mono)

    def _deadline_exceeded(self) -> bool:
        return self._run_started_mono is not None and self._elapsed() > self._c.wall_clock_budget_seconds

    def _should_stop(self) -> bool:
        """Handed to repository tools: a pure poll, never raising."""
        return self._cancellation.is_cancelled() or self._deadline_exceeded()

    def _record_decision(self, site: str, kind: str) -> None:
        self._post_return_decisions.append((site, self.log.next_sequence(), kind))

    def _transition(self, iteration: int, trigger: domain.Trigger) -> domain.TransitionResult:
        if not (
            trigger in domain.ABORT_TRIGGERS
            or trigger is domain.Trigger.RUN_STARTED
            or self._termination_started
        ):
            self._cancellation_check(_TRANSITION_CHECK_SITES[trigger])
        from_state = self.state
        result = domain.transition(from_state, trigger)
        self._emit(
            events.StateTransitioned(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=result.next_state,
                iteration=iteration,
                trigger=trigger,
                from_state=from_state,
                to_state=result.next_state,
                terminal_reason=result.terminal_reason,
            )
        )
        self.state = result.next_state
        return result

    def _dispatch_tool(
        self, iteration: int, tool: domain.ToolName, tool_call_id: str, arguments: dict[str, object]
    ) -> None:
        """Emit ToolRequested then PolicyDecisionRecorded for a legal
        tool call. Raises domain.IllegalToolCallError (fail closed) if
        the tool isn't legal in the current state — checked before
        anything is emitted. The cancellation check runs first."""
        self._cancellation_check(_DISPATCH_CHECK_SITES[tool])
        domain.check_tool_call(self.state, tool)
        self._emit(
            events.ToolRequested(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=tool,
                tool_call_id=tool_call_id,
                redacted_arguments_json=_canonical_json(arguments),
            )
        )
        self._emit(
            events.PolicyDecisionRecorded(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=tool,
                tool_call_id=tool_call_id,
                allowed=True,
                reason=f"{tool.value} is legal in state {self.state.value}",
            )
        )

    def run(self) -> events.RunFinished:
        """Emit `RunStarted` and record `RUN_STARTED`, then run the rest
        of the run through `_run_after_start()` behind a single ordinary-
        exception terminalization boundary (Milestone 3 Slice 3C-3, ADR
        0004 Amendment 9): an unanticipated `Exception` escaping normal
        run execution is routed exactly once through `_terminate()`,
        producing `RunFinished(terminal_reason=UNRECOVERABLE_ERROR,
        error.code=ErrorCode.UNCLASSIFIED_FAILURE)`.

        Deliberately narrow. Not covered: any `BaseException` that is not
        an `Exception` (`KeyboardInterrupt`/`SystemExit`/`GeneratorExit`
        propagate untouched by construction — they don't inherit from
        `Exception`); a failure during the
        `RunStarted` emission or `RUN_STARTED` transition above (outside
        this boundary — the event contract doesn't yet say whether
        terminalization is even valid before `RunStarted` exists in the
        log, so this method doesn't invent an answer); and a failure
        arising inside `_terminate()` itself, which the one-shot
        `_termination_started` guard (see `_terminate()`) ensures is
        never converted into a second terminalization attempt — it
        propagates unchanged instead.

        Cancellation (ADR 0005 Amendment 1): a check that observes the
        token raises the private `_CancellationObserved`, caught here
        before the ordinary-exception boundary and routed once through
        `_terminate(CANCELLED)`.
        """
        c = self._c
        self._run_started_mono = self._clock.monotonic()

        self._emit(
            events.RunStarted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=domain.RunState.INIT,
                iteration=0,
                repository_path=c.repository_path,
                task_statement=c.task_statement,
                verify_command=self._verifier.command,
                approval_mode=c.approval_mode,
            )
        )
        self._transition(0, domain.Trigger.RUN_STARTED)

        cancelled = False
        deadline = False
        try:
            return self._run_after_start()
        except _CancellationObserved:
            if self._termination_started:
                raise
            cancelled = True
        except _DeadlineObserved:
            if self._termination_started:
                raise
            deadline = True
        except Exception:
            if self._termination_started:
                raise
            # Fall through to the fallback _terminate() call below,
            # outside this handler. The exception is never bound to a
            # name anywhere in this block, so nothing here retains it.

        if cancelled:
            # Outside the handler, for the same clean-context reason as below.
            return self._terminate(self._pass_index, domain.Trigger.CANCELLED)

        if deadline:
            self._emit(
                events.BudgetExceeded(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=self._pass_index,
                    kind=domain.BudgetKind.WALL_CLOCK,
                    limit_value=c.wall_clock_budget_seconds,
                    observed_value=self._elapsed(),
                )
            )
            return self._terminate(self._pass_index, domain.Trigger.BUDGET_EXCEEDED)

        # Reached only when an ordinary Exception escaped
        # _run_after_start() and _terminate() had not yet started.
        # Deliberately outside the `except` block: no exception is
        # "currently being handled" at this point, so if this fallback
        # _terminate() call itself raises, that new exception starts
        # with a clean __context__/__cause__ — never chained to the
        # exception that was just discarded above.
        return self._terminate(
            self._pass_index,
            domain.Trigger.UNRECOVERABLE_ERROR,
            error=OperationalError(
                code=ErrorCode.UNCLASSIFIED_FAILURE,
                error_id=f"{c.run_id}-unclassified-failure",
                message="the run terminated due to an unanticipated internal error",
            ),
        )

    def _run_after_start(self) -> events.RunFinished:
        c = self._c

        # Milestone 3 Slice 3C-1 (ADR 0004 Amendment 8): confirm the
        # run's lifecycle projection is ACTIVE before any baseline,
        # model, or tool work begins. No controller-driven baseline,
        # Docker verification, patch, or checkpoint-ref mutation has
        # begun yet at this point -- but the physical workspace (the
        # entered GitWorktree a future composition layer constructs
        # before RunController itself) may already exist, since this
        # controller is always handed an already-entered Workspace, not
        # one it enters itself. That is precisely why routing a failure
        # here through the ordinary _terminate() sequence is safe rather
        # than skipping it: _terminate() still disposes or preserves
        # that workspace and invokes the checkpoint session's cleanup,
        # the same early-abort pattern already used for a failed initial
        # READ_FILE.
        if self._lifecycle_owner is not None:
            try:
                self._lifecycle_owner.activate()
                self._lifecycle_owner_active = True
            except OwnerStatePublicationError:
                return self._terminate(
                    0,
                    domain.Trigger.UNRECOVERABLE_ERROR,
                    error=OperationalError(
                        code=ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED,
                        error_id=f"{c.run_id}-lifecycle-activate",
                        message="the run's lifecycle projection could not be confirmed active",
                    ),
                )
        self._cancellation_check("C1")

        self._cancellation_check("C2")
        baseline_result = self._verifier.run_baseline()
        self._note_deferred_interrupt(baseline_result)
        self._total_duration += baseline_result.duration_seconds
        self._emit(
            events.BaselineRecorded(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=0,
                outcome=baseline_result.outcome,
                command=self._verifier.command,
                exit_code=baseline_result.exit_code,
                duration_seconds=baseline_result.duration_seconds,
                cleanup_status=baseline_result.cleanup_status,
                error=baseline_result.error,
            )
        )
        if baseline_result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED:
            self._any_verifier_cleanup_unconfirmed = True
        self._baseline_summary = {
            "exit_code": baseline_result.exit_code,
            "outcome": baseline_result.outcome.value,
            "stderr_tail": encoded_tail(baseline_result.stderr, BASELINE_TAIL_BYTES),
            "stdout_tail": encoded_tail(baseline_result.stdout, BASELINE_TAIL_BYTES),
            "untrusted": True,
        }
        if baseline_result.outcome is events.VerificationOutcome.CANCELLED:
            return self._terminate(0, domain.Trigger.CANCELLED)
        if baseline_result.outcome not in (
            events.VerificationOutcome.PASSED,
            events.VerificationOutcome.TEST_FAILURE,
        ):
            # An operational failure running the baseline itself (the
            # executor couldn't even establish what "broken" looks
            # like) aborts the run outright — there's nothing to repair
            # towards yet.
            return self._terminate(0, domain.Trigger.UNRECOVERABLE_ERROR, error=baseline_result.error)
        self._cancellation_check("C3")
        self._transition(0, domain.Trigger.BASELINE_RECORDED)

        pass_index = 0
        verification_attempt = 0
        repair_iterations_used = 0
        plan_revisions_used = 0

        while True:
            self._pass_index = pass_index
            self._passes_begun += 1
            explored = self._explore_pass(pass_index)
            if isinstance(explored, events.RunFinished):
                return explored
            plan, digests, revision, material = explored
            self._transition(pass_index, domain.Trigger.PLAN_PROPOSED)
            self._transition(pass_index, domain.Trigger.PLAN_RECORDED)

            self._cancellation_check("C14")
            request = ApprovalRequest(
                plan_revision=revision,
                plan_digest=digests.plan_digest,
                change_digest=digests.change_digest,
                material_change=material,
                args=plan,
            )
            approval_cancelled = False
            try:
                response = self._approval.decide(request)
            except OperationCancelled:
                # Only here: raised anywhere else it is an ordinary
                # unexpected exception (UNCLASSIFIED_FAILURE). No
                # ApprovalRecorded is emitted.
                approval_cancelled = True
            if approval_cancelled:
                return self._terminate(pass_index, domain.Trigger.CANCELLED)
            if not isinstance(response, ApprovalResponse):
                raise TypeError("the approval provider did not return an ApprovalResponse")
            decision = response.decision
            self._emit(
                events.ApprovalRecorded(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    decision=decision,
                    approval_mode=c.approval_mode,
                    plan_revision=revision,
                    plan_digest=digests.plan_digest,
                    operator_note=response.recorded_note,
                )
            )
            # J3: the approval wait counts toward elapsed time; this ordinary
            # safe point checks cancellation, then the soft deadline.
            self._cancellation_check("C15")

            if decision is domain.ApprovalDecision.REJECTED:
                return self._terminate(pass_index, domain.Trigger.PLAN_REJECTED)

            if decision is domain.ApprovalDecision.REVISION_REQUESTED:
                self._transition(pass_index, domain.Trigger.PLAN_REVISION_REQUESTED)
                if plan_revisions_used >= c.max_plan_revisions:
                    self._emit(
                        events.BudgetExceeded(
                            run_id=c.run_id,
                            sequence=self.log.next_sequence(),
                            timestamp=self._now(),
                            state=self.state,
                            iteration=pass_index,
                            kind=domain.BudgetKind.PLAN_REVISIONS,
                            limit_value=c.max_plan_revisions,
                            observed_value=plan_revisions_used,
                        )
                    )
                    return self._terminate(pass_index, domain.Trigger.BUDGET_EXCEEDED)
                plan_revisions_used += 1
                self._transcript.append(
                    FeedbackItem(
                        FeedbackKind.REVISION_REQUESTED,
                        pass_index,
                        {"operator_note": response.recorded_note, "plan_revision": revision},
                    )
                )
                pass_index += 1
                continue

            # APPROVED -> EXECUTE
            self._last_approved_change_digest = digests.change_digest
            approved = ApprovedPlan(revision=revision, args=plan, digests=digests)
            self._transition(pass_index, domain.Trigger.PLAN_APPROVED)
            patch_ok, patch_error = self._dispatch_apply_patch(pass_index, approved)
            if not patch_ok:
                return self._terminate(
                    pass_index, domain.Trigger.UNRECOVERABLE_ERROR, error=patch_error
                )
            self._cancellation_check("C19")

            self._transition(pass_index, domain.Trigger.PATCH_APPLIED)

            self._cancellation_check("C21")
            verification_result = self._verifier.run(verification_attempt)
            self._note_deferred_interrupt(verification_result)
            verification_attempt += 1
            self._total_duration += verification_result.duration_seconds
            if verification_result.cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED:
                self._any_verifier_cleanup_unconfirmed = True
            self._emit(
                events.VerificationCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    outcome=verification_result.outcome,
                    command=self._verifier.command,
                    exit_code=verification_result.exit_code,
                    duration_seconds=verification_result.duration_seconds,
                    fail_to_pass=(),
                    pass_to_pass_broken=(),
                    cleanup_status=verification_result.cleanup_status,
                    error=verification_result.error,
                )
            )

            if verification_result.outcome is events.VerificationOutcome.CANCELLED:
                # Never the TEST_FAILURE repair branch.
                return self._terminate(pass_index, domain.Trigger.CANCELLED)
            if verification_result.outcome in (
                events.VerificationOutcome.PASSED,
                events.VerificationOutcome.TEST_FAILURE,
            ):
                self._cancellation_check("C22")

            if verification_result.outcome is events.VerificationOutcome.PASSED:
                return self._terminate(pass_index, domain.Trigger.VERIFICATION_PASSED)

            if verification_result.outcome is events.VerificationOutcome.TEST_FAILURE:
                # An expected domain outcome, not an error: repair if
                # budget allows. Its bounded, sanitized output tails become
                # never-dropped feedback for the next pass (in memory only).
                self._transcript.append(
                    FeedbackItem(
                        FeedbackKind.VERIFICATION_FAILED,
                        pass_index,
                        {
                            "exit_code": verification_result.exit_code,
                            "outcome": verification_result.outcome.value,
                            "plan_revision": revision,
                            "stderr_tail": encoded_tail(verification_result.stderr, VERIFICATION_TAIL_BYTES),
                            "stdout_tail": encoded_tail(verification_result.stdout, VERIFICATION_TAIL_BYTES),
                            "untrusted": True,
                        },
                    )
                )
                self._transition(pass_index, domain.Trigger.VERIFICATION_FAILED)
                if repair_iterations_used >= c.max_repair_iterations:
                    self._emit(
                        events.BudgetExceeded(
                            run_id=c.run_id,
                            sequence=self.log.next_sequence(),
                            timestamp=self._now(),
                            state=self.state,
                            iteration=pass_index,
                            kind=domain.BudgetKind.REPAIR_ITERATIONS,
                            limit_value=c.max_repair_iterations,
                            observed_value=repair_iterations_used,
                        )
                    )
                    return self._terminate(pass_index, domain.Trigger.BUDGET_EXCEEDED)
                repair_iterations_used += 1
                pass_index += 1
                continue  # loop back to EXPLORE for another attempt

            # An operational outcome (TIMEOUT / ENVIRONMENT_FAILURE /
            # COMMAND_START_FAILURE): abort outright, without consuming
            # repair budget — this isn't a normal repair-triggering
            # failure, it's the executor itself failing to produce a
            # trustworthy result at all.
            return self._terminate(
                pass_index, domain.Trigger.UNRECOVERABLE_ERROR, error=verification_result.error
            )

    # ------------------------------------------------------------------
    # Ledger D4b: the model-driven exploration pass
    # ------------------------------------------------------------------

    def _build_context(self, pass_index: int, used: int) -> tuple[ModelContext, frozenset[str]]:
        """The model-visible context for one request, dropping the oldest
        droppable feedback until it fits `CONTEXT_CAP_BYTES`, and the frozen
        set of evidence IDs a plan in that turn may cite: successful
        repository-tool results of the current pass, still retained."""

        def make() -> ModelContext:
            return ModelContext(
                task_statement=self._c.task_statement,
                baseline=self._baseline_summary,
                pass_index=pass_index,
                tool_calls_remaining=self._c.max_tool_calls_per_pass - used,
                transcript=tuple(self._transcript),
                dropped_count=self._dropped_count,
            )

        context = make()
        while context.size > CONTEXT_CAP_BYTES:
            index = next((i for i, item in enumerate(self._transcript) if item.kind in DROPPABLE_KINDS), None)
            if index is None:  # unreachable: RunConfig's static fit proof
                raise RuntimeError("the model context cannot fit its cap")
            del self._transcript[index]
            self._dropped_count += 1
            context = make()
        citable = frozenset(
            item.body["tool_call_id"]
            for item in self._transcript
            if item.kind is FeedbackKind.TOOL_RESULT and item.pass_index == pass_index and item.body["ok"]
        )
        return context, citable

    def _emit_model_request_failed(self, pass_index: int, request_id: str) -> None:
        self._emit(
            events.ModelRequestFailed(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                request_id=request_id,
                error=OperationalError(
                    code=ErrorCode.MODEL_PROVIDER_REQUEST_FAILED,
                    error_id=f"{request_id}-failed",
                    message="model request raised before returning a response",
                ),
            )
        )

    def _request_model(self, pass_index: int, request_id: str, context: ModelContext) -> tuple[object, float]:
        """One blocking model call with its pairing (D4 plan revision 6
        §7.1a). If `respond()` raises, `ModelRequestFailed` is emitted and
        the exception propagates: an ordinary `Exception` to Slice 3C-3
        (row 3), an interrupt as the exact instance. No C25 decision is
        recorded for a call that raised."""
        self._emit(
            events.ModelRequestStarted(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                request_id=request_id,
                model_name=self._model_name,
            )
        )
        start = self._clock.monotonic()
        try:
            turn = self._model.respond(context)
        except Exception:
            self._total_duration += max(0.0, self._clock.monotonic() - start)
            self._emit_model_request_failed(pass_index, request_id)
            raise
        except BaseException as interrupt:
            try:
                self._emit_model_request_failed(pass_index, request_id)
            except BaseException as emit_failure:  # noqa: BLE001 - the interrupt must stay primary
                attach_cleanup_failures(interrupt, (emit_failure,))
            raise interrupt
        latency = max(0.0, self._clock.monotonic() - start)
        self._total_duration += latency
        return turn, latency

    def _model_error(self, request_id: str, code: ErrorCode, message: str) -> OperationalError:
        return OperationalError(code=code, error_id=f"{request_id}-{code.value}", message=message)

    def _explore_pass(
        self, pass_index: int
    ) -> events.RunFinished | tuple[PlanProposal, PlanDigests, int, bool]:
        """One EXPLORE pass: model turns until a valid plan is proposed or
        the run ends. Returns `(plan, digests, revision, material_change)`,
        or the `RunFinished` of a run that ended here. This loop is the sole
        terminal owner for everything that happens inside the pass."""
        c = self._c
        used = 0
        turn_index = 0
        seq = 0
        while True:
            turn_index += 1
            self._cancellation_check(_FIRST_REQUEST_SITE if turn_index == 1 else _LATER_REQUEST_SITE)
            context, citable = self._build_context(pass_index, used)
            request_id = f"{c.run_id}-req-{pass_index}-{turn_index}"
            turn, latency = self._request_model(pass_index, request_id, context)
            classified = classify_turn(turn)

            if classified.rejected is not None:
                error = self._model_error(
                    request_id,
                    ErrorCode.MODEL_RESPONSE_MALFORMED,
                    "model client returned an object that is not a model turn"
                    if classified.rejected.value == "not_a_model_turn"
                    else "model client returned a turn with an unrecognized status",
                )
                self._emit(
                    events.ModelResponseRejected(
                        run_id=c.run_id,
                        sequence=self.log.next_sequence(),
                        timestamp=self._now(),
                        state=self.state,
                        iteration=pass_index,
                        request_id=request_id,
                        rejection_reason=classified.rejected,
                        error=error,
                    )
                )
                self._record_decision(_MODEL_DECISION_SITE, _DECISION_OPERATIONAL)
                return self._terminate(pass_index, domain.Trigger.UNRECOVERABLE_ERROR, error=error)

            status = classified.status
            defect = classified.metadata_defect
            completed = status is events.ModelResponseStatus.COMPLETED
            malformed = classified.metadata_defect or classified.malformed if completed else None
            event_error: OperationalError | None = None
            if malformed is not None:
                event_error = self._model_error(
                    request_id, ErrorCode.MODEL_RESPONSE_MALFORMED, f"malformed model response: {malformed.describe()}"
                )
            elif status is events.ModelResponseStatus.FAILED:
                event_error = self._model_error(
                    request_id, ErrorCode.MODEL_RESPONSE_FAILED_STATUS, "model response reported a failed status"
                )
            self._emit(
                events.ModelResponseReceived(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    request_id=request_id,
                    model_name=classified.model_name,
                    response_id=classified.response_id,
                    status=status,
                    incomplete_reason=classified.incomplete_reason,
                    input_tokens=classified.input_tokens,
                    output_tokens=classified.output_tokens,
                    latency_seconds=latency,
                    tool_call_count=classified.tool_call_count,
                    error=event_error,
                )
            )

            if not completed:
                # Run-ending statuses (revision 6 §7.1b). Returned operational
                # failures outrank a pending cancellation (ADR 0005 row 4), so
                # the token is not consulted for them.
                if defect is not None or status in (
                    events.ModelResponseStatus.FAILED,
                    events.ModelResponseStatus.IN_PROGRESS,
                    events.ModelResponseStatus.QUEUED,
                ):
                    self._record_decision(_MODEL_DECISION_SITE, _DECISION_OPERATIONAL)
                    if defect is not None:
                        error = self._model_error(
                            request_id,
                            ErrorCode.MODEL_RESPONSE_MALFORMED,
                            f"malformed model response: {defect.describe()}",
                        )
                    elif status is events.ModelResponseStatus.FAILED:
                        error = event_error
                    else:
                        error = self._model_error(
                            request_id,
                            ErrorCode.MODEL_RESPONSE_MALFORMED,
                            "synchronous model request returned a non-final response status",
                        )
                    return self._terminate(pass_index, domain.Trigger.UNRECOVERABLE_ERROR, error=error)
                self._record_decision(_MODEL_DECISION_SITE, _DECISION_CHECKED)
                if status is events.ModelResponseStatus.CANCELLED:
                    return self._terminate(pass_index, domain.Trigger.CANCELLED)
                # A valid INCOMPLETE is a non-error outcome: a pending
                # cancellation or a passed deadline outranks it.
                self._cancellation_check(_MODEL_DECISION_SITE)
                return self._terminate(pass_index, domain.Trigger.MODEL_RESPONSE_INCOMPLETE)

            self._record_decision(_MODEL_DECISION_SITE, _DECISION_CHECKED)
            self._cancellation_check(_MODEL_DECISION_SITE)

            # The atomic per-turn charge (rev 2 §9.2): before any ToolRequested.
            if malformed is not None:
                charge = min(max(classified.declared_calls, 1), MALFORMED_CHARGE_CAP)
            else:
                charge = len(classified.calls)
            limit = c.max_tool_calls_per_pass
            if used + charge > limit:
                self._emit(
                    events.BudgetExceeded(
                        run_id=c.run_id,
                        sequence=self.log.next_sequence(),
                        timestamp=self._now(),
                        state=self.state,
                        iteration=pass_index,
                        kind=domain.BudgetKind.TOOL_CALLS,
                        limit_value=limit,
                        observed_value=used,
                        projected_value=used + charge,
                    )
                )
                return self._terminate(pass_index, domain.Trigger.BUDGET_EXCEEDED)
            used += charge

            if malformed is not None:
                self._transcript.append(
                    FeedbackItem(
                        FeedbackKind.MALFORMED_RESPONSE,
                        pass_index,
                        {"call_index": malformed.call_index, "reason": malformed.reason},
                    )
                )
                continue

            plan: PlanProposal | None = None
            plan_seen = False
            terminal: OperationalError | None = None
            for call in classified.calls:
                self._cancellation_check(_TOOL_DISPATCH_SITE)
                seq += 1
                tool_call_id = f"tc-{pass_index}-{seq}"
                if plan_seen:
                    self._deny_call(pass_index, tool_call_id, call, "after_propose_plan")
                else:
                    if call.tool is domain.ToolName.PROPOSE_PLAN:
                        plan_seen = True
                    reason = self._policy_denial(call, citable)
                    if reason is not None:
                        self._deny_call(pass_index, tool_call_id, call, reason)
                    elif call.tool is domain.ToolName.PROPOSE_PLAN:
                        self._accept_plan_call(pass_index, tool_call_id, call)
                        plan = call.args
                    else:
                        terminal = self._execute_repository_call(pass_index, tool_call_id, call)
                        if terminal is not None:
                            break
                self._record_decision(_TOOL_DECISION_SITE, _DECISION_CHECKED)
                self._cancellation_check(_TOOL_DECISION_SITE)
            if terminal is not None:
                # The returned cleanup-unconfirmed route (§7.4): it outranks
                # cancellation, so the token is not consulted.
                self._record_decision(_TOOL_DECISION_SITE, _DECISION_OPERATIONAL)
                return self._terminate(pass_index, domain.Trigger.UNRECOVERABLE_ERROR, error=terminal)
            if plan is not None:
                return self._propose(pass_index, plan)

    def _policy_denial(self, call: ParsedCall, citable: frozenset[str]) -> str | None:
        """The policy stage, before any execution or filesystem access. A
        categorical reason, or None if the call may run."""
        if call.tool not in domain.legal_tools(self.state):
            return "tool_not_legal_in_state"
        if call.args is None:
            rejection = call.rejection
            if rejection is ArgumentRejection.ADMINISTRATIVE_PATH:
                return "administrative_path"
            if rejection is not None and rejection.value.startswith("path_"):
                return f"path_not_permitted:{rejection.value}"
            if rejection in (ArgumentRejection.CURSOR_INVALID, ArgumentRejection.CURSOR_MISMATCH):
                return f"cursor_invalid:{rejection.value}"
            return f"arguments_invalid:{rejection.value if rejection else 'unknown'}"
        if isinstance(call.args, SearchTextArgs) and call.args.cursor is not None:
            rejection_text = self._tools.check_search_cursor(call.args)
            if rejection_text is not None:
                return f"cursor_invalid:{rejection_text}"
        if isinstance(call.args, ProposePlanArgs) and not set(call.args.evidence_refs) <= citable:
            return "arguments_invalid:evidence_ref_invalid"
        return None

    def _redacted(self, call: ParsedCall) -> str:
        if call.args is not None:
            return redact_arguments(call.args)
        return redact_invalid_arguments(call.rejection or ArgumentRejection.WRONG_TYPE, call.raw_arguments)

    def _emit_tool_request(self, pass_index: int, tool_call_id: str, call: ParsedCall, allowed: bool, reason: str) -> None:
        self._emit(
            events.ToolRequested(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                tool=call.tool,
                tool_call_id=tool_call_id,
                redacted_arguments_json=self._redacted(call),
                provider_call_id=call.provider_call_id,
            )
        )
        self._emit(
            events.PolicyDecisionRecorded(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                tool=call.tool,
                tool_call_id=tool_call_id,
                allowed=allowed,
                reason=reason,
            )
        )

    def _emit_tool_completed(
        self,
        pass_index: int,
        tool: domain.ToolName,
        tool_call_id: str,
        *,
        duration: float,
        summary: str = "",
        error: OperationalError | None = None,
    ) -> None:
        self._emit(
            events.ToolCompleted(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                tool=tool,
                tool_call_id=tool_call_id,
                success=error is None,
                duration_seconds=duration,
                result_summary=summary,
                error=error,
            )
        )

    def _deny_call(self, pass_index: int, tool_call_id: str, call: ParsedCall, reason: str) -> None:
        self._emit_tool_request(pass_index, tool_call_id, call, False, reason)
        self._emit_tool_completed(
            pass_index,
            call.tool,
            tool_call_id,
            duration=0.0,
            error=OperationalError(
                code=ErrorCode.TOOL_INPUT_INVALID,
                error_id=f"{self._c.run_id}-{tool_call_id}-denied",
                message=f"tool call denied: {reason}",
            ),
        )
        self._transcript.append(
            FeedbackItem(
                FeedbackKind.TOOL_DENIED,
                pass_index,
                {
                    "provider_call_id": call.provider_call_id,
                    "reason": reason,
                    "tool": call.tool.value,
                    "tool_call_id": tool_call_id,
                },
            )
        )

    def _accept_plan_call(self, pass_index: int, tool_call_id: str, call: ParsedCall) -> None:
        self._emit_tool_request(pass_index, tool_call_id, call, True, f"propose_plan is legal in state {self.state.value}")
        self._emit_tool_completed(pass_index, call.tool, tool_call_id, duration=0.0, summary="plan proposed")

    def _execute_repository_call(
        self, pass_index: int, tool_call_id: str, call: ParsedCall
    ) -> OperationalError | None:
        """Run one allowed repository tool with the §7.3 wrapper. Returns the
        fixed error of the cleanup-unconfirmed route (terminal), else None.
        A tool body that raised keeps its `ToolCompleted` pairing and then
        propagates; it gets no C27 decision."""
        self._emit_tool_request(pass_index, tool_call_id, call, True, f"{call.tool.value} is legal in state {self.state.value}")
        # The method is looked up inside the guard, so even a failing
        # lookup keeps the ToolRequested/ToolCompleted pairing.
        assert call.tool in _REPOSITORY_TOOLS
        start = self._clock.monotonic()
        cleanup: ToolCleanupUnconfirmedError | None = None
        try:
            outcome = getattr(self._tools, call.tool.value)(call.args, self._should_stop)
            # Validated inside the guard, so a wrong return type still gets
            # its ToolCompleted; the message never names the returned type.
            if not isinstance(outcome, ToolOutcome):
                raise TypeError("a repository tool did not return a ToolOutcome")
        except ToolCleanupUnconfirmedError as exc:
            cleanup = exc
        except Exception:
            failed_duration = max(0.0, self._clock.monotonic() - start)
            self._total_duration += failed_duration
            self._emit_tool_completed(
                pass_index,
                call.tool,
                tool_call_id,
                duration=failed_duration,
                error=OperationalError(
                    code=ErrorCode.TOOL_EXECUTION_FAILED,
                    error_id=f"{self._c.run_id}-{tool_call_id}-unexpected",
                    message="tool failed unexpectedly",
                ),
            )
            raise
        except BaseException as interrupt:
            try:
                self._emit_tool_completed(
                    pass_index,
                    call.tool,
                    tool_call_id,
                    duration=max(0.0, self._clock.monotonic() - start),
                    error=OperationalError(
                        code=ErrorCode.TOOL_EXECUTION_FAILED,
                        error_id=f"{self._c.run_id}-{tool_call_id}-interrupted",
                        message="tool interrupted",
                    ),
                )
            except BaseException as emit_failure:  # noqa: BLE001 - the interrupt must stay primary
                attach_cleanup_failures(interrupt, (emit_failure,))
            raise interrupt
        duration = max(0.0, self._clock.monotonic() - start)
        self._total_duration += duration
        if cleanup is not None:
            return self._record_tool_cleanup(pass_index, call.tool, tool_call_id, cleanup, duration)
        self._emit_tool_completed(
            pass_index, call.tool, tool_call_id, duration=duration, summary=outcome.summary, error=outcome.error
        )
        body: dict[str, object] = {
            "ok": outcome.ok,
            "provider_call_id": call.provider_call_id,
            "tool": call.tool.value,
            "tool_call_id": tool_call_id,
        }
        if outcome.ok:
            body["payload"] = outcome.payload
        else:
            assert outcome.error is not None
            body["error"] = {"code": outcome.error.code.value, "message": outcome.error.message}
        self._transcript.append(FeedbackItem(FeedbackKind.TOOL_RESULT, pass_index, body))
        return None

    def _propose(self, pass_index: int, plan: PlanProposal) -> tuple[PlanProposal, PlanDigests, int, bool]:
        self._plan_revision += 1
        revision = self._plan_revision
        digests = compute_digests(plan)
        material = digests.change_digest != self._last_approved_change_digest
        self._emit(
            events.PlanProposed(
                run_id=self._c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=pass_index,
                problem_hypothesis=sanitize_recorded_text(plan.problem_hypothesis),
                evidence_refs=plan.evidence_refs,
                proposed_file_paths=(plan.change.path,),
                verification_intent=sanitize_recorded_text(plan.verification_intent),
                risk_notes=sanitize_recorded_text(plan.risk_notes),
                plan_revision=revision,
                plan_digest=digests.plan_digest,
                change_digest=digests.change_digest,
                material_change=material,
            )
        )
        return plan, digests, revision, material

    def _record_tool_cleanup(
        self,
        iteration: int,
        tool: domain.ToolName,
        tool_call_id: str,
        cleanup: ToolCleanupUnconfirmedError,
        duration: float,
    ) -> OperationalError:
        """The only place a repository-tool descriptor-cleanup failure
        becomes events and state (D4 plan revision 5 §7.4). Retains the
        error by identity, emits exactly one failed `ToolCompleted` with a
        fixed message, defers a body interrupt under D3's first-wins rule,
        and returns the fixed error. It never calls `_terminate`; the
        caller's existing terminal owner does, and the returned failure
        outranks a pending cancellation (ADR 0005 Amendment 1 §3)."""
        self._tool_cleanup_failures.append(cleanup)
        error = OperationalError(
            code=ErrorCode.TOOL_EXECUTION_FAILED,
            error_id=f"{self._c.run_id}-tool-cleanup-{len(self._tool_cleanup_failures)}",
            message=ToolCleanupUnconfirmedError.MESSAGE,
        )
        body = cleanup.body_failure
        interrupt = body if body is not None and not isinstance(body, Exception) else None
        try:
            self._emit(
                events.ToolCompleted(
                    run_id=self._c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=iteration,
                    tool=tool,
                    tool_call_id=tool_call_id,
                    success=False,
                    duration_seconds=duration,
                    result_summary="",
                    error=error,
                )
            )
        except BaseException as emit_exc:
            if interrupt is not None:
                attach_cleanup_failures(emit_exc, (interrupt,))
            raise
        if interrupt is not None and self._deferred_interrupt is None:
            self._deferred_interrupt = interrupt
        return error

    def _map_checkpoint_error(self, exc: Exception, iteration: int) -> OperationalError:
        """ADR 0003 Amendment 2's exact reason+MutationOutcome mapping.

        `CheckpointRefError` (a Git-level checkpoint-ref failure): the
        code is chosen from the confirmed `MutationOutcome`, not the
        categorical reason alone — the same outcome-first discipline
        `checkpoint_session.py` itself uses:
        - `UNCHANGED` with a plain compare-and-swap rejection (no
          accompanying command/protocol failure) ->
          `CHECKPOINT_REF_UPDATE_REJECTED`.
        - `UNCHANGED` accompanying a known command/protocol failure
          (a timeout, a launch failure, an unacknowledged transaction
          stage) -> `CHECKPOINT_REF_OPERATION_FAILED`.
        - `UNEXPECTED` or `SYMBOLIC` -> `CHECKPOINT_REF_UNEXPECTED_STATE`.
        - `UNKNOWN` (including `TRANSACTION_CLEANUP_UNCONFIRMED`, which
          always forces `UNKNOWN`) -> `CHECKPOINT_REF_OUTCOME_UNKNOWN`.

        `CheckpointSessionError` (a session-state misuse, e.g. a
        malformed commit hash, or calling establish/advance from the
        wrong intent) is a controller-facing invariant violation, not a
        Git-level failure with an outcome to classify.

        `CheckpointPublicationError` (Milestone 3 Slice 3B-7, ADR 0004
        Amendment 7): the durable lifecycle-projection record of a
        checkpoint-ref transition could not be confirmed published.
        Whether the Git mutation itself was ever reached depends on
        which publication phase failed — a pre-mutation (transitional-
        intent) publication failure means Git was never attempted; a
        post-mutation (collapse) or confirmed-UNCHANGED-recovery
        publication failure means the Git mutation already concluded
        (successfully, or confirmed UNCHANGED and recovered) before
        this failure occurred. This branch never asserts which case
        applies. This is a distinct failure surface from every
        `CheckpointRefError` outcome above — always
        `CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED`, with its own fixed,
        sanitized message (distinct from the `CheckpointRefError`/
        `CheckpointSessionError` message below) — never folded into the
        `CheckpointSessionError` fallback branch (which would wrongly
        report it as a programming-invariant violation). The exception's
        own finer-grained `reason`, and any persistence-layer detail it
        might carry, are never persisted in the `OperationalError` —
        `reason` remains available in-process only.
        """
        if isinstance(exc, CheckpointRefError):
            if exc.outcome is MutationOutcome.UNCHANGED:
                code = (
                    ErrorCode.CHECKPOINT_REF_UPDATE_REJECTED
                    if exc.reason is CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED
                    else ErrorCode.CHECKPOINT_REF_OPERATION_FAILED
                )
            elif exc.outcome in (MutationOutcome.UNEXPECTED, MutationOutcome.SYMBOLIC):
                code = ErrorCode.CHECKPOINT_REF_UNEXPECTED_STATE
            else:
                code = ErrorCode.CHECKPOINT_REF_OUTCOME_UNKNOWN
            message = "the checkpoint-ref operation for this patch could not be accepted"
        elif isinstance(exc, CheckpointPublicationError):
            code = ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED
            # Deliberately distinct, fixed, sanitized text — never the
            # exception's own message/reason, which could describe
            # persistence-layer detail this taxonomy never exposes.
            message = "the checkpoint lifecycle projection transition could not be confirmed"
        else:
            code = ErrorCode.INTERNAL_INVARIANT_VIOLATION
            message = "the checkpoint-ref operation for this patch could not be accepted"
        return OperationalError(
            code=code,
            error_id=f"{self._c.run_id}-checkpoint-{iteration}",
            message=message,
        )

    def _dispatch_apply_patch(
        self, iteration: int, approved: ApprovedPlan
    ) -> tuple[bool, OperationalError | None]:
        """One `apply_patch` transaction, in ADR 0003 Amendment 1/
        Amendment 2's exact accepted order: ToolRequested,
        PolicyDecisionRecorded, the workspace entry gate, lazy
        checkpoint establishment (only from ABSENT), the patch itself,
        the approved-path postcondition, the checkpoint-ref advance,
        and only then ToolCompleted(success=True), CheckpointCreated,
        and PatchApplied — in that order. Every failure emits exactly
        one `ToolCompleted(success=False)` carrying one `OperationalError`
        and produces no success-shaped event at all. Returns (ok, error).
        """
        c = self._c
        # Both digests are recomputed from the stored immutable proposal
        # before anything is dispatched; a mismatch is a controller bug and
        # fails closed (Slice 3C-3 -> UNCLASSIFIED_FAILURE).
        if compute_digests(approved.args) != approved.digests:
            raise RuntimeError("the approved plan no longer matches its digests")
        change = approved.args.change
        approved_paths = frozenset({change.path})
        tool_call_id = f"{c.run_id}-tc-patch-{iteration}"
        self._dispatch_tool(
            iteration,
            domain.ToolName.APPLY_PATCH,
            tool_call_id,
            {
                "change_digest": approved.digests.change_digest,
                "expected_sha256": hashlib.sha256(change.expected_text.encode("utf-8")).hexdigest(),
                "new_sha256": hashlib.sha256(change.new_text.encode("utf-8")).hexdigest(),
                "path": change.path,
                "plan_revision": approved.revision,
            },
        )

        start = self._clock.monotonic()

        def _fail(error: OperationalError) -> tuple[bool, OperationalError]:
            duration = self._clock.monotonic() - start
            self._total_duration += duration
            self._emit(
                events.ToolCompleted(
                    run_id=c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=iteration,
                    tool=domain.ToolName.APPLY_PATCH,
                    tool_call_id=tool_call_id,
                    success=False,
                    duration_seconds=duration,
                    result_summary="",
                    error=error,
                )
            )
            return False, error

        # Steps 3-7 run behind one guard (ledger D4b correction pass): an
        # unexpected exception from any collaborator still completes this
        # tool triple with one sanitized failure, counts its duration once,
        # and re-raises (Slice 3C-3). All emission stays outside the guard,
        # so a failing emission never causes a second ToolCompleted.
        try:
            failure, result = self._run_patch_transaction(iteration, approved_paths, change)
        except Exception:
            failed_duration = max(0.0, self._clock.monotonic() - start)
            self._total_duration += failed_duration
            self._emit_tool_completed(
                iteration,
                domain.ToolName.APPLY_PATCH,
                tool_call_id,
                duration=failed_duration,
                error=OperationalError(
                    code=ErrorCode.TOOL_EXECUTION_FAILED,
                    error_id=f"{tool_call_id}-unexpected",
                    message="tool failed unexpectedly",
                ),
            )
            raise
        except BaseException as interrupt:
            try:
                self._emit_tool_completed(
                    iteration,
                    domain.ToolName.APPLY_PATCH,
                    tool_call_id,
                    duration=max(0.0, self._clock.monotonic() - start),
                    error=OperationalError(
                        code=ErrorCode.TOOL_EXECUTION_FAILED,
                        error_id=f"{tool_call_id}-interrupted",
                        message="tool interrupted",
                    ),
                )
            except BaseException as emit_failure:  # noqa: BLE001 - the interrupt must stay primary
                attach_cleanup_failures(interrupt, (emit_failure,))
            raise interrupt
        if failure is not None:
            return _fail(failure)

        # Steps 8-10: only now, with the transaction fully accepted.
        duration = self._clock.monotonic() - start
        self._total_duration += duration
        self._emit(
            events.ToolCompleted(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                tool=domain.ToolName.APPLY_PATCH,
                tool_call_id=tool_call_id,
                success=True,
                duration_seconds=duration,
                result_summary=f"{result.operation_count} operation(s) applied",
            )
        )
        checkpoint_id = result.commit_hash
        self._emit(
            events.CheckpointCreated(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=self._last_checkpoint_id,
                commit_hash=result.commit_hash,
            )
        )
        self._last_checkpoint_id = checkpoint_id
        self._emit(
            events.PatchApplied(
                run_id=c.run_id,
                sequence=self.log.next_sequence(),
                timestamp=self._now(),
                state=self.state,
                iteration=iteration,
                operation_count=result.operation_count,
                files_changed=result.changed_paths,
                checkpoint_id=checkpoint_id,
                diff_bytes=result.diff_bytes,
                plan_revision=approved.revision,
                change_digest=approved.digests.change_digest,
            )
        )
        return True, None

    def _run_patch_transaction(
        self, iteration: int, approved_paths: frozenset[str], change: ProposedChange
    ) -> tuple[OperationalError | None, PatchResult | None]:
        """Steps 3-7 of `_dispatch_apply_patch`, emitting nothing: either a
        mapped failure, or the accepted `PatchResult` once the checkpoint
        ref has advanced. Unexpected exceptions propagate to the caller's
        guard."""
        c = self._c
        # Step 3: the workspace entry gate. The expected commit is the
        # sole trusted initial checkpoint SHA while no ref exists yet
        # (ABSENT), or the ref's currently accepted checkpoint once one
        # does — never a separately tracked value of this controller's
        # own.
        expected_commit = (
            self._workspace.initial_commit
            if self._session.intent is CheckpointIntent.ABSENT
            else self._session.accepted_sha
        )
        try:
            self._workspace.entry_gate(expected_commit)
        except Exception as exc:  # noqa: BLE001 — any Workspace implementation's own exception type
            return (
                OperationalError(
                    code=ErrorCode.WORKSPACE_ENTRY_GATE_FAILED,
                    error_id=f"{c.run_id}-gate-{iteration}",
                    message="the workspace entry gate refused this patch attempt",
                ),
                None,
            )

        # Step 4: lazy establishment, only from ABSENT (ADR 0003
        # Amendment 1 point 4 / Amendment 2 section 1).
        if self._session.intent is CheckpointIntent.ABSENT:
            try:
                self._session.establish(self._workspace.initial_commit)
            except (CheckpointRefError, CheckpointSessionError, CheckpointPublicationError) as exc:
                return self._map_checkpoint_error(exc, iteration), None

        # Step 5: the patch itself.
        # The approved, digest-bound model proposal is the sole source of
        # patch operations (ledger D4b).
        result = self._patch_applier.apply(
            c.run_id, iteration, approved_paths, (PatchOperation.from_change(change),)
        )
        if not isinstance(result, PatchResult):
            raise TypeError("the patch applier did not return a PatchResult")
        if not result.success:
            return result.error, None

        # Step 6: the approved-path postcondition — defense-in-depth,
        # not the primary enforcement (see PatchApplier's docstring and
        # codeagent.patch.GitPatchApplier.apply, which must reject an
        # unapproved target before any write or commit happens at all).
        actual_paths = set(result.changed_paths)
        if not actual_paths.issubset(approved_paths):
            return (
                OperationalError(
                    code=ErrorCode.INTERNAL_INVARIANT_VIOLATION,
                    error_id=f"{c.run_id}-scope-{iteration}",
                    message="patch changed files outside the approved plan's proposed_file_paths",
                ),
                None,
            )

        # Step 7: advance the checkpoint ref to the newly accepted commit.
        try:
            self._session.advance(result.commit_hash)
        except (CheckpointRefError, CheckpointSessionError, CheckpointPublicationError) as exc:
            return self._map_checkpoint_error(exc, iteration), None
        return None, result

    def _fallback_evidence_receipt(self, pass_index: int, reason: str) -> EvidenceReceipt:
        return EvidenceReceipt(
            success=False,
            complete=False,
            artifact_id=None,
            sha256_payload=None,
            bytes_written=None,
            error=OperationalError(
                code=ErrorCode.EVIDENCE_CAPTURE_FAILED,
                error_id=f"{self._c.run_id}-evidence-{reason}-{pass_index}",
                message=f"the evidence capture step failed unexpectedly ({reason})",
            ),
        )

    def _capture_evidence(self, pass_index: int) -> EvidenceReceipt:
        """Always attempted, regardless of the run's underlying domain
        outcome, and never blocking any subsequent cleanup step (ADR
        0003 Amendment 2) — including when the `EvidenceSink` itself
        misbehaves. An `EvidenceSink` is expected to report its own
        failures as a well-formed `EvidenceReceipt` (see
        `evidence.FilesystemEvidenceSink`), but this method does not
        trust that contract blindly: an unexpected exception from
        either the sink's `capture()` call or from constructing the
        resulting `EvidenceCaptured` event (e.g. a malformed receipt)
        is converted into a sanitized `EVIDENCE_CAPTURE_FAILED` result
        instead of propagating and aborting the rest of `_terminate`.
        Emitted in the run's current (pre-terminal) state, matching
        Event's ordering convention for a causative event.
        """
        try:
            receipt = self._evidence_sink.capture(
                worktree_path=self._workspace.path,
                source_repo_path=self._workspace.source_repo_path,
                initial_commit=self._workspace.initial_commit,
                lifecycle_id=self._c.lifecycle_id,
            )
        except Exception:  # noqa: BLE001 — a sink must never be able to block teardown
            receipt = self._fallback_evidence_receipt(pass_index, "sink-raised")

        try:
            self._emit(
                events.EvidenceCaptured(
                    run_id=self._c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    success=receipt.success,
                    complete=receipt.complete,
                    artifact_id=receipt.artifact_id,
                    sha256_payload=receipt.sha256_payload,
                    bytes_written=receipt.bytes_written,
                    error=receipt.error,
                )
            )
        except Exception:  # noqa: BLE001 — a malformed receipt must not block teardown either
            receipt = self._fallback_evidence_receipt(pass_index, "receipt-malformed")
            self._emit(
                events.EvidenceCaptured(
                    run_id=self._c.run_id,
                    sequence=self.log.next_sequence(),
                    timestamp=self._now(),
                    state=self.state,
                    iteration=pass_index,
                    success=False,
                    complete=False,
                    error=receipt.error,
                )
            )
        return receipt

    def _terminate(
        self,
        pass_index: int,
        trigger: domain.Trigger,
        error: OperationalError | None = None,
    ) -> events.RunFinished:
        """The single terminal path, replacing every direct
        `_transition` + `_finish` call (ADR 0003 Amendment 2).

        1. Always attempt durable evidence capture.
        2. If any verifier result this run was cleanup-UNCONFIRMED:
           preserve the workspace and skip both worktree disposal and
           checkpoint-ref deletion.
        3. Otherwise dispose the workspace.
        4. If disposal itself could not be confirmed: skip checkpoint-ref
           deletion (an unconfirmed worktree may still reference it).
        5. Otherwise delete the checkpoint session's ref (a no-op from
           ABSENT — no `apply_patch` ever occurred).
        6. Determine the final trigger/error per the terminal precedence
           below, perform the (possibly overridden) domain transition,
           and emit `RunFinished` last.

        Precedence for `RunFinished.error`: any verifier/worktree/ref
        cleanup unconfirmed, or any owner-state lifecycle-publication
        failure (`begin_cleanup()`/`complete()`, Milestone 3 Slice
        3C-1) > evidence incomplete > evidence durability unconfirmed >
        evidence artifact collision > evidence capture failure > the
        original result. A cleanup-unconfirmed or evidence failure
        always overrides `trigger` to `UNRECOVERABLE_ERROR`, since
        domain.py's `TerminalReason` has no separate slot for "the
        original outcome succeeded but teardown did not" — the original
        `error` (if any) stays exactly where it was already recorded
        (e.g. on the `ToolCompleted` that reported it); only
        `RunFinished.error` is replaced.

        Owner-state publication (Slice 3C-1): `begin_cleanup()` is
        attempted first, before evidence capture — its failure is
        recorded but never skips evidence capture or the resource-
        cleanup steps below, which always run regardless.  `complete()`
        is attempted once `begin_cleanup()` succeeded and all owned-
        resource cleanup (verifier/worktree/ref) is confirmed —
        deliberately independent of `evidence_receipt.success`: a
        `lifecycle.json` operational-recovery projection reaching
        `COMPLETE` and an `EvidenceSink` audit-capture failure are
        reported through separate channels and must never be
        conflated. Both `begin_cleanup()`/`complete()` are only ever
        attempted when `self._lifecycle_owner_active` is true (i.e.
        `activate()` was already confirmed) — `PREPARING -> CLEANING`
        is not a legal owner-state edge. Neither call is ever retried
        or followed by an automatic `refresh()`.

        Milestone 3 Slice 3C-3 (ADR 0004 Amendment 9): a genuine one-shot
        guard, checked and set before any of the above — not a bare flag
        write. A second call (a logic bug, never expected under normal
        control flow, since every existing call site is an immediate
        `return self._terminate(...)`) is refused outright, before any
        additional evidence capture, resource cleanup, lifecycle
        publication, transition, or event emission. This method's own
        cleanup order and precedence are otherwise unchanged by this
        guard.
        """
        if self._termination_started:
            raise RuntimeError("run termination has already started")
        self._termination_started = True

        lifecycle_publication_unconfirmed = False
        if self._lifecycle_owner_active and self._lifecycle_owner is not None:
            try:
                self._lifecycle_owner.begin_cleanup()
            except OwnerStatePublicationError:
                lifecycle_publication_unconfirmed = True

        evidence_receipt = self._capture_evidence(pass_index)

        worktree_cleanup_unconfirmed = False
        ref_cleanup_unconfirmed = False
        if self._any_verifier_cleanup_unconfirmed:
            try:
                self._workspace.preserve()
            except Exception:  # noqa: BLE001 — a preserve() failure must not block RunFinished
                worktree_cleanup_unconfirmed = True
        else:
            try:
                self._workspace.dispose()
            except Exception:  # noqa: BLE001 — any Workspace implementation's own exception type
                worktree_cleanup_unconfirmed = True
            if not worktree_cleanup_unconfirmed:
                try:
                    self._session.delete()
                except Exception:  # noqa: BLE001 — CheckpointRefError or a fake's own type
                    ref_cleanup_unconfirmed = True

        if (
            self._lifecycle_owner_active
            and self._lifecycle_owner is not None
            and not lifecycle_publication_unconfirmed
            and not self._any_verifier_cleanup_unconfirmed
            and not worktree_cleanup_unconfirmed
            and not ref_cleanup_unconfirmed
        ):
            try:
                self._lifecycle_owner.complete()
            except OwnerStatePublicationError:
                lifecycle_publication_unconfirmed = True

        cleanup_unconfirmed = (
            self._any_verifier_cleanup_unconfirmed
            or worktree_cleanup_unconfirmed
            or ref_cleanup_unconfirmed
            or lifecycle_publication_unconfirmed
        )

        final_trigger = trigger
        final_error = error
        if cleanup_unconfirmed:
            final_trigger = domain.Trigger.UNRECOVERABLE_ERROR
            final_error = OperationalError(
                code=ErrorCode.LIFECYCLE_CLEANUP_UNCONFIRMED,
                error_id=f"{self._c.run_id}-cleanup-{pass_index}",
                message="a verifier container, worktree, or checkpoint-ref cleanup step, "
                "or the run's lifecycle-projection bookkeeping, could not be confirmed",
            )
        elif not evidence_receipt.success:
            final_trigger = domain.Trigger.UNRECOVERABLE_ERROR
            final_error = evidence_receipt.error

        result = self._transition(pass_index, final_trigger)
        return self._finish(pass_index, result.terminal_reason, error=final_error)

    def _finish(
        self,
        pass_index: int,
        terminal_reason: domain.TerminalReason,
        error: OperationalError | None = None,
    ) -> events.RunFinished:
        """`iterations_used` is the number of exploration passes begun
        (ADR 0005 Amendment 1 section 8, as corrected at implementation):
        for every run that reached its first pass this equals
        `pass_index + 1`, not the zero-based `pass_index` itself, so a run
        that reaches DONE on its very first pass reports 1. A run that ends
        before its first pass (activation failure, a baseline operational
        failure or cleanup-unconfirmed, or cancellation at C1-C4) reports 0.
        """
        finished = events.RunFinished(
            run_id=self._c.run_id,
            sequence=self.log.next_sequence(),
            timestamp=self._now(),
            state=self.state,
            iteration=pass_index,
            terminal_reason=terminal_reason,
            total_cost_microusd=0,
            total_tokens=0,
            total_duration_seconds=self._total_duration,
            iterations_used=self._passes_begun,
            error=error,
        )
        self._emit(finished)
        return finished
