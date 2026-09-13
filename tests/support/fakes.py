"""Deterministic test doubles implementing controller.py's collaborator
Protocols (ModelClient, ApprovalProvider, Verifier, PatchApplier, Clock).

Not production code. Lives under tests/ specifically so it can never be
imported from src/codeagent by accident — see the correction-pass
finding that flagged the previous version of controller.py for
depending on concrete fake types directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codeagent import domain, events
from codeagent.controller import PatchResult, PlanProposal, ReadResult, VerificationResult
from codeagent.errors import ErrorCode, OperationalError

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


class FakeModel:
    """Two-step per ModelClient's narrow Protocol: requests one read
    path, then always proposes the same canned plan once given the
    read evidence. Records `last_read_result` so a test can assert
    the controller actually passed real read evidence through — see
    test_controller.py's evidence-propagation test — without this fake
    conditioning its *behavior* on that evidence (see
    MarkerGatedFakeModel below for a fake that does).

    `read_path` defaults to the plan's own first proposed file path —
    the fixture's usual case where the model reads exactly the file it
    later proposes to patch.
    """

    def __init__(self, plan: PlanProposal, read_path: str | None = None) -> None:
        self._plan = plan
        self._read_path = read_path if read_path is not None else plan.proposed_file_paths[0]
        self.last_read_result: ReadResult | None = None

    def request_read_path(self) -> str:
        return self._read_path

    def propose_plan(self, read_result: ReadResult) -> PlanProposal:
        self.last_read_result = read_result
        return self._plan


class MarkerGatedFakeModel:
    """Test-only model whose plan is genuinely conditioned on the read
    evidence it receives, rather than merely recording it: propose_plan
    raises unless the given ReadResult succeeded and its content
    contains `marker`. Used specifically to prove the controller passes
    real content through before asking for a plan, not to model any
    future live-provider decision logic — see ModelClient's docstring
    on why that broader shape is explicitly out of scope here.
    """

    def __init__(self, read_path: str, marker: str, plan: PlanProposal) -> None:
        self._read_path = read_path
        self._marker = marker
        self._plan = plan
        self.last_read_result: ReadResult | None = None

    def request_read_path(self) -> str:
        return self._read_path

    def propose_plan(self, read_result: ReadResult) -> PlanProposal:
        self.last_read_result = read_result
        if not read_result.success or self._marker not in (read_result.content or ""):
            raise AssertionError(
                "MarkerGatedFakeModel.propose_plan was called without the expected "
                f"read evidence (marker {self._marker!r} not found) — the controller "
                "must dispatch a successful READ_FILE and pass its real content "
                "before ever calling propose_plan"
            )
        return self._plan


class FakeApprovalProvider:
    def __init__(self, decisions: tuple[domain.ApprovalDecision, ...]) -> None:
        if not decisions:
            raise ValueError("decisions must be a nonempty tuple")
        self._decisions = decisions

    def decide(self, visit_index: int) -> domain.ApprovalDecision:
        i = min(visit_index, len(self._decisions) - 1)
        return self._decisions[i]


class FakeVerifier:
    def __init__(
        self,
        outcomes: tuple[events.VerificationOutcome, ...],
        baseline_outcome: events.VerificationOutcome = events.VerificationOutcome.TEST_FAILURE,
        command: tuple[str, ...] = FIXTURE_VERIFY_COMMAND,
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

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def _result(self, outcome: events.VerificationOutcome) -> VerificationResult:
        return VerificationResult(
            outcome=outcome,
            exit_code=_FAKE_EXIT_CODE_BY_OUTCOME[outcome],
            duration_seconds=0.01,
            stdout="",
            stderr="",
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
    ) -> None:
        self._should_fail = should_fail
        self._changed_paths = changed_paths
        self._n = 0

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str]
    ) -> PatchResult:
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
                    code=ErrorCode.PATCH_VALIDATION_FAILED,
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


class FakeRepositoryReader:
    """No real filesystem involved — see codeagent.reader.WorktreeFileReader
    for the real implementation this fakes. Returns the same configured
    content for any relative_path unless `should_fail`; doesn't validate
    the path itself (path validation is the real reader's job, exercised
    against real content in test_reader.py and the real Docker E2E)."""

    def __init__(self, content: str = "fixture content", should_fail: bool = False) -> None:
        self._content = content
        self._should_fail = should_fail
        self._n = 0

    def read_file(self, relative_path: str) -> ReadResult:
        self._n += 1
        if self._should_fail:
            return ReadResult(
                success=False,
                content=None,
                byte_count=0,
                error=OperationalError(
                    code=ErrorCode.TOOL_INPUT_INVALID,
                    error_id=f"fake-read-err-{self._n}",
                    message="fixture-forced read failure",
                ),
            )
        return ReadResult(
            success=True,
            content=self._content,
            byte_count=len(self._content.encode("utf-8")),
        )


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
