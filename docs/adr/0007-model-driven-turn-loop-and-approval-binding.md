# ADR 0007: Model-driven turn loop, response precedence and approval binding

Status: Accepted (2026-10-20), at joint review. Records the invariants
accepted in the D4 plan (revisions 5 and 6) and the D4b readiness addendum
(J1–J4), as implemented by ledger D4b.

Implementation status: **implemented, locally verified on macOS
(2026-10-20), accepted at joint review, committed as `981f93b` (an ordinary
fast-forward over `0b4b320`), and confirmed on GitHub-hosted Linux CI (run
[39519675089](https://github.com/G-ChandraSekhar/codeagent/actions/runs/39519675089)).**
The authoritative CI evidence is in "Linux CI evidence" at the end of this
ADR. Not operator-reachable: no CLI path runs a task, the only model is a
test-only scripted fake, and `_lifecycle_run` stays unimported by any bundled
module (T41). See `ENGINEERING_LOG.md`'s 2026-10-20 D4b entries for the local
verification. (Historical: before the commit, this line read "implemented,
locally verified on macOS (2026-10-20) and accepted at joint review; Linux CI
pending", which was true then.)

Amendment 1 (ledger D5, slice D5-A: continuation after a pre-write patch
validation failure): **Accepted (2026-11-07)**, at joint review;
**implemented, locally verified on macOS (2026-11-07), committed as `79895f8`
(an ordinary fast-forward over `086dc62`), and confirmed on GitHub-hosted
Linux CI (run
[42454095719](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42454095719)).**
The authoritative CI evidence is in "Amendment 1: Linux CI evidence" at the end
of this ADR. See "Amendment 1" for the decisions, which refine those above for
that case. (Historical: before the commit, this marker read "implemented and
locally verified on macOS (2026-11-07), but currently unstaged, uncommitted and
not Linux-CI-confirmed", which was true then.)

Amendment 2 (ledger D5, slice D5-B: recreation feedback): **Accepted
(2026-11-08)**, at joint review; **implemented, locally verified on macOS,
committed as `ba653b7`, and confirmed on GitHub-hosted Linux CI (run
[42592929825](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42592929825)).** The authoritative CI evidence is in ADR 0004 §21.17. See
"Amendment 2" at the end of this ADR. (Historical: before the commit this line
said "implemented and locally verified on macOS; unstaged, uncommitted and not
Linux-CI-confirmed"; at acceptance it said "not implemented".)

## Context

Before D4b, `RunController` asked a two-method fake model for one read path,
read the file itself, and then asked for a plan. The patch content was
supplied by the caller (`run_lifecycle_aware(patch_operations=...)` and
`GitPatchApplier(worktree, operations)`), not by the approved proposal. D4a
landed a descriptor-relative, read-only toolkit (`RepoTools`) behind a
transitional adapter. D4b replaces both seams with a model-driven turn loop.

The decisions below are cross-cutting (events, domain, controller, tests) and
costly to reverse, so they are recorded here rather than only in the plan.

## Decision

### 1. The model seam (J1)

- `ModelClient.respond(ModelContext) -> ModelTurn` is the only model call.
  `ModelTurn` and `ToolCall` carry untrusted **bytes** (`model_name`,
  `response_id`, `provider_call_id`, `name`, `arguments_json`);
  `model_protocol.classify_turn` validates them without raising.
- Every `ModelRequestStarted(request_id)` is followed immediately by exactly
  one of `ModelResponseReceived`, the new `ModelResponseRejected`, or
  `ModelRequestFailed`.
  - `respond()` raised an `Exception`: `ModelRequestFailed`, then Slice 3C-3's
    fallback (`UNCLASSIFIED_FAILURE`). The exception's text never appears.
  - `respond()` raised an interrupt: `ModelRequestFailed`, then the exact
    interrupt. An emission failure is attached, never substituted.
  - A returned non-`ModelTurn`, or a turn whose status is not a
    `ModelResponseStatus`: `ModelResponseRejected(request_id,
    rejection_reason, error=MODEL_RESPONSE_MALFORMED)`. It carries only the
    enum reason (`NOT_A_MODEL_TURN`, `STATUS_UNRECOGNIZED`): no `repr`, type
    name, raw status, metadata or call data. The run ends
    `UNRECOVERABLE_ERROR`, uncharged.
- **Pairing rule.** Test traces are checked strictly
  (`tests/support/trace_assertions.assert_model_pairing`). A run ending
  `UNCLASSIFIED_FAILURE` never excuses a missing pair; only a request ID named
  in `allow_unpaired` may lack one, which only the one-shot emission-fault
  test uses.
- Controller-minted tool-call IDs are `tc-<pass>-<seq>`. `provider_call_id`
  is audit-only (`ToolRequested.provider_call_id`, validated
  `[A-Za-z0-9_-]{1,128}`) and is never citable as evidence.

### 2. Precedence after a model call returns (D4 plan revision 6 §7.1b)

| Outcome | Result | With a pending cancellation |
|---|---|---|
| `ModelResponseRejected` | `UNRECOVERABLE_ERROR` / `MODEL_RESPONSE_MALFORMED` | the same (ADR 0005 row 4 outranks row 5) |
| `FAILED`, valid metadata | `UNRECOVERABLE_ERROR` / `MODEL_RESPONSE_FAILED_STATUS` | the same |
| `IN_PROGRESS`, `QUEUED` | `UNRECOVERABLE_ERROR` / `MODEL_RESPONSE_MALFORMED` | the same |
| Run-ending status with a metadata defect; `INCOMPLETE` with a missing or invalid reason | `UNRECOVERABLE_ERROR` / `MODEL_RESPONSE_MALFORMED` | the same |
| `INCOMPLETE`, valid | `MODEL_RESPONSE_INCOMPLETE`, `error=None` | `CANCELLED` |
| Provider `CANCELLED` | `CANCELLED` | `CANCELLED` |
| `COMPLETED`, valid | atomic charge, then dispatch | `CANCELLED`, uncharged |
| `COMPLETED`, recoverable malformed | charge, `MALFORMED_RESPONSE` feedback, continue | `CANCELLED`, uncharged, no feedback |

The operational-failure rows terminate without consulting the token; the
non-error rows run the ordinary safe point.

### 3. `MODEL_RESPONSE_INCOMPLETE`

A new `Trigger` and `TerminalReason`, legal only as `(EXPLORE,
MODEL_RESPONSE_INCOMPLETE) -> DONE`. It is not an abort trigger, and
`RunFinished` with this reason carries no error.

### 4. Safe points (J2)

C6–C11 are retired. The consulted set is exactly C1–C5 and C12–C27:

- **C5** is the first model request of each pass (passes begun are counted
  before it); **C24** is every later request in the pass.
- **C25** is the decision point after a *returned* model outcome; **C27**
  after a *returned* tool outcome (success, policy denial, a failure that
  becomes feedback, a returned `should_stop` interruption, or the returned
  cleanup-unconfirmed route).
- **C26** precedes every model-requested tool triple, including denials.
- A model call or tool body that **raised** creates no C25/C27 decision; it
  keeps its pairing event and follows row 3, or the interrupt propagates.
- A repository tool that raises an ordinary exception, or returns something
  other than a `ToolOutcome`, is treated identically: exactly one failed
  `ToolCompleted` (`TOOL_EXECUTION_FAILED`, "tool failed unexpectedly", never
  the returned object's type or contents), its measured duration added once
  to the run total, no C27 decision, then row 3 (`UNCLASSIFIED_FAILURE`).
- Decisions are recorded, for tests only, in `_post_return_decisions` as
  `(site, sequence, kind)` with `kind` either `checked` or
  `operational_failure_precedence`.

### 5. Budgets and the soft wall-clock deadline (J3)

- `RunConfig.max_tool_calls_per_pass` (default 25) is charged one whole turn
  at a time, before any `ToolRequested`. A recoverable malformed turn is
  charged `min(max(declared, 1), 9)`. The counter resets each pass.
- `RunConfig.wall_clock_budget_seconds` (default 900) is a **soft** deadline,
  checked after cancellation at every safe point and through `should_stop()`.
  Approval waiting counts toward elapsed time; C15 runs after
  `ApprovalRecorded`. A long blocking call (for example an interactive
  approval wait) can overrun it arbitrarily; no bounded overrun is claimed.
- Precedence at an ordinary safe point: cancellation, then the wall clock,
  then the tool-call budget.

### 6. Approval binding and the patch seam

- A plan is exactly the strictly parsed `propose_plan` arguments
  (`PlanProposal = ProposePlanArgs`). `tool_schema.compute_digests` is the
  only digest implementation (`codeagent.plan.v1`, `codeagent.change.v1`).
- `PlanProposed`, `ApprovalRequest`/`ApprovalRecorded` and `PatchApplied`
  carry the plan revision and digests. `material_change` is measured against
  the last **approved** change.
- `ApprovalProvider.decide(ApprovalRequest) -> ApprovalResponse`. A revision
  request requires a meaningful note (Unicode L/M/N/P/S); notes are sanitized
  before they are recorded.
- `apply_patch` is dispatched only by the controller, never by the model. Both
  digests are recomputed from the stored `ApprovedPlan` before dispatch (a
  mismatch fails closed as `UNCLASSIFIED_FAILURE`), and the approved,
  digest-bound change is the **sole** source of the patch operation.
  `GitPatchApplier(worktree_path)` takes the trusted worktree path only;
  `run_lifecycle_aware` has no `patch_operations` parameter.

### 7. Evidence and feedback

- A plan may cite only successful repository-tool results from the current
  pass that are still retained in the model context; the citable set is frozen
  when the request is built (same-turn results are not citable).
- Feedback lives only in the in-memory `ModelContext` (224 KiB cap; tool
  results, denials and malformed-turn items are droppable oldest-first;
  verification failures and revision requests are never dropped; a static fit
  proof in `RunConfig` guarantees they fit). Events keep their existing
  redaction: no file content, snippet, verification output, `expected_text`
  or `new_text` is recorded.

### 8. Query control characters (J4)

`search_text` rejects any control character other than tab in `query` with
`ArgumentRejection.QUERY_CONTROL_CHARACTER` and the fixed message "query must
not contain control characters other than tab". Bidi format characters (for
example U+202E, U+2066) are accepted: a documented display-spoofing residual
for D6.

## Consequences

- One stale `expected_text` ends the run (`PATCH_VALIDATION_FAILED`) until D5
  adds patch-failure feedback, four operations and worktree replacement.
  (Historical: superseded by Amendment 1 (D5-A). A pre-write
  `PATCH_VALIDATION_FAILED` now returns the run to EXPLORE with
  `PATCH_FAILED` feedback and ends only through the repair budget.
  Post-write failures still end the run until D5-B.)
- `_read_adapter.py`, `RepoTools.read_whole_file`, `ReadResult`,
  `RepositoryReader` and `tests/unit/test_reader.py` are deleted.
- Feedback can contain repository and test content. It reaches only the
  in-memory context of the scripted fake; D7 must define redaction and
  disclosure controls before any live model.

## Non-claims

No operator path, no live model, no D5 transactional patching, and neither
Gate α nor v1 completion.

## Correction pass (2026-10-20, joint review)

Joint review of the first implementation found two defects in the
repository-tool wrapper, both reproduced before any edit:

1. The `ToolOutcome` return-type check ran after the guarded call, so a wrong
   return type ended `UNCLASSIFIED_FAILURE` with `ToolRequested` and
   `PolicyDecisionRecorded` but no `ToolCompleted`.
2. A tool that raised an ordinary exception recorded its duration on
   `ToolCompleted` but never added it to `RunFinished.total_duration_seconds`.

Both are fixed inside the same wrapper (§4's new bullet), with regression
tests and two mutations that restore each defect (both caught).

The re-review found the same partial-pair class on the controller-dispatched
`apply_patch` path (present before D4b): an unexpected exception from the
patch applier, an invalid applier result, or an exception from checkpoint
establishment or advance outside the three mapped checkpoint types left the
triple without `ToolCompleted`. All four were reproduced, then fixed in this
pass:

- Steps 3–7 (entry gate, establishment, the patch, the approved-path
  postcondition, advance) now run in one helper that emits nothing and returns
  a mapped failure or the validated `PatchResult`. Every existing mapped
  failure keeps its exact category and error object.
- The helper call is guarded. An ordinary exception emits exactly one failed
  `ToolCompleted` (`TOOL_EXECUTION_FAILED`, "tool failed unexpectedly", with no
  exception text, type, path, patch content, Git output or checkpoint detail),
  adds its elapsed duration once, and re-raises into Slice 3C-3
  (`UNCLASSIFIED_FAILURE`). An interrupt emits "tool interrupted" and is
  re-raised as the exact instance, with an emission failure attached.
- Emission stays outside the guard, so a failing emission never causes a
  second `ToolCompleted`. No `CheckpointCreated`, `PatchApplied` or verification
  follows, and no C27 is recorded on this controller-dispatched path.

Evidence remained local macOS only at the time of this correction pass
(historical: "Linux CI pending" was true then); the Linux CI evidence follows.

## Linux CI evidence (2026-10-21)

Commit `981f93b774a103dd83b057ea57201c69f669ba97` ("feat: add model-driven
repair loop"), pushed to `main` as an ordinary fast-forward over `0b4b320`.
Run [39519675089](https://github.com/G-ChandraSekhar/codeagent/actions/runs/39519675089)
was verified through the Actions API and an independently fetched raw job log.

- **Run:** push-triggered; head SHA exactly `981f93b…ba97`; `completed` /
  `success`.
- **Job:** one job, "Test (ubuntu-24.04, Python 3.12)". All 13 API-reported
  steps succeeded. There are zero `##[error]` lines in the raw log and zero API
  annotations.
- **Environment:** runner image `ubuntu-24.04` (Ubuntu 24.04.5 LTS), CPython
  3.12.15, and Docker client and server 28.0.4.
- **Docker:** the mandatory preflight succeeded. The pinned
  `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
  reported `linux/amd64`.
- **Dedicated verbose step:** `python -m pytest
  tests/integration/test_slice_c.py tests/integration/test_cancellation_e2e.py
  -v`, with `CODEAGENT_REQUIRE_DOCKER: 1`, gave `14 passed in 14.53s`. All 14
  are named `PASSED`: the four `test_slice_c.py` tests, and E1–E10 as ten test
  cases.
- **Complete suite:** `python -m pytest -q`, with `CODEAGENT_REQUIRE_DOCKER: 1`
  in that step's own environment, gave `4807 passed, 4 skipped in 193.24s`,
  with zero failed. 4,807 + 4 = 4,811, which matches the local collected total
  (4,809 passed + 2 skipped).
- **Leftover-container check:** the anchored
  `^codeagent-(verify-|baseline-|verification-)` filter over a full
  `docker ps -a` name listing produced empty output, and the step succeeded.

**Evidence limits:**
- `test_d4b_e2e.py` was not part of the verbose step.
- A20–A22 are not named in the log. Their execution is an inference from the
  complete-suite command, the matching collection total, the successful run,
  and Docker being required and available.
- The four quiet-mode skips are unidentified; `pytest -q` does not name them,
  and they are not guessed here.
- This is GitHub-hosted `ubuntu-24.04` x86_64 automated-test evidence, not a
  security review.

**Unchanged non-claims:** no operator path, no live model or provider
adapter, no D5 transactional patching, and neither Gate α nor v1 completion.

## Amendment 1 (Accepted 2026-11-07): continue after a pre-write patch validation failure (D5-A)

Status: **Accepted (2026-11-07)**, after joint review. D5-A is implemented,
locally verified on macOS (2026-11-07), committed as `79895f8` (an ordinary
fast-forward over `086dc62`), and Linux-CI-confirmed in run
[42454095719](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42454095719);
see "Amendment 1: Linux CI evidence" below. (Historical: before the commit,
this line said D5-A was "currently unstaged, uncommitted and not
Linux-CI-confirmed", which was true then.)
The final review accepted: Gate zero before classification for every mapped
T1–T7 failure; T8 and T9 keeping their existing exception and raise paths;
the behavioural C29 absence contract for D5-A (§A1.2, §A1.9); and A17, for a
final mutation count of 17 categories / 18 concrete runs (§A1.10). Milestone 2, ledger
D5, slice D5-A only. It advances the D5 row's "repair continues" criterion for
the pre-write case only; D5 stays "In progress, not Done".

**Source.** The jointly accepted D5 continuation plan, revision 3
(`D5_N4_patch_failure_continuation_plan_rev3.md`, kept outside the repository,
sha256 `9b7a7dd1246cae68fdd2aeba1bfab1a866d1d107cf6542ffb9c32880b32fc305`).
Where this amendment and that plan differ, this amendment governs once
accepted. Line references are to `086dc62`.

### A1.1 Scope

- **In scope:** after an `apply_patch` that failed before any write with
  `PATCH_VALIDATION_FAILED` (a stale `expected_text`), the run returns to
  EXPLORE with patch-failure feedback instead of ending.
- **Out of scope:** worktree recreation and N4 (slice D5-B, ADR 0004
  Amendment 21); four-operation patching, whole-proposal validation, N1, N2,
  the syntax guard and the test-file policy (D5-C); D6 and any operator path.
- **Supersession.** With this amendment implemented, the Consequences line
  "One stale `expected_text` ends the run (`PATCH_VALIDATION_FAILED`) until D5
  adds patch-failure feedback, four operations and worktree replacement" is
  labelled historical in this implementation diff.

### A1.2 Domain

- `domain.Trigger.PATCH_FAILED = "patch_failed"`, with exactly one legal edge,
  `(EXECUTE, PATCH_FAILED) → EXPLORE`. It is illegal from every other state. It
  is not an abort trigger and adds no `TerminalReason`.
- **Check site C30.** `controller._TRANSITION_CHECK_SITES` (in
  `controller.py:589-597`, not `domain.py`) gains
  `Trigger.PATCH_FAILED: "C30"`, so C30 is that transition's entry check.
- **C28** is a direct `_cancellation_check("C28")` in the patch-failure
  routing (§A1.5).
- **C29 is neither registered nor consulted in D5-A.** It stays reserved for
  slice D5-B, as the safe point immediately after a successful
  `WorkspaceRecreator.begin()`. D5-A never calls `begin()`, maps no trigger or
  tool to C29, and never runs a C29 check or records a C29 decision. This is a
  behavioural requirement, pinned by the site-inventory test (§A1.9), not a
  restriction on the source text.
- **The consulted-site set** extends decision 4 for patch-failure runs only:
  C1–C5 and C12–C27 as before, plus C28 and C30. C6–C11 stay retired. The
  existing inventory test
  (`test_d4b_model_loop.py::test_j2_consulted_site_inventory_is_c1_to_c5_and_c12_to_c27`)
  has no patch failure, so its expected set is unchanged.

### A1.3 Gate zero

Immediately after the failed `apply_patch`'s `ToolCompleted`, and before
classification, C28, the budget check, feedback or any transition:

> if `_any_verifier_cleanup_unconfirmed` is true, call
> `_terminate(UNRECOVERABLE_ERROR, error=E)` and stop, where `E` is the
> triggering patch error.

- The existing cleanup-unconfirmed precedence in `_terminate()` stays
  authoritative: the workspace is preserved, disposal and ref deletion are
  skipped, and `RunFinished.error` becomes `LIFECYCLE_CLEANUP_UNCONFIRMED`.
  `E` stays on its `ToolCompleted`.
- It applies to T1 too, as defence in depth. Today the state is unreachable:
  `events.validate_verification_outcome_shape` pairs an unconfirmed container
  cleanup only with `ENVIRONMENT_FAILURE` (`events.py:284-301`), which ends
  the run at baseline (`controller.py:1049-1057`) and at verification
  (`:1235-1237`). Tests reach Gate zero by fault injection.

### A1.4 Classification

`E` is the failed `ToolCompleted`'s error; the stage is `_patch_failure_stage`,
which is reset for every transaction.

| # | Stage / code | Session | D5-A disposition |
|---|---|---|---|
| T1 | `APPLY` / `PATCH_VALIDATION_FAILED` | `PRESENT` | **continue, no recreation** |
| T2 | `APPLY` / `PATCH_UNSUPPORTED_GIT_SUBSTRATE`, `PATCH_REPOSITORY_OBJECTS_UNAVAILABLE` | any | terminate with `E` |
| T3 | `ENTRY_GATE` / `WORKSPACE_ENTRY_GATE_FAILED` | `PRESENT` | terminate with `E` (recreation is D5-B) |
| T4 | `APPLY` / `PATCH_APPLICATION_FAILED` (post-write) | `PRESENT` | terminate with `E` (recreation is D5-B) |
| T5 | `ADVANCE` / a confirmed-`UNCHANGED` code | `PRESENT` | terminate with `E` (recreation is D5-B) |
| T6 | `ESTABLISH` / any; `ADVANCE` / any other code (transitional checkpoint failures) | not `PRESENT` | terminate, unchanged |
| T7 | `POSTCONDITION` / `INTERNAL_INVARIANT_VIOLATION` | `PRESENT` | terminate, unchanged |
| T8 | an unexpected `Exception` in the transaction | — | unchanged: one `TOOL_EXECUTION_FAILED` `ToolCompleted`, then `UNCLASSIFIED_FAILURE` |
| T9 | a non-`Exception` `BaseException` in the transaction | — | unchanged: propagates along Amendment 14's raise path |
| T10 | Gate zero holds (any of T1–T7) | — | terminate at Gate zero (§A1.3); T8 and T9 keep their own exception and raise paths |

**D5-A never continues on a contaminated worktree.** T1 is a pre-write
refusal, so nothing was written; the next pass's entry gate re-proves the
worktree before any later write. Every failure that may follow a write (T3–T5)
still terminates, even when a `WorkspaceRecreator` is supplied:
`_recreation_can_continue()` stays constant `False`, so the recreator is never
called. Any unknown failure terminates.

### A1.5 Ordering

After a failed `apply_patch`, in EXECUTE:

1. `ToolCompleted(apply_patch, success=False, error=E)` already exists
   (unchanged).
2. **Gate zero** (§A1.3).
3. **Classification** (§A1.4). Everything except T1 terminates with `E`.
4. **C28:** cancellation, then the wall clock. Observed: `CANCELLED`, or
   `BudgetExceeded(WALL_CLOCK)` then `BUDGET_EXCEEDED`; teardown acts on the
   current worktree.
5. **Repair budget:** if `repair_iterations_used >= max_repair_iterations`,
   emit the existing `BudgetExceeded(kind=REPAIR_ITERATIONS)` in EXECUTE, then
   terminate with `BUDGET_EXCEEDED`. Never continues.
6. **C30 and the transition:** `_transition(pass_index, PATCH_FAILED)` runs
   C30 (cancellation, then the wall clock) and then emits
   `StateTransitioned(EXECUTE → EXPLORE, patch_failed)`. Observed at C30: the
   run ends `CANCELLED` or `BUDGET_EXCEEDED` with the transcript and counters
   untouched.
7. **Only after C30 passes and the transition event is emitted:**
   - append one `FeedbackItem(PATCH_FAILED, …)` (§A1.6);
   - `repair_iterations_used += 1`, exactly once;
   - `pass_index += 1`;
   - start the next exploration pass, whose first model request is preceded
     by the existing C5.

**Nothing appends feedback or changes a counter before C30.** The existing
verification-failure path, which appends before its C23 transition
(`controller.py:1197-1211`), is not changed: nothing blocking lies between its
append and that check.

Event shape of a continuation:

```text
ToolCompleted(apply_patch, success=False, error=PATCH_VALIDATION_FAILED)
  [Gate zero passes] [C28] [budget ok]
StateTransitioned(EXECUTE→EXPLORE, patch_failed)    ← entry check C30
  (in memory: PATCH_FAILED feedback appended; counters advanced)
  [C5] ModelRequestStarted … next pass
```

### A1.6 Feedback

- **New kind:** `model_protocol.FeedbackKind.PATCH_FAILED`. It is **never
  dropped**: it is not added to `DROPPABLE_KINDS`, just like
  `VERIFICATION_FAILED` and `REVISION_REQUESTED`.
- **Body:** controller-built canonical JSON:

  ```json
  {"previous_attempt_failed": true, "plan_revision": 3,
   "change_digest": "<digest of the change that failed>",
   "error_code": "patch_validation_failed",
   "message": "<engine message, sanitized, truncated to 512 bytes>",
   "worktree_recreated": false, "untrusted": true}
  ```

  `plan_revision` and `change_digest` are those of the approved plan whose
  patch failed. `error_code` is the fixed `ErrorCode` value. `message` is the
  engine's already-sanitized message, truncated to 512 bytes on a UTF-8
  boundary.
- **Never included:** repository file content, `expected_text`, `new_text`,
  a diff, Git output, evidence-artifact data or identifiers, raw exception
  text, or a path other than the engine's sanitized fragment of the model's
  own path. Events keep their existing redaction (decision 7).
- **Bound:** a new `PATCH_FAILED_ITEM_MAX_BYTES`, asserted to be at most
  `VERIFICATION_ITEM_MAX_BYTES`.
- **Static context-fit proof.** `RunConfig`'s existing never-dropped allowance
  reserves `(max_repair_iterations + 1) × VERIFICATION_ITEM_MAX_BYTES` for
  repair feedback (`controller.py:564-570`). A `PATCH_FAILED` item is appended
  only after a charged continuation; a `VERIFICATION_FAILED` item is appended
  per verification failure, before its budget check. Every continuation of
  either kind charges the one shared repair counter, so the two kinds together
  number at most `max_repair_iterations + 1`. With
  `PATCH_FAILED_ITEM_MAX_BYTES ≤ VERIFICATION_ITEM_MAX_BYTES`, the existing
  allowance covers them, and the proof's formula is unchanged.

### A1.7 Reread and reapproval

- **Citations:** decision 7's current-pass rule is authoritative and
  unchanged. A revised plan may cite only successful repository-tool results
  from the new pass; earlier-pass results stay in the context, droppable and
  visible, but uncitable. A revised proposal therefore rests on fresh
  evidence.
- **Approval:** every revised plan goes through `_propose` and `decide()`.
  Approval is always requested; the digest binding (decision 6) is unchanged.
- **Identical re-proposal:** it reports `material_change=False`, as decision 6
  already defines, and approval is still requested. The feedback records that
  the previous attempt failed (`previous_attempt_failed: true`) and carries the
  failed `change_digest`, so the identical change is recognizable.

### A1.8 Budgets and precedence

- **Precedence:** Gate zero, then classification, then C28 (cancellation, then
  the wall clock), then the repair budget, then C30 (cancellation, then the
  wall clock).
- **Exhausted repair budget:** the existing `BudgetExceeded(REPAIR_ITERATIONS)`
  and `BUDGET_EXCEEDED`; no continuation.
- **Charging:** one continuation charges exactly one repair iteration, on the
  same counter verification failures use.
- **Verification attempts:** a patch failure never increments
  `_verification_attempt`; only reaching VERIFY does.
- **Passes:** `pass_index` advances once per continuation, and
  `iterations_used` keeps its ADR 0005 Amendment 1 definition (exploration
  passes begun, counted before C5).
- **Durations:** the failed `apply_patch`'s duration is counted once by
  `_dispatch_apply_patch`, unchanged. The continuation adds no duration.
- **Tool calls:** every `apply_patch` dispatch, in every pass, has a unique
  `tool_call_id` and exactly one `ToolCompleted`.

### A1.9 Acceptance tests

- **Real acceptance scenario (real Git; real Docker):**
  `tests/integration/test_patch_failure_continuation.py::test_d5a_stale_expected_text_continues_to_passing_verification_real_docker`.
  A scripted model proposes a change whose `expected_text` is stale. The patch
  fails pre-write with `PATCH_VALIDATION_FAILED`, then `PATCH_FAILED` feedback,
  a reread through the current pass's tools, a revised plan citing only that
  pass, reapproval, a real apply and checkpoint, and a passing real
  verification. A real-Git-only counterpart in the same file runs without
  Docker.
- **Gate zero, for T1 and for non-continuation mapped failures:**
  `test_t1_after_verifier_cleanup_unconfirmed_terminates`, and
  `test_gate_zero_precedes_classification_for_every_mapped_failure`,
  parametrized over T1, T2 (both codes), T3, T4, T5, T6 and T7. In each case the
  unconfirmed-verifier-cleanup state is injected, and the test asserts:
  - the D5-A patch-failure classifier was never called (a call-count spy on
    the module-level classifier the controller uses: zero calls);
  - no C28 entry in `_cancellation_sites`;
  - no `patch_failed` transition and no `PATCH_FAILED` feedback item;
  - no repair-budget check (no `BudgetExceeded`, counter unchanged) and no
    recreator call, even with a recreator supplied;
  - `RunFinished(UNRECOVERABLE_ERROR, error.code=LIFECYCLE_CLEANUP_UNCONFIRMED)`,
    `preserve()` called and no disposal.

  **Why the classifier spy is needed.** In D5-A, T2–T7 already end through the
  same `_terminate(UNRECOVERABLE_ERROR, error=E)` call Gate zero makes, and
  `_terminate()`'s precedence then gives the same cleanup-unconfirmed result.
  An implementation that applied Gate zero only to T1, or only to T1–T5, would
  therefore produce identical terminal events for T2–T7. Outcome assertions
  alone cannot catch it; only the ordering (Gate zero before classification)
  can.
- **Classification:** T1, T2 (both codes), T3–T5 (terminate in A), T6, T7, T8,
  T9 and T10, each asserting the terminal reason and error and the absence of a
  `patch_failed` transition where it terminates.
- **Ordering:** cancellation and deadline observed at C28 and at C30;
  `test_t1_cancellation_at_c30_leaves_transcript_and_counters_unchanged`; the
  exact site sequence in an extended cancellation sweep; and
  `test_patch_failure_site_inventory_slice_a`: a run with one T1
  continuation records C28, then C30, each exactly once for the failed patch,
  in that order, between the failed `ToolCompleted` and the next C5; no `"C29"`
  entry exists in `_cancellation_sites` or `_post_return_decisions`; and no
  value of `_TRANSITION_CHECK_SITES` or `_DISPATCH_CHECK_SITES` is `"C29"`.
- **Feedback:** the body's exact fields, its bound, its fit assertion, that it
  survives drop pressure, and redaction: no file content, `expected_text` or
  exception text in the body or any event.
- **Citations:** a revised plan citing an earlier-pass result is denied
  (`evidence_ref_invalid`).
- **Reapproval:** an identical re-proposal gives `material_change=False`,
  `decide()` is called, and the feedback holds `previous_attempt_failed` and
  the failed digest.
- **Recreation unreachable** (historical, D5-A only; D5-B makes recreation
  reachable on the internal continuation path and renames this test
  `test_contaminating_failure_never_continues_without_recreated`, ADR 0004
  §21.17): `test_slice_a_contaminating_failure_still_terminates`: with a
  recreator supplied, T4 gives no `begin()` call, no recreation event, and
  terminates with `E`.
- **Accounting:** budget boundaries at 0, 1 and the maximum; the
  `BudgetExceeded` fields; one charge per continuation; `iterations_used`;
  `_verification_attempt` unchanged; durations counted once; unique
  `tool_call_id`s; one `ToolCompleted` per `ToolRequested`.
- **Disclosed test corrections.** Existing tests that pin "a stale
  `expected_text` ends the run" are corrected to the continuation behaviour,
  not deleted, including `test_controller.py:422` and `:2142`,
  `test_d4b_model_loop.py:1530` and `test_d4b_e2e.py:193`. Each correction is
  listed in the implementation's engineering-log entry.

### A1.10 Mutation plan

Seventeen categories, eighteen concrete runs, each caught by a named test's
explicit assertion. A1–A16 are plan revision 3's; A17 is added by this
amendment, because revision 3's A5 removes Gate zero entirely and so cannot
catch a Gate zero applied only to some failures:

| # | Mutation | Caught by (named test: assertion) |
|---|---|---|
| A1 | continue on `PATCH_UNSUPPORTED_GIT_SUBSTRATE` | `test_t2_substrate_terminates`: `finished.error.code is PATCH_UNSUPPORTED_GIT_SUBSTRATE` and no `patch_failed` transition |
| A2 | continue on `PATCH_REPOSITORY_OBJECTS_UNAVAILABLE` | `test_t2_objects_unavailable_terminates`: the same shape |
| A3 | continue with the session not `PRESENT` | `test_t6_non_present_session_terminates`: terminal, no transition |
| A4 | continue on the postcondition | `test_t7_postcondition_terminates`: `INTERNAL_INVARIANT_VIOLATION`, no transition |
| A5 | continue T1 after an unconfirmed verifier cleanup (Gate zero removed) | `test_t1_after_verifier_cleanup_unconfirmed_terminates`: no C28 site, no transition, `LIFECYCLE_CLEANUP_UNCONFIRMED` |
| A6 | never charge the repair budget | `test_t1_repair_budget_exhausts`: `BudgetExceeded.observed_value == max` after max+1 refusals |
| A7 | budget checked after continuing | `test_t1_budget_zero_never_continues`: with `max_repair_iterations=0`, no `patch_failed` transition |
| A8 | `PATCH_FAILED` droppable | `test_patch_failed_item_never_dropped_under_pressure`: the item survives and `dropped_count` excludes it |
| A9 | omit the feedback item | `test_t1_feedback_item_appended`: the transcript holds exactly one `PATCH_FAILED` |
| A10 | omit `previous_attempt_failed`/`change_digest` | `test_identical_reproposal_reports_failure_and_requires_approval`: the body has both; `decide()` called with `material_change=False` |
| A11 | allow earlier-pass citations | `test_revised_plan_cannot_cite_earlier_pass`: `evidence_ref_invalid` denial |
| A12 | skip C28 | `test_cancellation_sweep_includes_c28_c30`: the exact site sequence |
| A13 | feedback appended before the transition, before C30 | `test_t1_cancellation_at_c30_leaves_transcript_and_counters_unchanged`: transcript length and repair counter unchanged |
| A14 | `_patch_failure_stage` not reset | `test_stage_reset_across_two_attempts`: the second attempt classified by its own stage |
| A15 | `_verification_attempt` incremented on a patch failure | `test_patch_failure_does_not_advance_verification_attempt`: the verifier called with attempt 0 after one T1 |
| A16 | T4 treated as T1 (continuing into a contaminated tree) | `test_slice_a_contaminating_failure_still_terminates`: no transition, `E` terminal |
| A17 | Gate zero narrowed and moved after classification: (i) applied only to T1; (ii) applied only to T1–T5 (**two concrete runs**) | `test_gate_zero_precedes_classification_for_every_mapped_failure`: the classifier spy's call count must be zero, so both variants fail all eight parameter cases (classification now runs first in every case). The cases that discriminate each narrowing by outcome are T2–T7 for (i) and T6–T7 for (ii) |

**Strict harness rule.** Each harness runs: a clean baseline (all target
tests pass), then every mutation in turn, then a clean baseline again. A
mutation counts as caught only if at least one named test fails by an explicit
assertion; a collection error, import error, timeout or unrelated failure is
not a catch. After every mutation the mutated source is restored and verified
by SHA-256 against the pre-run hash, with `PYTHONDONTWRITEBYTECODE=1` so a
restored same-size file cannot load a stale `.pyc`. An equivalent mutant is
reported as such with its reason, never silently dropped.

**Invalidated Steps 6–8 harnesses.** A harness is invalidated when D5-A
changes any production source it mutates or any test file it runs. Membership
is fixed mechanically from each harness's file lists at implementation:
- **Step 6 (27 mutations): invalidated.** It mutates `controller.py`
  (recreation classification and `_attempt_recreation`), which D5-A changes.
  Its script is not preserved in the planning workspace, only its recorded
  output (each mutation's name and catching tests). Before D5-A's mutation run
  it must be located or rebuilt from that record, and a rebuild is disclosed.
- **Step 7 (33 mutations, `_lifecycle_run.py`; runs
  `test_controller_recreation.py` and `test_worktree_recreation.py`):
  invalidated if D5-A edits either test file.** That is expected, because the
  `validation` case of `test_non_candidates_never_reach_the_recreator` now
  drives a continuation rather than a termination.
- **Step 8 (24 mutations, `_lifecycle_run.py`, `reconciliation.py`,
  `worktree_absence.py`): not expected to be invalidated.** D5-A changes none
  of those sources and none of its tests. If D5-A edits any of them, Step 8 is
  re-run.
- **The same rule applies to the D4b mutation set**, because D5-A rewrites
  `controller.py`'s patch-failure routing.

### A1.11 CI

- `.github/workflows/ci.yml`'s verbose "Run real Docker verification tests"
  step gains exactly one node:
  `tests/integration/test_patch_failure_continuation.py::test_d5a_stale_expected_text_continues_to_passing_verification_real_docker`.
- `test_patch_failure_continuation.py` stays in the quiet full suite in full;
  no other node or file is added to the verbose step.
- **Expected added verbose runtime:** about 1–4 s. This is an estimate,
  replaced by the measured value in the documentation reconciliation.
- **Exact-commit Linux CI** for the D5-A commit must succeed before its
  documentation reconciliation is written.

### A1.12 Estimate and status

- **Accepted provisional D5 estimate:** **174–289 focused planning hours**, low
  confidence, replacing 95–155. The non-overlapping decomposition, from plan
  revision 3:

  | Component | Hours |
  |---|---|
  | C-1 Steps 1–8 | 56–92 |
  | remaining Step 9 | 1–2 |
  | D5-A, excluding Step 9 | 22.5–36 |
  | D5-B, excluding Step 9 | 49.5–79 |
  | D5-C | 45–80 |

- **Derived totals:**

  | Item | Hours |
  |---|---|
  | Remaining D5–D10 | 337–629 |
  | First operator run | 373–627 |
  | Gate α | 440–762 |
  | Public v1 | 502–901 |
  | Gate O (D1–D3) | 86–140, unchanged |
  | Completed D1–D4 | 165–272, unchanged |

- **D5-C** must be re-estimated from its own detailed plan before it is
  implemented; its 45–80 h is the least grounded component.
- **Ledger timing.** This implementation diff updates
  `docs/V1_COMPLETION_LEDGER.md`, `CLAUDE.md` and `ENGINEERING_LOG.md` to the
  accepted 174–289 h D5 range and the derived totals above.
- These are component planning estimates, not measured hours and not delivery
  commitments.

### A1.13 Non-claims

- D5 is not complete. D5-A is an incomplete subset.
- No recreation continuation. Post-write failures still end the run until
  D5-B.
- No live model, provider adapter or operator path. The only model remains
  the test-only scripted fake.
- No new threat-model conclusion. T-E1, T-F1 and T-F2 are unchanged.
- Continuing after a pre-write refusal does not prove the four-operation
  protocol, whole-proposal validation, the syntax guard, test-file rules, N1
  or N2.

## Amendment 1: Linux CI evidence (2026-11-08)

Commit `79895f8842425c0589576547de80aa2deda6e098` ("feat: continue after
pre-write patch failure"), pushed to `main` as an ordinary fast-forward over
`086dc62`. Run
[42454095719](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42454095719)
was verified through the Actions API and an independently fetched raw job log.

- **Run:** push-triggered; head SHA exactly `79895f8…e098`; attempt 1;
  `completed` / `success`; created 2026-11-08T06:16:56Z, updated
  2026-11-08T06:20:21Z.
- **Job:** exactly one job, "Test (ubuntu-24.04, Python 3.12)", 06:17:00Z to
  06:20:21Z. All 13 API-reported steps succeeded. There are zero API
  annotations and zero `##[error]` lines in the raw log.
- **Environment:** runner version 2.338.2; runner image `ubuntu-24.04`, version
  20261101.360.1 (Ubuntu 24.04.5); CPython 3.12.16; Git 2.56.0; Docker client
  and server 28.0.4.
- **Docker:** the mandatory preflight succeeded. The pinned
  `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`
  reported `linux/amd64`.
- **Dedicated verbose step** (06:17:10Z to 06:17:22Z): `python -m pytest
  tests/integration/test_slice_c.py tests/integration/test_cancellation_e2e.py
  tests/integration/test_patch_failure_continuation.py::test_d5a_stale_expected_text_continues_to_passing_verification_real_docker
  -v`, with `CODEAGENT_REQUIRE_DOCKER: 1`, gave `15 passed in 11.87s`. All 15
  are named `PASSED`: the four `test_slice_c.py` tests, E1–E10 as ten test
  cases, and the D5-A real-Docker acceptance node.
- **Complete suite** (06:17:22Z to 06:20:18Z): `python -m pytest -q`, with
  `CODEAGENT_REQUIRE_DOCKER: 1` in that step's own environment, gave
  `7005 passed, 4 skipped in 174.93s`, with zero failed. 7,005 + 4 = 7,009,
  which matches the local collected total (7,007 passed + 2 skipped).
- **Leftover-container check:** the anchored
  `^codeagent-(verify-|baseline-|verification-)` filter over a full
  `docker ps -a` name listing printed nothing after its header, and the step
  succeeded.

**Evidence limits:**
- The D5-A real-Docker acceptance node is named evidence. The other D5-A
  tests ran only in the `pytest -q` step; their execution is inferred from the
  complete-suite command, its successful result and the matching collected
  total, not named in the log.
- The four quiet-mode skips are unnamed; `pytest -q` does not name them, and
  they are not guessed here. Docker was required and available in that step,
  so they are not attributable to unavailable Docker.
- `test_executor.py::test_l2_timeout_kills_the_whole_process_group` (D3)
  passed here but remains a disclosed pre-existing intermittent test
  (`ENGINEERING_LOG.md`, 2026-11-07 D5-A entry).
- This is GitHub-hosted `ubuntu-24.04` x86_64 automated-test evidence, not a
  security review.

**Unchanged non-claims:** D5 is not complete. (Historical: when this evidence
was recorded, D5-B (N4 recreation continuation, C29) was not implemented and
live recreation stayed unreachable. D5-B is now implemented, committed as
`ba653b7` and Linux-CI-confirmed in run 42592929825 (ADR 0004 §21.17).) D5-C and D6 remain unimplemented. No live model, provider adapter or operator path; no Gate α,
operator-ready, `COMPLETE_FOR_V1` or v1-complete claim; no threat-model
conclusion changes.

## Amendment 2 (Accepted 2026-11-08): recreation feedback after D5-B

Status: **Accepted (2026-11-08)**, after joint review. **Implemented, locally
verified on macOS (2026-11-08), committed as `ba653b7`, and confirmed on
GitHub-hosted Linux CI (run [42592929825](https://github.com/G-ChandraSekhar/codeagent/actions/runs/42592929825)); evidence in ADR 0004 §21.17.**
(Historical: before the commit this line said "Implemented and locally verified
on macOS (2026-11-08); unstaged, uncommitted and not Linux-CI-confirmed"; at
acceptance it said "Not implemented.") The controller appends `"worktree_recreated": true`
and `"recreation_outcome": "recreated"` only after `RECREATED` and C30.
`test_recreated_feedback_body` pins the exact body.
`test_patch_failed_bound_fits_the_shared_repair_allowance` uses the
recreation form (corrected during verification: it first still had the D5-A
form), and `PATCH_FAILED_ITEM_MAX_BYTES` is unchanged. D5-A's
`test_t1_feedback_body_exact` is unchanged. It was accepted together with ADR 0004 Amendment 21 (Accepted 2026-11-08, D5-B:
live recreation continues the repair loop). ADR 0004 Amendment 21 governs when the item below is
produced in the lifecycle sequence; this amendment governs only its schema.

### A2.1 Schema

The `PATCH_FAILED` feedback item of Amendment 1 §A1.6 gains a recreation form:

- **After a D5-B `RECREATED` result and a passed C30** (ADR 0004 Amendment 21
  §21.3 steps 11–14): the body is Amendment 1's body with
  `"worktree_recreated": true` and one added field,
  `"recreation_outcome": "recreated"`.
- **D5-A without recreation** (T1): unchanged. `"worktree_recreated": false`
  and no `recreation_outcome` field; Amendment 1's exact schema stands.
- **Unchanged in both forms:** `previous_attempt_failed: true`, the plan
  revision and failed `change_digest`, the triggering error's fixed code as
  `error_code`, and the engine's sanitized message truncated to 512 UTF-8
  bytes on a character boundary as `message`, `untrusted: true`.

### A2.2 Invariants

- The item stays never-droppable (`FeedbackKind.PATCH_FAILED` is not in
  `DROPPABLE_KINDS`).
- Nothing else is added: no repository content, patch content (`expected_text`,
  `new_text`), raw exception text, Git output, evidence-artifact data or
  identifiers, lifecycle identifier, or unsanitized path.
- **Bound:** `PATCH_FAILED_ITEM_MAX_BYTES` covers the new field, and its
  worst-case test is extended to include `"worktree_recreated": true` and
  `"recreation_outcome": "recreated"`. The constant must stay at most
  `VERIFICATION_ITEM_MAX_BYTES`; the existing value (twice the 512-byte message
  plus 512) is expected to suffice, and the extended test decides.
- **Static context-fit proof:** unchanged and still valid. Patch and
  verification feedback share the single repair counter, so the two kinds
  together number at most `max_repair_iterations + 1`.
- No event schema changes. No other decision of this ADR or of Amendment 1
  changes.

### A2.3 Tests

`test_recreated_feedback_body` (exact body with both recreation fields, after
`RECREATED` and C30); the extended worst-case bound test; D5-A's exact-body test
unchanged. These are part of ADR 0004 Amendment 21's test plan and its
mutation B13; this amendment adds no mutation category.

### A2.4 Non-claims

No D5 completion, four-operation protocol, live model, provider adapter,
operator path, Gate α, operator-ready, `COMPLETE_FOR_V1` or v1 claim, and no
threat-model change.
