# ADR 0007: Model-driven turn loop, response precedence and approval binding

Status: Accepted (2026-10-20), at joint review. Records the invariants
accepted in the D4 plan (revisions 5 and 6) and the D4b readiness addendum
(J1–J4), as implemented by ledger D4b.

Implementation status: **implemented, locally verified on macOS
(2026-10-20) and accepted at joint review; Linux CI pending.** Not operator-reachable: no
CLI path runs a task, the only model is a test-only scripted fake, and
`_lifecycle_run` stays unimported by any bundled module (T41). See
`ENGINEERING_LOG.md`'s 2026-10-20 D4b entry for the exact verification.

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

Evidence remains local macOS only; Linux CI pending.
