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

Amendment 3 (ledger D5, slice D5-C: four-operation proposals, v2 digests, the
patch policy and `ApprovedChange` authority): **Accepted 2026-11-10, after joint review; not implemented.** (Historical:
Proposed 2026-11-09.) See "Amendment 3" at the end of this ADR.

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

## Amendment 3 (Accepted 2026-11-10): four-operation proposals, v2 digests, the patch policy and `ApprovedChange` authority (D5-C)

Status: **Accepted 2026-11-10, after joint review; not implemented.**
(Historical: "Proposed (2026-11-09). Not accepted and not implemented.")
- D5-C is not implemented.
- D5 stays **In progress, not Done**.
- D6, operator reachability, Gate α and v1 remain unimplemented and unclaimed.

**Source:** the accepted D5-C plan chain. The governing plan is **revision 12**:
`/Users/chandrasekhar/code/codeagent-plans/D5_C_transactional_patch_protocol_plan_rev12.md`.
- Revision 12's accepted-content SHA-256 is
  `241c11f9b2169a5dba080fbdf82cd90e2c4e115f08ab80a56513678346812566`. The file
  now hashes `de42aaa199a1fbf200a68623c5405f9f190e2e57a7903607d6a002b55a0b38d8`,
  because only its status line changed to Accepted.
- **Order of governance:**
  - revisions 8–12 govern wherever they supersede revisions 1–7, each later
    revision governing the one before it;
  - revision 7 governs revision 6, revision 6 governs revision 5, and revision
    5 governs revision 4, which carries revision 3's design and revision 2's
    protocol.
- Revision 7's accepted-content SHA-256 is
  `4ef87d2d4134fb8fc3fe0a1d522cbec403be4851c667b2f9c4c1c3cf31592adb`, and
  revision 6's is
  `0a17f54c6073e46c3d7825dcc8bf540df67d3f08cb93f82266d579963fc2c76b`.
- **Revision 13** (accepted at joint review 2026-11-10;
  accepted-content SHA-256 `b39295869f8ed3388acd35b24519c81823de2cb6548738cc0e958a5ef671c744`; the file now
  hashes `4eb60305bb78a53dd423ef3eb466bca770d46af3ad28f21560f98d23478eba21` after its status-line change)
  records the 2026-11-10 decisions on Phase V routing and on the hotfix harness
  as a prior rerun (ADR 0003 Amendment 3 §A3.2 and §A3.9).
- **The mandatory pre-D5-C hotfix prerequisite is satisfied.** ADR 0006
  Amendment 5 is committed as `067fca0` and Linux-CI-confirmed in run
  42908777385; its evidence reconciliation is commit `6091f3b`. D5-C itself
  remains unimplemented.

**Ownership:** ADR 0003 Amendment 3 §A3.1 holds the ownership map. This
amendment owns the model-facing protocol, the digests, the policy, the contract
and classification. It cross-references the transaction (ADR 0003 Amendment 3),
the Git mechanics (ADR 0006 Amendment 6) and the sessions and cleanup (ADR 0004
Amendment 22).

### A3.1 The operations schema (replaces the single `change`)

`propose_plan`'s `change` is replaced by `operations`: a JSON array of 1 to
**8** objects. Each object has the discriminator `"op"` and exactly these keys,
all required, with no extras and no `null`:

| `op` | Keys | Meaning |
|---|---|---|
| `update` | `path`, `expected_text`, `new_text` | exact single-occurrence replacement (as today) |
| `add` | `path`, `content` | create a new regular file, mode 100644, with exactly `content` (which may be empty) |
| `delete` | `path` | remove a tracked regular file |
| `rename` | `from`, `to` | move a tracked regular file; bytes and mode unchanged |

**Limits:**
- each text field is strict UTF-8 with no lone surrogate, at most **64 KiB**;
- Σ(`expected_text`, `new_text`, `content`) ≤ **131,072** bytes;
- every path component ≤ **255** UTF-8 bytes, on top of the existing path rules
  (512-byte path, depth 32, `.git`/`git~1` denial).

No binary payload, no mode field, and no symlink, gitlink or directory
operation.

**Static rules** (refused as per-call denials, which are not turn-level):
- `rename` with `from == to` → `CHANGE_UNCHANGED`;
- a repository-control component (set defined by ADR 0006 Amendment 6 §A6.11) →
  `CONTROL_PATH`;
- an unknown `op` → `UNKNOWN_OPERATION`;
- an over-long component → `PATH_COMPONENT_TOO_LONG`.

**Cross-operation rules.** With *P* the multiset of all named paths, and
*key(p)* = NFC then casefold per component:
1. no two members are equal by *key*;
2. no member is a directory prefix of another by *key*;
3. 1 ≤ count ≤ 8.

A violation is `OPERATION_CONFLICT`. These rules exclude chains, swaps, cycles,
case-only renames and add-over-delete, and they make the final tree
order-independent.

**New `ArgumentRejection` members:** `OPERATION_CONFLICT`, `CONTROL_PATH`,
`PROTECTED_PATH`, `UNKNOWN_OPERATION` and `PATH_COMPONENT_TOO_LONG`.

**Repository-state rules** (checked in Phase V; ADR 0003 Amendment 3 §A3.2):

| Rule | `update` | `add` | `delete` | `rename` |
|---|---|---|---|---|
| Source tracked at HEAD as 100644/100755 | yes | — | yes | `from` |
| Source on disk: a regular file reached by a no-follow walk, ≤ 64 KiB | yes (also strict UTF-8) | — | yes (any bytes) | `from` (any bytes) |
| Target absent on disk; no ancestor a non-directory | — | yes | — | `to` |
| Target absent at HEAD; no *key* collision with HEAD entries in the same parent | — | yes | — | `to` |
| Target not ignored (ADR 0003 Amendment 3 §A3.3) | — | yes | — | `to` |
| Six-attribute ADR 0006 safety, cached and working tree, through `ReadOnlyGit.classify_attributes` against the capability's verified filter facts (ADR 0006 Amendment 6 §A6.2b) | yes | yes | yes | both |
| `expected_text` occurs exactly once; result ≤ 64 KiB | yes | — | — | — |
| Syntax guard (§A3.6) | `.py` | `.py` | — | — |
| Trusted policy (§A3.4–§A3.5) | yes | yes | yes | yes |

The proposal is also subject to the exact evidence preflight
(ADR 0003 Amendment 3 §A3.5).

**No compatibility path:** `change`, `ProposedChange`, `PatchOperation` and the
single-operation applier guard are removed. Every scripted single change becomes
one `update`.

### A3.2 Canonical forms and digests

`tool_schema.compute_digests(args, policy_fingerprint) -> PlanDigests` is the
only implementation. It validates `policy_fingerprint` as 64 lowercase hex.
`canonical_json` is unchanged: sorted keys, `(",", ":")` separators,
`ensure_ascii=False`, UTF-8.

**Canonical operation objects.** The `op` key is emitted in exactly one place:
- `{"op": "update", "path", "expected_text", "new_text"}`
- `{"op": "add", "path", "content"}`
- `{"op": "delete", "path"}`
- `{"op": "rename", "from", "to"}`

These are sorted by the UTF-8 bytes of the primary path (`path`, or `from`).

```
change_digest = sha256(canonical_json({"schema": "codeagent.change.v2",
                                        "operations": <canonical list>}))
plan_digest   = sha256(canonical_json({"schema": "codeagent.plan.v2",
    "change_digest": <change_digest>, "operations": <canonical list>,
    "evidence_refs": <list, proposal order>, "problem_hypothesis": <str>,
    "verification_intent": <str>, "risk_notes": <str>,
    "policy_fingerprint": <§A3.4 fingerprint>}))
```

- `plan_revision` is outside both digests and is checked separately (§A3.7).
- `material_change` still compares `change_digest` only.
- **Redaction** (`ToolRequested`, `PlanProposed`): per operation, its kind, its
  path(s), and for each text field the byte count and SHA-256. Never content.
- **`proposed_file_paths`** and **`PatchApplied.files_changed`** are every
  member of *P*, sorted.

**Golden vectors** (`test_digest_golden_vectors`). The mixed proposal (add
`new/a.py`; update `src/m.py` `a`→`b`; delete `old.txt`; rename `doc/ré.md`→
`docs/ré.md`; evidence references `tc-1-1` and `tc-1-2`; hypothesis `h`;
intent `v`; empty risk notes; `no_human`) gives:
- `change.v2`: `b349a0abbae9b45c57d06433644f1d7591e731452363b6e9967bc43b66df666a`;
- `plan.v2`: `e80dce4a6199be9e17e204a31027ee080faeb65feba49ccfd995bdfe8a1bf17a`.

Implementation adds one vector per operation kind and one non-ASCII vector.
Changing any vector requires a schema version bump.

**Field-binding inventory:** 24 canonical fields, each with exactly one mutation
disposition.

| Disposition | Count | Fields or IDs |
|---|---|---|
| Prior mutation | 1 | `plan.v2.risk_notes`: D4b MR3, re-anchored; its `-k digest` selection reaches `test_plan_digest_binding_sweep` and `test_digest_golden_vectors` |
| D5-C mutations | 23 | D-02, D-03, D-04, D-08…D-27 |
| Equivalent | 0 | — |

Of the 23, **8** are redundant fields caught only by a golden vector: D-03,
D-11, D-19, D-20, D-24, D-25, D-26 and D-27. Their omission changes the schema
bytes but not the security binding: the operations are bound twice in
`plan.v2`, the policy sets are exact, and the version is fixed.

### A3.3 Binding sweep

`test_plan_digest_binding_sweep` checks, for every plan-bound field, two
things: a mint with altered arguments and the original digests raises
`ValueError` and registers nothing; and an in-place alteration of a minted
snapshot is refused by `verify_approved_change` before any I/O.

Fields covered:
- `problem_hypothesis`, `verification_intent`, `risk_notes`;
- `evidence_refs` (added, removed, reordered);
- each operation's `op`;
- each kind's own fields;
- the operation count;
- `approval_class`;
- `plan_revision`, as a record mismatch.

Policy rules, configuration names and version cannot vary under §A3.4's
validation, so they are construction-refusal tests instead.

### A3.4 `PatchPolicy` and `codeagent.patch_policy.v1`

**Closed types** (in `patch_contract`):
- `ApprovalClass`: `HUMAN_INTERACTIVE = "human_interactive"`,
  `NO_HUMAN = "no_human"`.
- `TestPathRule` matches on each component's *key* (NFC, then casefold). "Final"
  is the last component; "non-final" is every other component.

| Member | Value | Matches when |
|---|---|---|
| `DIR_COMPONENT_TEST` | `"dir_component_test"` | some non-final component equals `"test"` |
| `DIR_COMPONENT_TESTS` | `"dir_component_tests"` | some non-final component equals `"tests"` |
| `FILE_CONFTEST_PY` | `"file_conftest_py"` | the final component equals `"conftest.py"` |
| `FILE_PREFIX_TEST_PY` | `"file_prefix_test_py"` | the final component starts with `"test_"` **and** ends with `".py"`. There is **no length test**, so `test_.py` matches |
| `FILE_SUFFIX_TEST_PY` | `"file_suffix_test_py"` | the final component ends with `"_test.py"`. There is **no length test**, so `_test.py` matches |

The two filename predicates equal `fnmatch.fnmatchcase(key, "test_*.py")` and
`fnmatchcase(key, "*_test.py")`, where `*` may be empty. pytest collects
`test_.py` and `_test.py` (planning probe; SHA-256
`3f64271e329bbbf9a3f3165abeb6ef32b89f8e74c8a12a256a173aceb61e10a7`).

- `VerificationConfigName`: `".coveragerc"`, `"conftest.py"`,
  `"pyproject.toml"`, `"pytest.ini"`, `"setup.cfg"` and `"tox.ini"`. A path
  matches when its final component's key equals the value, at any depth.

**Fields and validation** (exact types; refusals carry fixed text):
- `version`: `type is int`, and equal to 1;
- `approval_class`: `type is ApprovalClass`;
- `test_path_rules`: a tuple of `TestPathRule`, strictly increasing by the
  UTF-8 bytes of each value (no unsorted or duplicate input), and exactly equal
  to all five;
- `verification_config_names`: likewise, exactly equal to all six.

The only helper is `policy_for_approval_mode`. It maps `INTERACTIVE →
HUMAN_INTERACTIVE`, and `NONE` and `PLAN_FILE → NO_HUMAN`. `PLAN_FILE` stays
`NO_HUMAN` until D6 defines and proves a human-authored, digest-bound plan-file
approval, and a later accepted design update moves it. Exactly **two** v1
policies exist.

**Canonical object:**
```
{"approval_class": <value>, "schema": "codeagent.patch_policy.v1",
 "test_path_rules": [<values, UTF-8 byte order>],
 "verification_config_names": [<values, UTF-8 byte order>], "version": 1}
```

- **`fingerprint`** is its SHA-256. No callable, `repr`, enum member name, set
  iteration or platform-dependent value enters it.
- **Golden fingerprints:**
  - `human_interactive`: `05c2732f8b439e28438f317a9007f54ff3032f198e2de6b12307ce38b42fff94`;
  - `no_human`: `964a2796ba9edd54e05872b76ef84c82c488b4d9f605249c041ccfc12bd1cd89`.
- **Executable predicates:** a module-private `MappingProxyType` keyed by
  `TestPathRule` member and total over the enum. Predicates are reached only
  through trusted members and never enter the fingerprint.

### A3.5 The test-file and verification-configuration policy

A non-rename path is **test-involved** when it matches a trusted rule; a rename
is test-involved when either its source or target matches.

| Operation involving a test path | `INTERACTIVE` (`HUMAN_INTERACTIVE`) | `NONE` | `PLAN_FILE` |
|---|---|---|---|
| `add` | allowed, highlighted | refused | refused |
| `update` | allowed, highlighted | refused | refused |
| `delete` | refused | refused | refused |
| `rename` (source or target) | refused | refused | refused |
| any operation on verification configuration | refused | refused | refused |

- Refusals are `PROTECTED_PATH`: at `propose_plan`, and again in the applier
  from the trusted policy.
- **Highlighted** means `PlanProposed` and `ApprovalRequest` gain `test_file_paths:
  tuple[str, ...]` (sorted). That is an event and type addition.
- In `NONE` and `PLAN_FILE` nothing claims a human looked.
- No weakening detection is claimed for allowed interactive updates.

**Boundary tests** (`test_test_path_rule_boundaries`,
`test_test_path_predicates_match_pytest_globs`, and `test_policy_table` rows):
- `test_.py`, `_test.py`, `test_a.py` and `a_test.py` are test paths, including
  casefolded and NFD variants;
- `test.py`, `test_`, `_test`, `atest.py` and `test_a.txt` are not test paths
  at the root, but are under `tests/` or `test/`;
- under `NONE` and `PLAN_FILE`, add and update of `test_.py` and `_test.py` are
  refused;
- under `INTERACTIVE`, they are allowed and highlighted;
- delete and rename are refused in every mode.

### A3.6 Syntax guard

- **Scope:** `.py` targets of `add` and `update`.
- **Method:** in-process `ast.parse(content, feature_version=(3, 12))`.
  Nothing is executed and nothing is spawned.
- **Refusal:** the new content does not parse **and** (the operation is `add`,
  or the old content parsed).
- **Errors caught:** `SyntaxError`, `ValueError`, `RecursionError` and
  `MemoryError`.
- **Message:** "operation *i*: Python syntax error at line L, column C". It
  never includes source text.
- **Result:** `PATCH_VALIDATION_FAILED`, route T1.

### A3.7 `patch_contract` and `ApprovedChange` authority

**New dependency-neutral module `codeagent.patch_contract`.** It imports only
`domain`, `errors`, `tool_schema` and the standard library, never `controller`,
`patch`, `executor`, `_lifecycle_run`, `_git_safety`, `evidence` or `workspace`.
- It holds `PatchPolicy` and its types (§A3.4), **`PatchResult`** (moved from
  `controller`, which re-exports it), `ApprovedChange`, the registry, the mint
  function and `verify_approved_change`.
- `patch` no longer imports `controller`.

**`ApprovedChange`.** A plain `__slots__` class, **not a dataclass**:
- **Fields:** `args`, the full approved `ProposePlanArgs` snapshot; `policy`;
  `change_digest`; `plan_digest`; `plan_revision`; `approved_paths`, a
  frozenset derived at mint; `_token`; `__weakref__`.
- **Equality and hashing:** no `__eq__` or `__hash__` is defined, so both are
  object identity.
- **Immutability:** `__setattr__` and `__delattr__` raise `AttributeError`.
- **No copies:** `__copy__`, `__deepcopy__` and `__reduce_ex__` raise
  `TypeError`. `dataclasses.replace` raises `TypeError` because the class is not
  a dataclass.
- **Construction** requires `_token is _MINT_TOKEN`.

**Registry:**
- `_MINTED` is a `weakref.WeakKeyDictionary[ApprovedChange, _MintRecord]`, so
  membership is the live key object's identity, never a stored integer id.
- `_MintRecord` = `(change_digest, plan_digest, policy_fingerprint,
  approved_paths, plan_revision)`, captured at mint.
- An entry disappears once the minted object is collected.

**Mint.** `_mint_approved_change(args, policy, digests, revision)` is called
only by `controller`, at dispatch, after its existing digest recheck. It
recomputes **both** digests and the paths, then registers; a mismatch is
`ValueError` and nothing is minted.

**Verify.** `verify_approved_change(change)` is the applier's first statement,
before any Git or filesystem access. Its eight checks, all required:
1. `type(change) is ApprovedChange`;
2. the token is correct;
3. registry membership;
4. the `args` and `policy` types;
5. the fingerprint equals the record;
6. both digests, recomputed from the snapshot, equal both the stored values and
   the record;
7. the derived paths equal the stored paths and the record;
8. the revision equals the record.

**Applier signature:** `GitPatchApplier.apply(run_id, iteration, change:
ApprovedChange)`.

**Specified construction outcomes** (planning probe on Python 3.12.16; SHA-256
`8837b266cd0a441f29242e5b4e8bf63d4022b4973393cfe81cb2d5ebe504b846`):
- Construction without the token: `TypeError`.
- `replace`, `copy`, `deepcopy`, `pickle`: `TypeError`.
- An `object.__new__` clone with copied fields: not equal, not registered, and
  refused at step 3 before I/O.
- In-place tampering with the snapshot, policy or stored digests: refused at
  steps 5–7 before I/O.
- After the last reference is deleted and `gc.collect()` runs, the registry
  entry is gone.

**Required registry tests** (no allocator-specific `id()` reuse):
- `test_registry_entry_removed_after_collection`;
- `test_registry_membership_is_by_live_key`.

A reuse test is supplemental only (`CODEAGENT_SUPPLEMENTAL=1`) and never
required.

**Authority limit.** This is **repository-enforced construction authority**,
not an unforgeable language security boundary. A caller that deliberately
imports the private token **and** mutates the private registry can forge an
accepted instance. The defences are the private names, AST pins (one production
mint site; no `src` access to `_MINTED`, `_MINT_TOKEN` or `_MintRecord` outside
`patch_contract`; an acyclic import graph; no `id(` in `patch_contract`), and
review.

### A3.8 Classification and feedback

**T1–T10 are unchanged** (Amendment 1).

| Failure | Route |
|---|---|
| A Phase V refusal, including the evidence preflight over the bound | `PATCH_VALIDATION_FAILED`, T1 |
| A substrate refusal, a refused session open, a preflight infrastructure failure, split index | T2 |
| `GitSafetyError(FILTER_DRIVER_NAME_UNRENDERABLE)` or `GitSafetyError(FILTER_CONFIGURATION_CHANGED)` in Phase V | `PATCH_UNSUPPORTED_GIT_SUBSTRATE`, fixed sanitized text, **T2**: terminate; never `PATCH_FAILED` feedback, C28, C29, C30, continuation or recreation |
| `GitCapabilityRefused` in Phase V | **not translated**: no `PatchResult`, never T1 or T2; **T8** (one `TOOL_EXECUTION_FAILED` `ToolCompleted` with the fixed text "tool failed unexpectedly", then `UNCLASSIFIED_FAILURE`; no capability, session, configuration, path or exception text in any event; no feedback, C28, C29, C30, recreation, verification or later Git launch) |

The split for the new refusal classes is **owned by ADR 0003 Amendment 3 §A3.2** and is repeated here only as the classification it produces. T1–T11 are otherwise unchanged, as are Gate zero, cleanup dominance and interrupt precedence. (Historical: this row read "open: revisions 8–12 assign no route" before revision 13.)
| Any Phase M/C failure | `PATCH_APPLICATION_FAILED`, T4 (recreation) |

**New T11:** `GIT_SCRATCH_CLEANUP_UNCONFIRMED`, at any patch stage, after Gate
zero, **terminates**. It is never T1 or T3–T5. The code, record and precedence
are defined in ADR 0004 Amendment 22.

**Feedback:** `PATCH_FAILED` messages are categorical, with the operation index
and a sanitized path fragment, never content. `PATCH_FAILED_MESSAGE_MAX_BYTES`
and the item bound are unchanged. Phase S denials use the existing `TOOL_DENIED`
item.

### A3.9 Tests and mutation obligations

**Required tests:**
- schema, limits and cross-operation rules;
- golden vectors;
- `test_plan_digest_binding_sweep`;
- malformed-policy construction (`test_patch_policy_validation`) and
  `test_policy_has_exactly_two_v1_values`;
- `test_test_path_predicates_total`;
- the boundary tests (§A3.5);
- the policy table (every cell × every mode);
- `test_file_paths` highlighting;
- the syntax guard;
- the import graph, the mint site, and the identity/forgery/tampering contract
  tests;
- redaction;
- the applier receives exactly the minted change;
- the scripted-model four-operation end-to-end test, on real Git and on real
  Docker;
- the Phase V routing tests, owned and listed in ADR 0003 Amendment 3 §A3.2,
  which exercise this section's classification.

**Mutation groups owned here** (counted once in ADR 0003 Amendment 3 §A3.9's
228; Q-01…Q-45 are owned by ADR 0006 Amendment 6). (Historical: 206 before
revisions 8–12, and 226 before revision 13.)
- S-01…S-18;
- D-01…D-27;
- P-01…P-26, including P-25 (empty prefix wildcard excluded) and P-26 (empty
  suffix wildcard excluded);
- A-01…A-18;
- C-01…C-08 (C-09 and C-10, the Phase V routing variants, test ADR 0003
  Amendment 3 §A3.2's decision and are listed there);
- K-01…K-22.

### A3.10 Non-claims

- No live model or provider adapter, and no operator path.
- No weakening detection.
- No language-level unforgeability, for `ApprovedChange` or for the Git
  capabilities it is applied through (ADR 0006 Amendment 6 §A6.2b).
- The applier consumes capability refusals as defined in ADR 0006 Amendment 6
  (fixed message, no cause or context; interrupts propagate by identity). This
  amendment does not redefine them.
- No change to D4b's turn-loop precedence or budgets beyond the
  `propose_plan` schema.
- No implementation.
