"""Deterministic test doubles implementing controller.py's collaborator
Protocols (ModelClient, RepositoryTools, ApprovalProvider, Verifier,
PatchApplier, Clock).

Not production code. Lives under tests/ specifically so it can never be
imported from src/codeagent by accident — see the correction-pass
finding that flagged the previous version of controller.py for
depending on concrete fake types directly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codeagent import domain, events
from codeagent.checkpoint_session import CheckpointIntent
from codeagent.controller import ApprovalResponse, PatchResult, PlanProposal, VerificationResult
from codeagent.errors import ErrorCode, OperationalError
from codeagent.evidence import EvidenceReceipt
from codeagent.lifecycle_owner import OwnerStatePublicationError
from codeagent.model_protocol import FeedbackKind, ModelContext, ModelTurn
from codeagent.tool_schema import ProposedChange, ProposePlanArgs, ToolOutcome
from tests.support.scripted_model import propose_plan_call, read_file_call, turn

# FakeVerifier's own choice, not a controller-level restriction: this
# synthetic harness only fabricates PASSED/TEST_FAILURE, since it has
# no real executor to give TIMEOUT/ENVIRONMENT_FAILURE/
# COMMAND_START_FAILURE genuine meaning. codeagent.executor.DockerVerifier
# is not restricted this way — see its own tests.
_FAKE_SUPPORTED_OUTCOMES = frozenset(
    {events.VerificationOutcome.PASSED, events.VerificationOutcome.TEST_FAILURE}
)
_FAKE_EXIT_CODE_BY_OUTCOME = {
    events.VerificationOutcome.PASSED: 0,
    events.VerificationOutcome.TEST_FAILURE: 1,
}

# The one real verification command this project's fixture uses (see
# tests/fixtures/retry_worker/tests/test_worker.py and
# codeagent.executor.DEFAULT_COMMAND) — FakeVerifier defaults to the
# same value so a test trace's recorded command matches what slice C's
# real DockerVerifier actually executes, unless a test overrides it.
FIXTURE_VERIFY_COMMAND: tuple[str, ...] = (
    "python3",
    "-B",
    "-m",
    "unittest",
    "tests.test_worker",
)


def _require_fake_supported(name: str, outcomes: tuple[events.VerificationOutcome, ...]) -> None:
    unsupported = sorted({o.value for o in outcomes} - {o.value for o in _FAKE_SUPPORTED_OUTCOMES})
    if unsupported:
        raise ValueError(
            f"FakeVerifier's {name} only supports "
            f"{sorted(o.value for o in _FAKE_SUPPORTED_OUTCOMES)} — this synthetic harness "
            "has no real executor to give TIMEOUT/ENVIRONMENT_FAILURE/"
            f"COMMAND_START_FAILURE real meaning; got {unsupported}"
        )


# Placeholder evidence: the fakes below replace it with the controller-
# minted ID of the read they actually received (ledger D4b, §8.3).
_PLACEHOLDER_EVIDENCE = ("tc-0-1",)


def make_plan(
    path: str = "jobs/worker.py",
    *,
    problem_hypothesis: str = "idempotency key dropped on retry",
    verification_intent: str = "python3 -B -m unittest tests.test_worker",
    expected_text: str = "    job.retry_count += 1\n",
    new_text: str = "    pass\n",
    risk_notes: str = "",
) -> PlanProposal:
    """A model-proposable plan with a placeholder evidence reference."""
    return ProposePlanArgs(
        problem_hypothesis=problem_hypothesis,
        evidence_refs=_PLACEHOLDER_EVIDENCE,
        change=ProposedChange(path, expected_text, new_text),
        verification_intent=verification_intent,
        risk_notes=risk_notes,
    )


@dataclass(frozen=True)
class ReadEvidence:
    """What a fake model actually received from its last read."""

    success: bool
    content: str | None


def _last_read(context: ModelContext) -> tuple[str, ReadEvidence] | None:
    """The newest current-pass `read_file` result in the context: its
    controller-minted tool-call ID and its content."""
    for item in reversed(context.transcript):
        body = item.body
        if item.kind is FeedbackKind.TOOL_RESULT and item.pass_index == context.pass_index and body["tool"] == "read_file":
            if not body["ok"]:
                return body["tool_call_id"], ReadEvidence(False, None)
            content = "".join(text for _, text in body["payload"]["lines"])
            return body["tool_call_id"], ReadEvidence(True, content)
    return None


class FakeModel:
    """A deterministic two-turn model per pass (ledger D4b): the first turn
    reads `read_path`, the next proposes `plan`, citing the controller-minted
    ID of the read it received. It never conditions its plan on the read's
    content (see `MarkerGatedFakeModel`), but records it as
    `last_read_result`. A failed read is retried, so a reader that always
    fails exhausts the per-pass tool-call budget.

    `read_path` defaults to the plan's own target path."""

    model_name = "fake-model-v1"

    def __init__(self, plan: PlanProposal, read_path: str | None = None) -> None:
        self._plan = plan
        self._read_path = read_path if read_path is not None else plan.change.path
        self.last_read_result: ReadEvidence | None = None
        self.contexts: list[ModelContext] = []

    def _proposal(self, context: ModelContext) -> ModelTurn:
        found = _last_read(context)
        if found is None or not found[1].success:
            if found is not None:
                self.last_read_result = found[1]
            return turn(read_file_call(self._read_path))
        tool_call_id, evidence = found
        self.last_read_result = evidence
        self._check(evidence)
        return turn(propose_plan_call(dataclasses.replace(self._plan, evidence_refs=(tool_call_id,))))

    def _check(self, evidence: ReadEvidence) -> None:
        pass

    def respond(self, context: ModelContext) -> ModelTurn:
        self.contexts.append(context)
        return self._proposal(context)


class MarkerGatedFakeModel(FakeModel):
    """Test-only model whose plan is genuinely conditioned on the read
    evidence it receives: it proposes only after a successful read whose
    content contains `marker`, and raises otherwise. Used to prove the
    controller passes real content to the model before a plan exists."""

    def __init__(self, read_path: str, marker: str, plan: PlanProposal) -> None:
        super().__init__(plan, read_path)
        self._marker = marker

    def _check(self, evidence: ReadEvidence) -> None:
        if not evidence.success or self._marker not in (evidence.content or ""):
            raise AssertionError(
                "MarkerGatedFakeModel was asked to propose without the expected "
                f"read evidence (marker {self._marker!r} not found) — the controller "
                "must dispatch a successful read_file and pass its real content "
                "before the model proposes a plan"
            )


class FakeApprovalProvider:
    """Answers each `decide()` with the next configured decision (the last
    one repeats). A revision request carries a meaningful note, as the
    accepted `ApprovalResponse` contract requires."""

    def __init__(
        self, decisions: tuple[domain.ApprovalDecision, ...], *, revision_note: str = "please revise the plan"
    ) -> None:
        if not decisions:
            raise ValueError("decisions must be a nonempty tuple")
        self._decisions = decisions
        self._revision_note = revision_note
        self.requests: list = []

    def decide(self, request) -> ApprovalResponse:
        self.requests.append(request)
        i = min(len(self.requests) - 1, len(self._decisions) - 1)
        decision = self._decisions[i]
        note = self._revision_note if decision is domain.ApprovalDecision.REVISION_REQUESTED else None
        return ApprovalResponse(decision, note)


class FakeVerifier:
    """`cleanup_status` defaults to `CONFIRMED_ABSENT` for every result —
    the honest default for a fake that never creates a real container
    and so has nothing genuinely ambiguous to report — and can be
    overridden (e.g. to `UNCONFIRMED`) so controller tests can exercise
    the verifier-cleanup-unconfirmed path without any real Docker
    involvement. `NOT_APPLICABLE` is not offered here: it requires
    `outcome` to be `COMMAND_START_FAILURE`/`ENVIRONMENT_FAILURE`, which
    this fake's `_FAKE_SUPPORTED_OUTCOMES` never produces.
    """

    def __init__(
        self,
        outcomes: tuple[events.VerificationOutcome, ...],
        baseline_outcome: events.VerificationOutcome = events.VerificationOutcome.TEST_FAILURE,
        command: tuple[str, ...] = FIXTURE_VERIFY_COMMAND,
        cleanup_status: events.ContainerCleanupStatus = events.ContainerCleanupStatus.CONFIRMED_ABSENT,
    ) -> None:
        if not outcomes:
            raise ValueError("outcomes must be a nonempty tuple")
        if not command:
            raise ValueError("command must be a nonempty tuple")
        _require_fake_supported("outcomes", outcomes)
        _require_fake_supported("baseline_outcome", (baseline_outcome,))
        self._outcomes = outcomes
        self._baseline_outcome = baseline_outcome
        self._command = command
        self._cleanup_status = cleanup_status

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def _result(self, outcome: events.VerificationOutcome) -> VerificationResult:
        cleanup_status = self._cleanup_status
        effective_outcome = outcome
        if cleanup_status is events.ContainerCleanupStatus.UNCONFIRMED:
            # Mirrors DockerVerifier._execute's override: an unconfirmed
            # cleanup always forces ENVIRONMENT_FAILURE, regardless of
            # what the provisional outcome would otherwise have been.
            effective_outcome = events.VerificationOutcome.ENVIRONMENT_FAILURE
            return VerificationResult(
                outcome=effective_outcome,
                exit_code=None,
                duration_seconds=0.01,
                stdout="",
                stderr="",
                cleanup_status=cleanup_status,
                error=OperationalError(
                    code=ErrorCode.EXECUTOR_ENVIRONMENT_FAILURE,
                    error_id=f"fake-cleanup-unconfirmed-{outcome.value}",
                    message="fake verifier: cleanup deliberately unconfirmed",
                ),
            )
        return VerificationResult(
            outcome=effective_outcome,
            exit_code=_FAKE_EXIT_CODE_BY_OUTCOME[outcome],
            duration_seconds=0.01,
            stdout="",
            stderr="",
            cleanup_status=cleanup_status,
        )

    def run_baseline(self) -> VerificationResult:
        return self._result(self._baseline_outcome)

    def run(self, attempt_index: int) -> VerificationResult:
        i = min(attempt_index, len(self._outcomes) - 1)
        return self._result(self._outcomes[i])


class FakePatchApplier:
    """No real filesystem/git involved — see codeagent.patch.GitPatchApplier
    (slice B) for the real implementation this fakes.

    `changed_paths` defaults to the fixture's usual single file, but is
    overridable so a test can construct a PatchApplier that (mis)reports
    a file outside whatever the plan actually approved — see
    test_controller.py's plan-scope consistency test.
    """

    def __init__(
        self,
        should_fail: bool = False,
        changed_paths: tuple[str, ...] = ("jobs/worker.py",),
        failure_code: ErrorCode = ErrorCode.PATCH_VALIDATION_FAILED,
    ) -> None:
        self._should_fail = should_fail
        self._changed_paths = changed_paths
        self._failure_code = failure_code
        self._n = 0
        self.operations: list[tuple] = []

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str], operations: tuple
    ) -> PatchResult:
        self.operations.append(operations)
        # Deliberately does NOT enforce approved_paths itself — this
        # fake exists partly to let test_controller.py's plan-scope
        # test simulate a PatchApplier that fails to do the real,
        # preventive check GitPatchApplier does, exercising the
        # controller's defense-in-depth postcondition instead.
        self._n += 1
        if self._should_fail:
            return PatchResult(
                success=False,
                operation_count=0,
                changed_paths=(),
                diff_bytes=0,
                error=OperationalError(
                    code=self._failure_code,
                    error_id=f"{run_id}-fake-patch-err-{self._n}",
                    message="fixture-forced patch validation failure",
                ),
            )
        return PatchResult(
            success=True,
            operation_count=1,
            changed_paths=self._changed_paths,
            diff_bytes=42,
            commit_hash=f"{run_id}-fake-commit-{iteration}",
        )


class FakeRepositoryTools:
    """A filesystem-free `controller.RepositoryTools` double. `read_file`
    returns `content` for any path (or a fixed failure when `should_fail`);
    path validation is the real `RepoTools`'s job, exercised against real
    directories in `test_repo_tools.py` and the real Docker E2E."""

    def __init__(self, content: str = "fixture content", should_fail: bool = False) -> None:
        self._content = content
        self._should_fail = should_fail
        self._n = 0
        self.calls: list[tuple[str, object]] = []

    def _failure(self) -> ToolOutcome:
        self._n += 1
        return ToolOutcome(
            ok=False,
            payload=None,
            summary="",
            error=OperationalError(
                code=ErrorCode.TOOL_INPUT_INVALID,
                error_id=f"fake-read-err-{self._n}",
                message="fixture-forced read failure",
            ),
        )

    def read_file(self, args, should_stop) -> ToolOutcome:
        self.calls.append(("read_file", args))
        if self._should_fail:
            return self._failure()
        lines = [[i, line] for i, line in enumerate(self._content.splitlines(keepends=True), start=1)]
        payload = {"eof": True, "lines": lines, "next_start_line": None, "page_truncated": False}
        return ToolOutcome(ok=True, payload=payload, summary=f"{len(lines)} line(s)")

    def list_directory(self, args, should_stop) -> ToolOutcome:
        self.calls.append(("list_directory", args))
        payload = {"entries": [], "next_cursor": None, "page_truncated": False}
        return ToolOutcome(ok=True, payload=payload, summary="0 entr(ies)")

    def search_text(self, args, should_stop) -> ToolOutcome:
        self.calls.append(("search_text", args))
        payload = {
            "excluded": {},
            "next_cursor": None,
            "page_coverage_complete": True,
            "page_truncated": False,
            "results": [],
            "search_complete": True,
            "unsearched": {},
        }
        return ToolOutcome(ok=True, payload=payload, summary="0 result(s)")

    def check_search_cursor(self, args) -> str | None:
        return None


class SteppingClock:
    """Deterministic Clock: each call to now() advances a fixed step
    from a fixed epoch, and each call to monotonic() advances a fixed
    step from zero — two independent counters, fully reproducible. No
    real time, no sleeping, no monkeypatching datetime/time."""

    def __init__(
        self,
        epoch: datetime | None = None,
        step: timedelta = timedelta(milliseconds=1),
        monotonic_step: float = 0.001,
    ) -> None:
        self._next = epoch or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._step = step
        self._next_monotonic = 0.0
        self._monotonic_step = monotonic_step

    def now(self) -> datetime:
        current = self._next
        self._next = self._next + self._step
        return current

    def monotonic(self) -> float:
        current = self._next_monotonic
        self._next_monotonic += self._monotonic_step
        return current


class FakeWorkspace:
    """Deterministic `controller.Workspace` double (Milestone 2 slice
    2B-2): no filesystem or Git involved. `initial_commit` defaults to
    a fixed, valid-looking 40-hex SHA so it can be passed straight into
    a real `CheckpointSession.establish()`/`advance()` if a test wires
    one in; `FakeCheckpointSession` below doesn't require that shape at
    all.
    """

    def __init__(
        self,
        *,
        initial_commit: str = "a" * 40,
        path: Path | None = None,
        source_repo_path: Path | None = None,
    ) -> None:
        self._initial_commit = initial_commit
        self.path = path if path is not None else Path("/fake/worktree")
        self.source_repo_path = (
            source_repo_path if source_repo_path is not None else Path("/fake/source")
        )
        self.disposed = False
        self.preserved = False
        self.dispose_error: Exception | None = None
        self.entry_gate_calls: list[str] = []
        self.entry_gate_error: Exception | None = None

    @property
    def initial_commit(self) -> str:
        return self._initial_commit

    def entry_gate(self, expected_commit: str) -> None:
        self.entry_gate_calls.append(expected_commit)
        if self.entry_gate_error is not None:
            raise self.entry_gate_error

    def dispose(self) -> None:
        if self.dispose_error is not None:
            raise self.dispose_error
        self.disposed = True

    def preserve(self) -> None:
        self.preserved = True


class FakeCheckpointSession:
    """Deterministic `controller.CheckpointSessionLike` double: an
    in-memory `intent`/`accepted_sha` pair with no wrapped
    `CheckpointRef` and no Git call of its own. Failure injection uses
    the *real* `codeagent.checkpoint_ref.CheckpointRefError`/
    `codeagent.checkpoint_session.CheckpointSessionError` exception
    types, so `RunController._map_checkpoint_error`'s exact
    reason/outcome mapping is exercised identically to how it would be
    against the real `CheckpointSession`.
    """

    def __init__(self) -> None:
        self.intent: CheckpointIntent = CheckpointIntent.ABSENT
        self.accepted_sha: str | None = None
        self.establish_error: Exception | None = None
        self.advance_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.delete_calls = 0

    def establish(self, initial_sha: str) -> None:
        if self.establish_error is not None:
            raise self.establish_error
        self.intent = CheckpointIntent.PRESENT
        self.accepted_sha = initial_sha

    def advance(self, new_sha: str) -> None:
        if self.advance_error is not None:
            raise self.advance_error
        self.accepted_sha = new_sha

    def delete(self) -> None:
        self.delete_calls += 1
        if self.intent is CheckpointIntent.ABSENT:
            return
        if self.delete_error is not None:
            raise self.delete_error
        self.intent = CheckpointIntent.ABSENT
        self.accepted_sha = None


class FakeLifecycleOwnerPublisher:
    """Deterministic `controller.LifecycleOwnerPublisher` double
    (Milestone 3 Slice 3C-1): no filesystem, Git, or lifecycle-store
    involved. Records the exact call order of `activate()`/
    `begin_cleanup()`/`complete()` (as the string names) so a test can
    assert ordering directly, and raises the *real*
    `codeagent.lifecycle_owner.OwnerStatePublicationError` type when a
    failure is injected, so `RunController`'s exact catch/fold-into-
    precedence logic is exercised identically to how it would be
    against the real `LifecycleOwnerStatePublisher`."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.activate_error: OwnerStatePublicationError | None = None
        self.begin_cleanup_error: OwnerStatePublicationError | None = None
        self.complete_error: OwnerStatePublicationError | None = None

    def activate(self) -> None:
        self.calls.append("activate")
        if self.activate_error is not None:
            raise self.activate_error

    def begin_cleanup(self) -> None:
        self.calls.append("begin_cleanup")
        if self.begin_cleanup_error is not None:
            raise self.begin_cleanup_error

    def complete(self) -> None:
        self.calls.append("complete")
        if self.complete_error is not None:
            raise self.complete_error


class FakeEvidenceSink:
    """Deterministic `EvidenceSink` double: no filesystem or Git
    involved. Defaults to a trivial successful, complete, empty
    capture; a test overrides `receipt` to exercise any of the other
    three legal `EvidenceCaptured` shapes."""

    def __init__(
        self, receipt: EvidenceReceipt | None = None, *, raise_error: Exception | None = None
    ) -> None:
        self._receipt = receipt
        self._raise_error = raise_error
        self.capture_calls = 0
        self.last_call_kwargs: dict[str, object] | None = None

    def capture(
        self, *, worktree_path, source_repo_path, initial_commit, lifecycle_id
    ) -> EvidenceReceipt:
        self.capture_calls += 1
        if self._raise_error is not None:
            raise self._raise_error
        self.last_call_kwargs = {
            "worktree_path": worktree_path,
            "source_repo_path": source_repo_path,
            "initial_commit": initial_commit,
            "lifecycle_id": lifecycle_id,
        }
        if self._receipt is not None:
            return self._receipt
        return EvidenceReceipt(
            success=True,
            complete=True,
            artifact_id=lifecycle_id,
            sha256_payload="0" * 64,
            bytes_written=0,
            error=None,
        )


class CountingToken:
    """ADR 0005 Amendment 1 sweep double: counts `is_cancelled()` calls and
    reports cancellation from the `cancel_from`th call onward (never, when
    None). The controller records the check site itself."""

    def __init__(self, cancel_from: int | None = None) -> None:
        self._cancel_from = cancel_from
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self._cancel_from is not None and self.calls >= self._cancel_from


class BlockingCancellableApprovalProvider:
    """A blocking approval provider that stops waiting only when its token is
    cancelled, then raises `OperationCancelled` (ADR 0005 Amendment 1
    section 4). `on_wait` runs once when the wait begins (the test-only
    driver writes its `ready-approval` gate there). A deadline that passes
    fails loudly; it never returns a decision."""

    def __init__(self, token, *, on_wait=None, poll_seconds: float = 0.01, deadline_seconds: float = 120.0) -> None:
        self._token = token
        self._on_wait = on_wait
        self._poll = poll_seconds
        self._deadline = deadline_seconds

    def decide(self, request) -> ApprovalResponse:
        import time

        from codeagent.cancellation import OperationCancelled

        if self._on_wait is not None:
            self._on_wait()
        deadline = time.monotonic() + self._deadline
        while not self._token.is_cancelled():
            if time.monotonic() > deadline:
                raise AssertionError("approval wait was never cancelled before its deadline")
            time.sleep(self._poll)
        raise OperationCancelled()
