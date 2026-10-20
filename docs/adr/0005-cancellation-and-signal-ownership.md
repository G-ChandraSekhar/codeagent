# ADR 0005: Cooperative cancellation and signal ownership

Status: Accepted (2026-09-15)

Implementation status: **Amendment 1's mechanism is implemented and
verified locally (macOS, 2026-10-19), uncommitted; Linux CI pending** (see
Amendment 1 §12). It is exercised only through a test-only driver around the
internal composition; `main()` installs no handler before D6. The original
2026-09-15 text below is unchanged.

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

---

## Amendment 1 (Accepted 2026-10-06): entrypoint-owned cancellation mechanism, precise safe points, and process isolation (ledger D3)

**Status: Accepted 2026-10-06** after joint review. Implemented and verified
locally 2026-10-19, uncommitted; Linux CI pending (§12). Ledger
deliverable D3. This amendment refines the accepted decision above; where it
says otherwise, it supersedes the specific sentence it names. Every other part
of the decision, the consequences, and the rejected alternatives stands
unchanged.

**Why an amendment is needed.**

- **§2 cannot hold literally yet.** It says "the CLI `main()` installs SIGINT
  and SIGTERM handlers … when a run starts". No run starts from `main()`
  before ledger D6 (`codeagent solve`), and no operator path may run the
  lifecycle-aware composition before Gate O (ADR 0004 Amendment 14). That
  docstring requirement is restated in `_lifecycle_run.py`.
- **Several contracts were unstated:**
  - where the handler code lives;
  - a testable safe-point rule;
  - approval cancellation;
  - the cancellation outcome's cleanup statuses;
  - process-group behaviour on abort;
  - concurrency;
  - signal install and restore failures.

### 1. Ownership placement (refines §2)

- **The only production handler owner** is a context manager,
  `cli.owned_cancellation()`, in the entrypoint module `src/codeagent/cli.py`.
  No other production module calls `signal.signal`, enforced by an AST pin.
- **D3 does not wire `main()`.** `main()`, `_dispatch()`, `build_parser()`
  (still exactly `{reconcile}`) and `ExitCode` are unchanged. `reconcile` is
  never wrapped, and its existing `KeyboardInterrupt` propagation is kept.
- **D3 proves the mechanism only in a test-only driver process**
  (`tests/support/`). The driver wraps `_lifecycle_run.run_lifecycle_aware(cancellation=…)`
  in `owned_cancellation()` and receives real SIGINT (to its process group)
  and real SIGTERM (to its exact PID). No production module imports the
  driver or `_lifecycle_run` (T41 unchanged).
- **§2's "when a run starts" will be satisfied by D6,** when `solve`'s
  dispatch is made to run inside `owned_cancellation()`. Until then, §2's
  run-start installation is unmet.
- **Mandatory D6 acceptance:** repeat the real-signal acceptance tests through
  the real `codeagent solve`. **D6 also owns** operator exit-code mapping (for
  example 130 and 143).

### 2. Token, source, and handler (refines §§1 and 4)

**Types (`src/codeagent/cancellation.py`):**

- `CancellationToken` (read-only);
- `CancellationSource`;
- `NEVER_CANCELLED`;
- `OperationCancelled(Exception)`;
- `CancellationThreadError`.

**Main-thread only (supersedes §1's implication that a web endpoint can call
the same source).** The source is created on, and accepts requests only from,
the main thread. A request from any other thread raises
`CancellationThreadError` and leaves the state unchanged. Thread-safe or
HTTP cancellation is deferred to whichever deliverable first adds a web
entrypoint, and that deliverable must add a concurrent request path.

**The handler** performs one lock-free request:
`if not candidates: candidates.append(reason)`.

- No I/O, logging, locks, `threading.Event`, Docker or Git (§4 unchanged).
  A lock or `Event` in a handler can deadlock when the main thread already
  holds it.
- **The winner is the first committed request.** That is the first `append`
  to execute, which is not necessarily the first request entered.
- **Sequential requests never add a candidate.** Only handler re-entrancy
  inside an outer request's check-then-append window can, so the list has no
  fixed numeric bound.
- `is_cancelled()` and the winning reason never depend on any diagnostic.

**An opt-in diagnostic** (`record_requests=True`, used only by tests) counts
`request()` calls with one atomic append each. It is never consulted for
state. Standard POSIX signals can coalesce, so the count reflects delivered
requests, not signals sent.

### 3. Safe-point invariant (supersedes §5's first sentence)

After `RunStarted`, the controller checks the token:

- **at entry:** before every non-abort state transition except `RUN_STARTED`,
  every tool dispatch, every model request, and every verifier invocation;
- **after return:** immediately after every blocking collaborator returns,
  once that call's paired completion event has been emitted. This covers
  lifecycle activation, model calls, the read, approval and the verifier.

**Rules:**

- **Nothing new starts after cancellation is observed.** Once a check
  observes it, no further non-abort transition, tool dispatch, model request,
  verifier call or patch critical section begins. The run proceeds to
  `_terminate(Trigger.CANCELLED)`.
- **Event pairing is preserved.** Every started event already emitted has
  its pair before cancellation is honored.
- **The patch critical section has no check.** It runs from the `APPLY_PATCH`
  dispatch check to `PatchApplied` (or its failure `ToolCompleted`). §5's
  non-interruptible patch rule is unchanged; a pending cancellation is
  observed by the first check after the dispatch returns.
- **Named exemptions:** `RUN_STARTED`, so a cancelled run is recorded, and
  `activate()`, so teardown can reach `COMPLETE`.
- **A returned operational failure outranks a pending cancellation.** A
  failed read, a failed patch, or a verifier `TIMEOUT`, `ENVIRONMENT_FAILURE`
  or `COMMAND_START_FAILURE` keeps its existing terminal path.
- **Implementation:** the checks live inside `_transition()` and
  `_dispatch_tool()` and at named call sites, and raise a private exception
  that `run()` catches before its ordinary-exception boundary (ADR 0004
  Amendment 9). An exhaustive test cancels at each check in turn.

**Pre-run latency.** A signal during `prepare_lifecycle()` (including its
bounded reconciliation) or composition setup only sets the flag. It is first
observed at the controller's first check. The worktree may therefore be
materialized and then disposed by ordinary teardown, and no new durable shape
is created. Not claimed: that cancellation during admission prevents
materialization.

### 4. Approval cancellation (adds to §5)

A cancellable approval provider that stops waiting because of the token
raises `OperationCancelled`.

- The controller catches it **only** around `approval.decide()` and
  terminates `CANCELLED`, with **no** `ApprovalRecorded` event.
- Raised anywhere else, it is an ordinary unexpected exception:
  `UNRECOVERABLE_ERROR` / `UNCLASSIFIED_FAILURE` (Amendment 9 of ADR 0004).
- Interactive or plan-file input under cancellation is D6's.

### 5. Verifier and `VerificationOutcome.CANCELLED` (refines §§6 and 8)

**Entry.** If the token is already set, the verifier returns `CANCELLED`
with `cleanup_status = NOT_APPLICABLE` before any lifecycle publication or
Docker call.

**Attach wait.** It waits in slices of at most 0.1 s. A handler that only
sets a flag does not interrupt a blocking `wait(timeout)`; the read-only
experiment recorded in the D3 plan confirmed this on macOS.

**Observed-before-exit.**

- **If the token is observed while the attach client is still running:**
  1. kill the client's process group with `os.killpg(proc.pid, SIGKILL)`
     before reaping;
  2. call `proc.kill()` as a best-effort fallback, used when the group kill
     raises or the client cannot be confirmed exiting within the bounded
     grace period;
  3. reap the client exactly once;
  4. whatever steps 1 and 2 returned, continue cleanup by the immutable
     container id: publish `REMOVING(id)`, remove by id, confirm absence by
     a fresh listing, and publish `ABSENT`;
  5. return `CANCELLED` / `CONFIRMED_ABSENT`. If absence cannot be
     confirmed, the existing `ENVIRONMENT_FAILURE` / `UNCONFIRMED` override
     applies instead.
- **If the client exited first,** the genuine outcome stands, and the
  controller's post-return check cancels.

**Outcome shape.** `VerificationOutcome.CANCELLED` requires:

- `exit_code is None`;
- `error is None`;
- `cleanup_status ∈ {NOT_APPLICABLE, CONFIRMED_ABSENT}`.

It is valid for `BaselineRecorded`, `VerificationCompleted` and
`controller.VerificationResult` through `validate_verification_outcome_shape`.
If cleanup is unconfirmed, the existing override applies: the outcome is
`ENVIRONMENT_FAILURE` / `UNCONFIRMED`, never `CANCELLED`. A cancelled outcome
never enters the test-failure repair branch.

**Terminal mapping (§9 unchanged), in precedence order:**

1. cleanup unconfirmed or an owner-state publication failure →
   `LIFECYCLE_CLEANUP_UNCONFIRMED`;
2. an evidence failure → that evidence code;
3. an unexpected ordinary exception → `UNCLASSIFIED_FAILURE`;
4. a returned operational failure → its code;
5. otherwise `CANCELLED`, with `RunFinished.error is None`.

### 6. Process isolation (refines §7)

**Every production process launch uses its own session**
(`start_new_session=True`). There are exactly seven call nodes:

| Site | Location | API |
|---|---|---|
| L1 | `_bounded_subprocess.run_bounded_stdout` | `Popen` |
| L2 | `executor.DockerVerifier._start_and_inspect` | `Popen` |
| L3 | `_git_safety._run` | `run` |
| L4 | `_git_safety._read_bounded` | `Popen` |
| L5 | `_git_safety._read_bounded_preview` | `Popen` |
| L6 | `checkpoint_ref._run_git` | `run` |
| L7 | `checkpoint_ref._RefTransaction.__enter__` | `Popen` |

**Kill paths.**

- The owned `Popen` abort paths (L1, L2, L4, L5 and L7) kill the child's
  process group with `os.killpg` **before** reaping.
- The two `subprocess.run` timeout paths (L3 and L6) still kill only their
  direct child. This is a stated residual: hooks are disabled and filters are
  neutralized (ADR 0006), so no long-lived Git descendant is expected. That
  is an expectation, not a tested guarantee.

**A structural test pins the inventory:**

- **Exact counts:** each listed `(module, scope, API)` call node occurs
  exactly once, seven in total.
- **The flag:** each passes the literal `start_new_session=True`.
- **Forbidden spellings:** aliased imports, `from … import` of launch APIs,
  assigning a launch API to a name, and dynamic `getattr`/`__import__`
  spellings. Code review remains the only check for a launch name computed
  at runtime.

**The effect.** Successfully launched L1–L7 children run in separate
sessions. So, after session establishment, a terminal signal directed at
CodeAgent's foreground process group is not delivered to those children.
Abort paths use explicit process-group termination where specified (L1, L2,
L4, L5 and L7). No claim is made about the instant between `fork` and
session establishment. The L3/L6 direct-child timeout residual above stands.

### 7. Signal install and restore failures (refines §2's "restores the previous handlers in `finally`")

**The record.** Every failure is kept as an immutable `SignalOwnershipFailure`:

- `stage`: `INSTALL_SIGINT`, `INSTALL_SIGTERM`, `RESTORE_SIGTERM` or
  `RESTORE_SIGINT`;
- `kind`: `RAISED`, `OBSERVATION_RAISED` or `IDENTITY_MISMATCH`;
- `exception`: the exact instance for `RAISED` and `OBSERVATION_RAISED`;
  `None` for `IDENTITY_MISMATCH`, and never invented.

**The error.** `SignalOwnershipError(reason, failures)`:

- `reason` is one of `NOT_MAIN_THREAD`, `UNSUPPORTED_PLATFORM`,
  `INSTALL_FAILED` or `RESTORE_UNCONFIRMED`;
- it carries the complete ordered tuple of failures;
- its messages are fixed and name only the reason and signal names.

**Bookkeeping.**

- A signal is **owed exactly one restoration** when it was installed, or when
  its installation raised but left it changed, or left its state
  unobservable.
- Restoration runs in reverse order and is always attempted for every owed
  signal. It is confirmed only by `getsignal(sig) is before`.
- `unrestored` is an **ordered tuple** of signal names: the owed signals
  whose single restoration was not confirmed, in restoration-attempt order
  (reverse installation order, so SIGTERM before SIGINT). Each signal
  appears at most once. It is derived only from final restoration
  bookkeeping, never from an install-time mismatch that was later restored.

**The primary cause** (always `raise … from`; nothing depends on
`__context__`):

- installation stopped: the first install exception;
- exit with a body exception, when any restoration is unconfirmed: the
  raised result is `SignalOwnershipError(RESTORE_UNCONFIRMED, …)`, and the
  body exception is its explicit `__cause__` (cleanup dominance);
- otherwise: the first raised restore exception, or `None`.

**Other rules.**

- A body exception with every restoration confirmed propagates unchanged.
- A repeated `close()` after an unconfirmed state re-raises the **same**
  stored error object, with unchanged `failures` and `unrestored`, and
  attempts nothing.
- The D3 plan enumerates 28 tested rows.

### 8. Iteration accounting (disclosed correction)

`RunFinished.iterations_used` becomes the number of completed transitions
**into** EXPLORE.

- It is unchanged for every run that reached EXPLORE.
- It becomes 0, instead of 1, for runs that end before exploration:
  activation failure, a baseline operational failure, baseline
  cleanup-unconfirmed, and cancellation before EXPLORE.

**Implementation-time correction (2026-10-19, disclosed; decided by the
author before any code was written).** The first sentence above contradicts
the second for budget-exceeded runs: the controller transitions into
EXPLORE (`PLAN_REVISION_REQUESTED` or `VERIFICATION_FAILED`) and then aborts
with `BudgetExceeded` before exploring, so counting transitions would have
changed the existing pins `test_controller.py:242` (3→4), `:261` (1→2) and
`:278` (3→4), which the D3 plan said were unaffected. The implemented
definition is therefore **the number of exploration passes begun**: one
increment at the top of each pass, before C5. It equals `pass_index + 1` for
every run that reached its first pass (all existing pins hold) and is 0 for
every run that ends before it, which is what both bullets require.

### 9. Gate O after D3

Once D3 is implemented, locally verified, committed, and confirmed on Linux
CI, all Gate O prerequisites will be implemented:

- the dead-run checkpoint-ref row (ADR 0004 Amendment 16);
- the worktree-plus-container row (Amendment 19);
- abandonment (Amendment 18);
- this cancellation mechanism.

Only at that point may the ledger mark Gate O satisfied and permit D6 to wire
the first operator run. Until then, Gate O remains unsatisfied. Even then,
this amendment does not claim that operator cancellation is reachable or
proven before D6.

### 10. Non-claims

- **Operator reach:** no `solve` and no operator path. `main()` installs no
  handlers, D3 is not user-visible, and operator cancellation is unproven
  until D6.
- **Where cancellation is not observed:**
  - inside a model request (bounded by its timeout; D4 and D7);
  - while reading interactive input (D6);
  - inside a bounded Docker control-plane command (up to 30 s) or Git
    command;
  - during `prepare_lifecycle()` or composition setup.
- **Unhandled signals:** SIGHUP, SIGQUIT and SIGKILL are not handled; ADR
  0004 reconciliation covers what remains, and the reservation-before-
  `creating` residual is unchanged.
- **Persistence:** the cancellation reason is held in memory only, not
  persisted in events or the projection.
- **Embedding:** an embedding that keeps `KeyboardInterrupt` semantics still
  relies on reconciliation (Context finding 1 is unchanged).
- **Process groups:** L3 and L6 timeouts are direct-child-only.
- **Threats:** no cross-resource atomicity. The A4 races are unchanged. T-F1
  gains only cooperative-cleanup evidence; T-E1 and T-F2 are unchanged.
- **Evidence:** automated-test evidence, not a security review.

### 11. Planned verification

The accepted D3 plan, revision 5, specifies the verification (stored outside
the repository):

- the safe-point sweep;
- the event-shape parity matrix;
- per-site real-process `PATH`-shim tests for L1–L7, with group-kill and
  grandchild absence for L1, L2, L4, L5 and L7;
- the inventory pin;
- the 28-row signal-ownership matrix;
- the main-thread and re-entrancy tests for the source;
- the iteration-accounting regressions;
- real-signal, real-Docker tests E1–E12, including:
  - baseline, verification and approval-wait cancellation by group SIGINT
    and by exact-PID SIGTERM;
  - serialized repeated signals during gated cleanup;
  - a real cleanup-unconfirmed leftover reconciled by ADR 0004 Amendment 19;
  - pending cancellation during the patch;
  - signal during admission reconciliation;
  - handler restoration;
  - attach-client identity;
- after every real-signal row: exact per-admission entry outcomes, explicit
  reconciliation of each admission probe's own entry, and a final clean dry
  run;
- 46 numbered mutation categories (M1–M46): 51 concrete mutations, since
  M26 has six variants;
- forward and reverse focused runs, the full suite with
  `CODEAGENT_REQUIRE_DOCKER=1`, and cleanup checks;
- Linux CI, with the real-signal tests added to CI's dedicated Docker step so
  they are named.

The estimate is 38–60 focused hours (the ledger says 20–40). The ledger is
not modified until implementation changes D3's status.

### 12. Local implementation evidence (2026-10-19; uncommitted, Linux CI pending)

**Status.** Implemented and verified locally on macOS (Docker 29.8.0, already
running), on top of `e4426a1`. Not committed, not pushed, no Linux CI run.
**Gate O is not satisfied:** §9 requires the commit and a successful Linux CI
run first.

**Files.** New: `src/codeagent/cancellation.py`;
`tests/unit/test_cancellation.py`; `tests/unit/test_subprocess_inventory.py`;
`tests/support/process_shims.py`; `tests/support/cancellation_driver.py`
(test-only); `tests/integration/test_cancellation_e2e.py`. Changed:
`cli.py` (`owned_cancellation()` only; `main()`, `_dispatch()`,
`build_parser()` and `ExitCode` unchanged), `controller.py`, `executor.py`,
`events.py`, `_bounded_subprocess.py`, `_git_safety.py`, `checkpoint_ref.py`,
`_lifecycle_run.py`; tests `test_cli.py`, `test_controller.py`,
`test_executor.py`, `test_events.py`, `test_verification_result_parity.py`,
`test_bounded_subprocess.py`, `test_git_safety.py`, `test_checkpoint_ref.py`,
`tests/support/fakes.py`; `.github/workflows/ci.yml` (the dedicated Docker step
also runs `test_cancellation_e2e.py -v`).

**Deviations, disclosed.**

1. §8 iteration accounting: implemented as exploration passes begun (see the
   correction under §8; author decision before coding).
2. E11 (handler restoration) and E12 (attach-client identity) are assertions
   inside every applicable E-row (E11 in all; E12 in E1–E4, E7 and E8), not
   separate test functions. The ten E-test cases cover E1–E10.
3. The verifier's attach deadline remains real time (`time.monotonic`), as
   before D3; the slicing itself is unit-tested with a fake clock.
4. Per-site session tests run `docker` shims in `exit` mode (identity is
   recorded while alive, before any exec) and `git` shims in `exec` mode.
5. Disclosed test updates: the `VerificationOutcome` member-set pin; the
   executor `_FakeProc` gained `poll()` and an optional `pid` (tests reaching
   the group-kill path patch `os.killpg`, so a fake can never signal a real
   group), and the timeout test uses a 0.2 s budget.

**Verification (`CODEAGENT_REQUIRE_DOCKER=1`).**

| Run | Result |
|---|---|
Final totals, after all six joint-review corrections below (earlier passes
recorded: first local pass 1,760 / 2,643 / 4,247 and 51 of 51; after the
first correction 1,788 / 2,662 / 4,275 and 56 of 56; after the second 1,809 /
2,683 / 4,296 and 57 of 57; after the third 1,821 / 2,695 / 4,308 and 59 of
59; after the fourth 1,857 / 2,723 / 4,344 and 63 of 63; after the fifth
1,872 / 2,732 / 4,359 and 67 of 67):

| Run | Result |
|---|---|
| Directly affected 11 files | 1,906 passed, forward and reverse |
| Focused set: the 23-file set plus the three new test files | 2,771 passed, forward and reverse |
| Dedicated CI Docker command (`test_slice_c.py` + `test_cancellation_e2e.py -v`) | 14 passed, 0 skipped |
| Complete suite | 4,401 passed, 0 skipped (3,994 before D3) |
| Mutations | 70 of 70 caught: the 51 plan mutations (M1–M46; M26 as six variants) plus nineteen correction reverts (R-L1, R-L45, R-L7, R-PROBE, R-L2U, R-L2BASE, R-L7ORDER, R-L7U, R-L1BASE, R-L45BASE, R-L7BASE, R-L2DOM, R-L45WRAP, R-L1DROP, R-L7REPLACE, R-L2BODY, R-ATTACHNARROW, R-LRPRIVATE, R-LRDROP), each by a failing assertion (tests convert an escaping `KeyboardInterrupt` into a failure, so no revert can pass by aborting the session); every source restored and its SHA-256 verified |

The seven-call-node inventory matches the pre-D3 scan exactly. After every run:
no `codeagent-*` container, no extra worktree or `.git/worktrees` entry, no
`refs/codeagent` ref, no marker grandchild, driver or helper process, and no
default state root; `git diff --check` clean; `uv.lock` untouched.

**Correction after joint review (2026-10-19, before any commit).** Joint
review found a fail-closed defect in the first local implementation: the
L1, L4/L5 and L7 abort paths discarded every `os.killpg` exception, then
killed and reaped only the direct child, and could report confirmed cleanup.
A confirmed direct-child reap says nothing about descendants still in that
process group, which contradicted §6's group-cleanup claim (and, at L7, a
surviving descendant could retain Git resources). Now, in every owned abort
path:

- `os.killpg` is always attempted first. `ProcessLookupError` means the group
  is confirmed absent.
- Any other group-kill failure is retained, and every later cleanup step
  still runs; none is skipped because an earlier one failed. The pinned call
  contract per path (one group kill and, after a failed group kill and a
  confirmed reap, at most one signal-0 probe):
  - L1 and L4/L5: one direct `kill()` and one bounded reap.
  - L7: one direct `kill()` and a bounded reap; if that reap is not confirmed
    (timeout or failure), exactly one retry `kill()` and one more bounded
    reap — at most two direct kills and two reaps.
  - L2: a direct `kill()` only if the group kill raised, and a bounded reap;
    if that reap is not confirmed, exactly one retry `kill()` and one more
    bounded reap. The second reap was previously unbounded and is now bounded.

  (An earlier version of this section said "nothing is killed twice". That
  was true for L1 and L4/L5 only; L2 and L7 deliberately retry once.)
- **Post-reap probe (author decision, 2026-10-19).** Implementing the rule
  exposed a macOS behaviour, confirmed by a disposable experiment: `killpg`
  raises `PermissionError` when the group's only member is its exited,
  unreaped leader (no live process left; with a live member the call
  succeeds). Read strictly, the rule then reported every abort of an
  already-exited child as unconfirmed, which broke seven existing
  `_git_safety` tests, including the preview-overflow path that evidence
  capture uses. So after the direct child's single reap, a group-kill
  failure is followed by one `killpg(pgid, 0)` probe (signal 0, never a
  kill). Only `ProcessLookupError` from that probe confirms the group has no
  member left; success or any other error leaves it unconfirmed. PID reuse
  between the reap and the probe can only produce a false *unconfirmed*.
- That uncertainty dominates an otherwise confirmed reap, with each helper's
  sanitized category and fixed text:
  - `_bounded_subprocess` (L1): `TERMINATION_UNCONFIRMED`, internally chained
    from the exact failure. Precedence: an unconfirmed reap (chained from its
    own failure), then the group-kill failure, then a close failure
    (`CLEANUP_UNCONFIRMED`).
  - `_git_safety` (L4, L5): `PROCESS_CLEANUP_UNCONFIRMED`; every such error
    carries the group-kill failure as `__cause__`, never a `TimeoutExpired`
    (whose text names argv). L5's overflow abort now runs outside the handled
    region, so its own unconfirmed result is never followed by a second
    terminate.
  - the checkpoint transaction (L7): `TRANSACTION_CLEANUP_UNCONFIRMED`
    (outcome `UNKNOWN`); the explicit `__cause__` stays the abort failure when
    there is one (the existing contract), otherwise the group-kill failure,
    which is always retained as `group_kill_failure`.
- `executor._start_and_inspect` (L2), reviewed separately: an unconfirmed
  attach-client cleanup can no longer return a confirmed `CANCELLED` or
  `TIMEOUT`. The attempt fails closed as `ENVIRONMENT_FAILURE`
  (`EXECUTOR_ENVIRONMENT_FAILURE`, fixed text), while the container itself is
  still removed by immutable id and its absence confirmed as usual. (The
  first version of this correction discarded the group-kill exception; it is
  now retained, see the second correction below.)

Fault-injection tests cover each path (`ProcessLookupError`; `PermissionError`
and a non-`OSError` sentinel; the probe confirming absence; combined
group-kill and later cleanup failures; the exact group-kill, probe,
direct-kill and reap counts of the contract above). Temporarily restoring the swallow-and-continue
behaviour, one helper at a time, made every failure-direction test fail
(L1 4 of 5 — the fifth pins that an unconfirmed reap dominates, which holds
either way; L4/L5 6 of 6; L7 2 of 2; L2 4 of 4); each source was then restored byte for byte, verified by
SHA-256. These reverts are kept in the mutation set as R-L1, R-L45, R-L7 and
R-EXE, with R-PROBE (a probe that treats any error as absence) as a fifth.

**Second correction after joint review (2026-10-19, before any commit).**
Review found two fallback paths that could still raise raw and bypass the
promised cleanup:

- **L2.** `proc.kill()` and `proc.wait()` in the attach abort were unguarded.
  A raw exception escaped `_start_and_inspect`; callers catch only
  `_DockerLaunchError`, so the lifecycle-aware attempt never returned
  `REQUIRES_ID_CLEANUP` and removal by immutable id could be skipped. The
  abort is now `_abort_attach_client`, which never raises: the group-kill
  failure, every direct-kill and reap failure, and a failure of the sliced
  attach wait itself are retained, as exact instances with call counts, in an
  internal `_AttachAbort` record (kept as `DockerVerifier._last_attach_abort`
  for identity-based tests; never persisted). When the client and its group
  are not confirmed gone, or the wait failed, the attempt is
  `ENVIRONMENT_FAILURE` with the fixed text "the verification attach client
  could not be confirmed terminated", the known container id is still
  returned, and `REMOVING(id)` → `docker rm --force <id>` → fresh absence
  confirmation → `ABSENT` runs as usual. Confirmation is by observation: a
  reaped client with a killed (or probe-confirmed absent) group is confirmed
  even if a direct `kill()` call raised.
- **L7.** Both direct `kill()` calls, non-`TimeoutExpired` `wait()` failures
  and `poll()` failures could escape raw. All are now retained (in
  occurrence order, as `CheckpointRefError.cleanup_failures`; never a
  `TimeoutExpired`), every remaining bounded step still runs, and an
  unconfirmed termination ends as `TRANSACTION_CLEANUP_UNCONFIRMED` with the
  fixed message. Cause precedence: the abort failure when there is one;
  otherwise the earliest unconfirmed cleanup failure in occurrence order (the
  group kill first, then direct kills, reaps and observations); `None` if
  termination was simply never observed. The group-kill failure is always
  also retained as `group_kill_failure`.

New fault-injection tests cover, for L2 and L7 independently: the group kill
failing with a raising direct kill; a successful group kill, a timed-out reap
and a raising retry kill; a `wait()` raising a non-timeout sentinel; multiple
failures with the documented primary cause and retained secondaries; all later
cleanup still occurring; L2's removal and absence confirmation by immutable id
(lifecycle-aware and legacy paths); no sentinel text in any public error; and
exact kill and reap counts. Restoring the previous unguarded blocks verbatim
made every path-level L2 test fail (6 of 6) and 9 of the 14 L7 tests fail (the
five that pass are the earlier real-Git group-kill tests, which the previous
block already handled); the tests that call the new `_abort_attach_client`
directly do not apply to the old code. Each source was restored byte for byte,
verified by SHA-256; the two reverts are kept as R-L2U and R-L7U (R-EXE, whose
target no longer exists, was replaced by R-L2U).

**Third correction after joint review (2026-10-19, before any commit).** Two
claims in the second correction were not yet true; this subsection supersedes
them (the text above is left as written):

- **L2 could still be bypassed by a `BaseException`.** The attach abort and
  the sliced wait caught only `Exception`, so a `KeyboardInterrupt`,
  `SystemExit` or other non-`Exception` could escape before the container id
  was returned for cleanup. Now the sliced wait, the group kill, every direct
  kill, every reap and the signal-0 probe retain any `BaseException`
  (`ProcessLookupError` from a kill or probe still means "group absent", and a
  reap's `TimeoutExpired` is still never retained) in one chronological
  record, `_AttachAbort.failures_in_order`, as exact instances; every remaining
  step still runs. **Decision, flagged for joint review:** a retained
  non-`Exception` is not swallowed. `DockerVerifier._execute` re-raises the
  first one, as the exact instance, only after the attempt's container cleanup
  by immutable id (and, on the lifecycle-aware path, `REMOVING(id)` and the
  confirmed `ABSENT`) has run; swallowing it would silently discard a
  requested exit. The executor's former `_group_confirmed_absent` helper is
  replaced by an inline probe that retains its failure as `probe_failure`.
- **L7's cause was not chosen in occurrence order.** The second correction
  built the candidate list as "group-kill failure, then the rest", so an
  earlier `poll()` failure lost to a later group-kill failure. `__exit__` now
  keeps one chronological record (`CheckpointRefError.failures_in_order`, the
  group-kill failure appended when it occurs); the cause is the abort failure
  when there is one, otherwise the earliest entry of that record, skipping a
  group-kill failure the probe has cleared. `group_kill_failure` and
  `cleanup_failures` (the record without the group-kill failure) remain.

Tests: for L2, a custom `BaseException` and a real `KeyboardInterrupt` at each
of the sliced wait, group kill, direct kill, reap and probe — the exact
instance is raised only after removal by id and the `ABSENT` publication, it is
retained in order, and the kill, reap and `killpg` counts are pinned (10
cases). For L7, a pre-abort `poll()` failure P followed by a group-kill failure
G with termination never confirmed: the record is `(P, G)`, the cause is P (or
the abort failure when present), and every bounded step ran (one group kill,
two direct kills, two reaps, three observations). Restoring the previous code
verbatim made every new L2 case fail (R-L2BASE) and the new L7 case without an
abort failure fail (R-L7ORDER; with an abort failure the cause is that failure
under either ordering, so that variant passes correctly). Each source was
restored byte for byte, verified by SHA-256.

**Fourth correction after joint review (2026-10-19, before any commit).** The
retain-then-re-raise decision was accepted with one qualification, and the
same `BaseException` gap was fixed in L1, L4/L5 and L7. This subsection
supersedes the third correction's statement that L2 re-raises the retained
interrupt "only after the attempt's container cleanup by immutable id ... has
run": it re-raises **only after fully confirmed cleanup**.

- **L2 cleanup dominance.** `DockerVerifier._execute` re-raises the retained
  non-`Exception` (exact instance) only when the attach client is reaped, its
  process group is confirmed gone, and the container is confirmed absent,
  including the durable `ABSENT` publication on the lifecycle-aware path. If
  any of those is unconfirmed, the existing sanitized failure result is
  returned (`ENVIRONMENT_FAILURE`, with `UNCONFIRMED` cleanup where removal or
  absence is unconfirmed) and carries the interrupt by identity in a new
  `VerificationResult.deferred_interrupt` field (excluded from `repr`,
  equality and every event; it must be a non-`Exception`). The controller
  ends the run through its ordinary cleanup-unconfirmed path and exposes the
  interrupt as `RunController.deferred_interrupt`; `run_lifecycle_aware`
  returns it as `LifecycleRunResult.deferred_interrupt`. Neither fact
  replaces the other; operator exit-code mapping remains D6's.
- **L1, L4/L5 and L7, the same model.** Every failure of the group kill,
  direct kill(s), stream/descriptor close, bounded reap(s), writer join (L4)
  and signal-0 probe — `BaseException`s included — is retained in one
  chronological record as its exact instance and never stops a later step;
  `ProcessLookupError` still means "group absent"; a reap's `TimeoutExpired`
  is never retained. Each helper's pinned call contract (above) is unchanged.
  Cleanup fully confirmed → the earliest retained non-`Exception` is
  re-raised, as the exact instance, only after cleanup. Cleanup unconfirmed →
  the helper's sanitized category (`TERMINATION_UNCONFIRMED` /
  `CLEANUP_UNCONFIRMED`, `PROCESS_CLEANUP_UNCONFIRMED`,
  `TRANSACTION_CLEANUP_UNCONFIRMED`) with fixed text, carrying
  `failures_in_order` and `deferred_interrupt` by identity. Causes: L1 keeps
  its reap → group → close precedence; L4/L5 use the earliest retained
  failure (the group-kill failure whenever one occurred); L7 keeps the abort
  failure first, then the earliest chronological entry. L7's abort failure
  now takes part in the occurrence order for the interrupt, so a
  non-`Exception` from `abort()` is no longer swallowed after a confirmed
  cleanup. The probe's own failure is now retained (one L7 test's expectation
  was updated accordingly, a disclosed change). The modules' separate
  `_group_confirmed_absent` helpers were replaced by inline, retaining probes.
- **L1's outer path, verified rather than assumed.** A body interrupt (in the
  drain) already completed the abort before re-raising, and a test now pins
  that. But a `BaseException` raised inside L1's own abort steps escaped and
  skipped the remaining steps, so L1 did not qualify to stay unchanged; it
  now follows the model above.
- **Residuals, unchanged and named.** A body interrupt in L4/L5 (outside the
  abort steps) is still converted to `PROCESS_SETUP_FAILED`, as before D3.
  When cleanup is confirmed after a body interrupt, a second, later interrupt
  from the cleanup steps is not re-raised (the earlier one is). L3/L6 remain
  the direct-child timeout residual.

Tests (exact identities, chronological order, call counts, continuation of
later steps, fixed public text with no sentinel, path, argv or Git output):
L2 — dominance for unconfirmed removal, unobservable absence, a failed
`ABSENT` publication and an unconfirmed attach group (lifecycle-aware), and
unconfirmed removal, unobservable absence and an unconfirmed group (legacy);
re-raise after fully confirmed cleanup on both paths; the controller and
composition expose the interrupt. L7 — re-raise after confirmed cleanup for an
interrupt at the early `poll`, group kill, first and second direct kill, and
first reap; dominance for an interrupt at the second reap, the probe, and the
first kill with both reaps unconfirmed; the abort failure's precedence. L4/L5 —
re-raise for the group kill, direct kill and close; dominance for the reap,
the probe and a group kill followed by an unconfirmed reap; path-level through
real Git. L1 — the same set, path-level with a real child, and the body path.
Restoring the previous code verbatim (R-L1BASE, R-L45BASE, R-L7BASE, and
R-L2DOM for the unconditional re-raise) made the new tests fail; each source
was restored byte for byte and verified by SHA-256.

**Fifth correction after joint review (2026-10-19, before any commit).** The
fourth correction's cleanup dominance was accepted; its two named residuals
were not. One precedence model now applies to L1, L4/L5 and L7, and — called
out as an extension beyond the helpers named in review — to L2. This
supersedes the fourth correction's statements that, for L1, L4/L5 and L7,
"cleanup fully confirmed → the earliest retained non-`Exception` is
re-raised" (now only when there is no body failure), and its residuals 1 and
2 (both resolved below).

1. **Cleanup unconfirmed dominates.** The fixed-text categorical cleanup error
   is raised. The body failure is kept by the per-module convention already
   accepted — L1: the explicit `__cause__`; L4/L5: the earliest retained
   cleanup failure stays the cause; L7: the abort failure, else the earliest
   chronological entry, stays the cause — and in every module also as
   `body_failure`, by identity. `failures_in_order` and `deferred_interrupt`
   remain.
2. **Cleanup confirmed with a body failure.** The body failure stays primary:
   an ordinary unexpected body failure maps to the existing categorical error
   (L1 `MONITORING_FAILED`, L4/L5 `PROCESS_SETUP_FAILED`); a non-`Exception`
   body failure is re-raised as the exact instance — **L4/L5 no longer wrap a
   body `KeyboardInterrupt`/`SystemExit` as `PROCESS_SETUP_FAILED`**; L7 uses
   `__exit__`'s exception arguments and lets the `with` body's exception keep
   propagating. A later cleanup interrupt never replaces it.
3. **Cleanup confirmed, no body failure** (L4/L5's preview overflow stop;
   L7's `__exit__` with no body exception; L2 when the interrupt is the
   earliest failure): the earliest retained non-`Exception` is re-raised as
   the exact instance.

In cases 2 and 3 every other retained cleanup failure is attached to the
surfaced exception, by identity and in occurrence order, through one stable
attribute, `codeagent_cleanup_failures` (appended to, never replaced; a
repeat of the same object from a distinct call is kept; only the primary's own
position is left out). It never touches `__cause__`/`__context__` — every
re-raise of a pre-existing instance happens outside any `except` block, and
tests pin that a preset cause and context survive — and it never reads a
message. L2 applies case 2 to an ordinary attach-wait failure: the
`ENVIRONMENT_FAILURE` result stays primary and a later cleanup interrupt is
carried as `VerificationResult.deferred_interrupt`; L2's complete abort record
stays in the verifier's internal `_last_attach_abort`, which is not surfaced on
a returned result. Narrowed claims: L7's ordinary `Exception`s from closing its
selector and pipes are ignored, as before D3, not retained; an exception type
that refuses attributes cannot carry the attachment. L3/L6's direct-child
timeout remains the only accepted process-tree limitation.

Tests, per helper (L1, L4/L5, L7, and L2): an ordinary body failure followed by
a cleanup `KeyboardInterrupt`; a body interrupt followed by a second cleanup
interrupt; no body failure with two cleanup interrupts (L4/L5 preview, L7);
confirmed and unconfirmed cleanup; exact primary identity; ordered secondary
identities; preset `__cause__`/`__context__` intact; every later bounded step
still run; fixed public text with no sentinel, path, argv or Git output.
Restoring the previous behaviour (R-L45WRAP, R-L1DROP, R-L7REPLACE, R-L2BODY)
made the new tests fail; earlier reverts were re-anchored to the current code
so each still fails for its original reason.

**Sixth correction after joint review (2026-10-19, before any commit).** The
L2 extension was accepted. Three propagation and privacy defects were fixed;
this subsection supersedes the fifth correction's attachment implementation
(which tolerated only `AttributeError`/`TypeError`) and the fourth
correction's `LifecycleRunResult.deferred_interrupt` as a plain field.

- **Attachment can never replace the primary.** One shared helper,
  `cancellation.attach_cleanup_failures` (with `set_evidence_attribute`),
  replaces the per-module copies and L2's inline code. It guards both the read
  and the write of the attribute against every `BaseException` — an
  `AttributeError`, an ordinary custom exception, or a non-`Exception` raised
  by a custom `__getattribute__`/`__setattr__` — and never touches
  `__cause__`/`__context__`. An attribute-refusing exception therefore cannot
  expose the secondary record, and that is the only consequence. L2 now finds
  the primary in its record by identity rather than `==`.
- **`LifecycleRunResult.deferred_interrupt` is private**: `repr=False`,
  `compare=False`, and validated as a non-`Exception`; the exact instance
  stays readable through the field.
- **The deferred interrupt survives every composition outcome.** Amendment
  14's R4/R5 precedence is unchanged; the controller's deferred interrupt is
  carried alongside whatever surfaces: `LifecycleRunResult.deferred_interrupt`
  (returned path, including declared-only release failures);
  `LifecycleRunCleanupError.deferred_interrupt` (an unexpected reservation or
  lease failure, alone or together, on the returned path, and every raise-path
  cleanup failure); and, when an exception surfaces raw — a lone release-time
  interrupt (still primary, as Amendment 14 requires) or the raise path's
  original exception with every stage confirmed — the best-effort
  `codeagent_deferred_interrupt` attribute. The raise path now reads the
  controller's deferred interrupt too, since `controller.run()` can record one
  before another exception escapes.

Tests: the helper under all three refusal kinds, on read, write and both,
with the chain intact; path-level refusing primaries in L1, L4/L5, L7 and L2
(each still surfaces unchanged); `LifecycleRunResult` `repr` (path-like
sentinel absent), equality unaffected, the instance accessible, and
validation; and the composition outcomes — declared-only release failure,
unexpected ordinary release failure, a release-time interrupt, simultaneous
reservation and lease failures, and the raise path with and without a release
failure — each pinning identity, precedence, sanitized messages and exactly
one lease release. Reverts R-ATTACHNARROW, R-LRPRIVATE and R-LRDROP make the
new tests fail.

**One incident, disclosed.** An early draft of the E2E fixture did not set
`CODEAGENT_STATE_DIR` in the test process, so its first admission probe wrote
one `PREPARING`, all-absent entry into the default macOS state root. The
fixture's own default-root check failed the test. The directory (created by
that probe, containing only that entry against a deleted temporary repository)
was inspected and removed, and the fixture now pins the isolated root for the
test process too.

**Remaining risks.** The non-claims in §10 stand. Additionally: macOS evidence
only until Linux CI; on macOS `ps` exposes no session id, so E12's session check
there rests on the inventory pin, the shim tests and `pgid == pid`; gated tests
fail on a deadline (up to 300 s) rather than hang; automated-test evidence, not
a security review.
