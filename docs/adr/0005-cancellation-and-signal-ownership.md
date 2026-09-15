# ADR 0005: Cooperative cancellation and signal ownership

Status: Accepted (2026-09-15)

Implementation status: **not implemented.** No mechanism in this ADR
exists in production code yet.

## Context

`domain.py` already defines `Trigger.CANCELLED` (an abort trigger) and
`TerminalReason.CANCELLED`, but nothing fires them: the controller has
no cancellation input, and no CodeAgent code installs signal handlers.
The handoff requires the CLI and the future web UI to call the same
controller, with cancellation available over HTTP — so cancellation
cannot be a terminal-only or signal-only mechanism.

Findings from reading current production code (not executed):

- `DockerVerifier._execute` calls `_attempt` outside any `try/finally`,
  so a `BaseException` such as `KeyboardInterrupt` during
  create/start/inspect skips container removal.
- With no SIGTERM handler, Python's default disposition terminates the
  process without running `finally` blocks or context-manager exits.
- `docker start --attach` runs in CodeAgent's process group, so a
  terminal Ctrl-C also reaches it, and attach mode forwards signals
  into the container. A Ctrl-C could therefore be recorded as a test
  failure. This is an inference from Docker CLI behavior and must be
  confirmed by a test.
- `VerificationOutcome` has no cancellation member (the `CANCELLED` in
  `events.py` belongs to `ModelResponseStatus`). `RunFinished` requires
  `error is None` for `TerminalReason.CANCELLED`.

Stage-2 spike S5 (`spikes/s5/S5_RESULT.md`) showed, on macOS/arm64 and
Linux/x86_64, that cooperative cancellation, SIGINT, and SIGTERM
delivered to the exact child PID let the child clean up fully when its
handlers only set a flag and cleanup ran in normal control flow. S5
did **not** exercise signals delivered to a process group, attach-mode
signal forwarding, a signal arriving while a subprocess is running, or
SIGHUP. S5's control-file cancellation protocol is spike-only and is
not adopted. No spike code is reused in production.

Owned-resource cleanup, confirmation, and reconciliation are decided in
ADR 0004.

## Decision

1. **Token and source.** Cancellation is represented by a read-only
   `CancellationToken` passed to the controller and to cancellable
   collaborators. The process entrypoint owns the corresponding
   `CancellationSource`. The first cancellation request's reason is
   recorded; later requests are idempotent.
2. **Only entrypoints own signal handlers.** The CLI `main()` installs
   SIGINT and SIGTERM handlers on the main thread when a run starts and
   restores the previous handlers in `finally`. The future web server's
   lifespan hook requests cancellation for active runs, and its HTTP
   cancel endpoint calls the same source. Library modules (controller,
   executor, workspace, patch) never call `signal.signal`.
3. **Signal semantics.**
   - The first SIGINT or SIGTERM requests cancellation.
   - Repeated signals are idempotent: they never escalate, never exit
     early, and never skip bounded cleanup.
   - SIGHUP is not handled in v1; its default disposition terminates
     the process, and ADR 0004 reconciliation covers what remains.
   - SIGKILL remains the operator's hard stop, covered by ADR 0004.
4. **Handlers only set a flag.** No I/O, logging, locks, Docker, or Git
   inside a handler.
5. **Safe points.** The controller checks the token before each state
   transition, tool dispatch, model request, and verification, and
   fires `Trigger.CANCELLED`. Patch application is a non-interruptible
   critical section (ADR 0003: an interrupted apply contaminates the
   worktree); cancellation is observed after it completes. Model
   requests remain bounded by their own timeouts.
6. **Interruptible verification.** The verifier waits in short slices.
   On cancellation it stops the attach client, then removes and
   confirms the container using ADR 0004's confirmation rules. If
   removal is confirmed it returns `VerificationOutcome.CANCELLED`;
   otherwise the existing cleanup-unconfirmed override applies
   (`ENVIRONMENT_FAILURE`).
7. **Subprocess session isolation.** Docker and Git lifecycle
   subprocesses start in their own session (`start_new_session=True`),
   so terminal-generated signals reach only CodeAgent's handler and
   never the untrusted test process.
8. **Event contract.** Add `VerificationOutcome.CANCELLED`, valid for
   `BaselineRecorded`, `VerificationCompleted`, and
   `controller.VerificationResult`, with `exit_code is None` and
   `error is None`, enforced through
   `events.validate_verification_outcome_shape`.
9. **Terminal mapping.** After cancellation is observed, the controller
   performs ADR 0004 terminal teardown.
   - Cleanup confirmed: `Trigger.CANCELLED` →
     `TerminalReason.CANCELLED`, `RunFinished.error is None` (existing
     rule unchanged).
   - Cleanup unconfirmed: overridden to `Trigger.UNRECOVERABLE_ERROR`
     with ADR 0004's cleanup-unconfirmed error code.
10. **Bounded cleanup.** Cleanup is bounded by per-operation timeouts.
    If an external supervisor's grace period elapses and it sends
    SIGKILL, ADR 0004 reconciliation handles the remainder.

## Consequences

- Operator cancellation from the CLI and the web UI follows one path
  and produces one typed outcome.
- `events.py` and its shape tests gain a new outcome member; report and
  benchmark tooling must treat `CANCELLED` as neither a test failure
  nor an environment failure.
- A cancelled run whose cleanup cannot be confirmed is reported as
  `UNRECOVERABLE_ERROR`, not `CANCELLED`; the operator's cancellation
  request is not visible as the terminal reason in that case.
- Cancellation can be delayed by an in-flight model request or patch
  application.
- Closing the terminal (SIGHUP) in v1 terminates without cooperative
  cleanup; leftovers go through ADR 0004 reconciliation.
- Code embedding the controller without an entrypoint (tests,
  notebooks) gets no signal handling; it passes its own token.

Required tests (none exist yet): the token is honored at every safe
point; cancellation during patch application waits for completion;
`VerificationOutcome.CANCELLED` shape rules and parity across the three
consumers; a real CLI subprocess receiving SIGINT **to its process
group** during real verification ends `CANCELLED` with the container
confirmed removed and never `TEST_FAILURE`; SIGTERM to the exact PID;
repeated signals during cleanup do not abort it; handlers are restored
after `main()` returns; cleanup-unconfirmed after cancellation yields
`UNRECOVERABLE_ERROR`; web-style cancellation through the same source.

## Rejected alternatives

- **Relying on `KeyboardInterrupt` and context managers** — SIGTERM is
  uncovered, and the exception can surface inside critical sections.
- **Cleanup inside signal handlers** — re-entrancy in arbitrary
  interpreter state.
- **Library-installed handlers** — breaks embedding in the web server
  and in tests.
- **S5's control-file cancellation protocol** — spike-only; the HTTP
  path would not use it.
- **Recording cancellation as `ENVIRONMENT_FAILURE`** — an operator
  cancellation is not an environment failure in the implementation
  guide's failure taxonomy and would distort failure analysis.
- **Raising without a `VerificationCompleted` event** — loses the
  record that a container ran.
- **Exiting immediately on a second signal** — deliberately leaves
  orphans; bounded cleanup is preferred.
- **Handling SIGHUP in v1** — not evidenced; deferred.

## Evidence

`spikes/s5/S5_RESULT.md` scenarios 2–4 (cooperative cancellation,
SIGINT, SIGTERM) in
`spikes/s5/evidence/macos-docker-desktop-arm64/run-ffd711753021/` and
`spikes/s5/evidence/linux-x86_64/run-34783737248-attempt-1/`.
Production findings from reading `src/codeagent/executor.py`,
`src/codeagent/events.py`, and `src/codeagent/domain.py` at commit
`ad362cf`.
