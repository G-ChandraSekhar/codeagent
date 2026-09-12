# CodeAgent — Implementation Guide and Project Handoff

Status: PRE-BUILD GUIDANCE  
Companion to `DESIGN_SPEC.md` and `BUILD_PLAN.md`

## 0. How to use this document

This document is project context, not an instruction to make unrequested changes.
An implementing engineer or LLM should first inspect the current repository and
confirm the user's immediate request before editing files, running commands, or
changing the architecture.

When this guide conflicts with an unresolved option in `DESIGN_SPEC.md`, use the
decision recorded here. When it conflicts with code that has already been
implemented, do not silently rewrite the implementation: report the conflict and
recommend a migration. Explicit instructions from the user always take priority.

This guide refines the original design for two goals:

1. Ship a credible, bounded v1 rather than an over-broad autonomous-agent demo.
2. Maximize the engineering evidence visible to reviewers of the GitHub project.

---

## 1. Recommended product definition

CodeAgent is a human-supervised coding-agent runtime for Python repositories. It
explores a repository, produces an inspectable plan, applies a transactional
patch inside an isolated workspace, runs verification in a resource-constrained
container, and emits an auditable report. It may iterate on failures within
explicit cost, time, and tool-use budgets.

The portfolio differentiator is not raw benchmark performance. It is the quality
of the system around the model:

- deterministic orchestration;
- typed and bounded tools;
- explicit trust boundaries;
- transactional repository changes;
- reproducible verification;
- complete execution traces;
- honest, repeatable evaluation.

The project should be described as a **bounded coding-agent runtime and evaluation
system**, not as a general autonomous software engineer.

---

## 2. V1 boundaries

### In scope

- Python repositories
- One task and one repository per run
- CLI operation
- OpenAI Responses API through custom function tools
- A single agent controlled by a deterministic host state machine
- Docker-based test and command execution
- Plan approval before the first mutation
- Temporary Git worktree for all agent changes
- Structured local event trace and final report
- Synthetic benchmark tasks plus a small frozen real-issue evaluation set

### Out of scope

- IDE, web, or chat UI
- Multiple cooperating agents
- Multi-language repository support
- Distributed workers, queues, or databases
- Automatic pull-request creation
- Arbitrary deployment or network access
- Claims of general SWE-bench competitiveness
- Fully unsupervised operation as the default

Do not expand these boundaries merely because the model or a framework supports
additional features.

---

## 3. Decisions that are safe to take optimistically

These decisions are reversible, appropriately scoped, and should not block
implementation:

| Area | V1 decision |
|---|---|
| Language | Python 3.12+ |
| Package layout | `src/` layout with `pytest` |
| User interface | Synchronous CLI |
| Agent topology | One agent |
| State storage | Local JSONL events and generated Markdown/JSON reports |
| LLM provider | One OpenAI implementation behind a small interface |
| LLM protocol | Responses API with strict JSON schemas for function tools |
| Workspace | Temporary Git worktree created per run |
| Editing | A typed patch operation supporting add, update, delete, and rename |
| Execution | Structured argv, never a shell command string by default |
| Container networking | Disabled by default |
| Approval | Interactive by default |
| Benchmark development | Controlled synthetic fixtures first |
| CI | Fake deterministic model; no API key required |

Avoid speculative interfaces for hypothetical providers, databases, UIs, or
languages. Extract an interface only at a real nondeterministic or external
boundary: model client, workspace, process executor, approval provider, and event
sink.

---

## 4. Decisions that require the author's direct involvement

The author should make, review, and be able to defend these decisions rather than
accepting generated defaults without understanding them:

1. **Threat model:** identify trusted inputs, untrusted repository content,
   container escape concerns, secret exposure risks, and expected attacker
   capabilities.
2. **State-machine invariants:** define exactly which tools are legal in each
   state and which component may transition states.
3. **Policy semantics:** decide what process requests are rejected, why they are
   rejected, and how false positives are handled.
4. **Transaction semantics:** define when a patch is validated, applied,
   checkpointed, rejected, or rolled back.
5. **Success contract:** determine which tests constitute task completion and
   which regressions invalidate a result.
6. **Benchmark construction:** write or personally audit every task, hidden
   evaluator, reference fix, and contamination check.
7. **Failure analysis:** inspect unsuccessful traces and classify actual causes
   instead of treating aggregate pass rate as the whole result.
8. **Architectural records:** write the final rationale in the author's own words
   after implementation experience validates or changes the initial choice.

AI assistance is appropriate for boilerplate, fixtures, repetitive unit tests,
typing, and prose editing. The author should retain ownership of security and
correctness boundaries.

---

## 5. Revised architecture

```text
CLI
 │
 ▼
Run Controller ───────────────► Approval Provider
 │                                  │
 │ emits typed events               │ approve/reject
 ▼                                  ▼
Model Adapter ◄────────────── State/Tool Policy
 │                                  │
 │ typed tool request               │ validated capability
 ▼                                  ▼
Tool Dispatcher ─────────────► Transactional Workspace
 │                                  │
 │ structured process request       │ temporary Git worktree
 ▼                                  ▼
Process Policy ──────────────► Docker Executor
                                    │
                                    ▼
                                Verifier

Every component ─────────────► Append-only Event Sink
```

### Core rule

The model proposes actions. Host code decides whether they are valid and performs
all state transitions. The model must never be the authority for its own budget,
approval status, policy result, or verification outcome.

### Suggested modules

```text
src/codeagent/
  cli.py
  domain.py
  controller.py
  budgets.py
  events.py
  reporting.py
  approval.py
  llm/
    base.py
    fake.py
    openai.py
  tools/
    schemas.py
    dispatcher.py
    files.py
    patch.py
  workspace/
    git_worktree.py
  execution/
    policy.py
    docker.py
  verification/
    base.py
    pytest.py

tests/
  unit/
  integration/
  e2e/

benchmarks/
  cases/
  harness/
  reports/

docs/
  adr/
  threat-model.md
  failure-taxonomy.md
```

The names may change. Preserve the dependency direction: domain and controller
logic must not depend directly on Docker, the OpenAI SDK, or terminal rendering.

---

## 6. State-machine rules

Recommended states:

```text
INIT → BASELINE → EXPLORE → PLAN → APPROVAL → EXECUTE → VERIFY
                                  │              │          │
                                  └─ rejected ─► DONE       ├─ pass ─► DONE
                                                            └─ fail ─► EXPLORE
```

`BASELINE` is added to the original design. Before mutation, record repository
status and run the relevant test command when feasible. This distinguishes newly
introduced regressions from failures already present in the repository.

Required invariants:

- Read-only tools are available during `EXPLORE`.
- The plan must be structured data, not only prose.
- No mutation tool is callable before approval.
- Approval applies to a plan revision, not forever to the whole run.
- A materially changed plan requires renewed approval unless the configured
  approval mode explicitly waives it.
- Only the controller increments counters and transitions states.
- Verification is initiated by the controller, not left to model discretion.
- Every terminal state records an explicit reason.
- Every external operation emits start and completion/failure events.

Use exhaustive transition tests. An illegal state/tool combination should fail
closed and produce a trace event.

---

## 7. Tool design

### Read tools

- `list_directory(path, cursor?, limit?)`
- `read_file(path, start_line?, end_line?)`
- `search_text(query, path?, cursor?, limit?)`

All paths must be repository-relative after normalization. Reject absolute paths,
`..` traversal, symlink escapes, device files, and reads above a configured size.
Return line numbers, truncation status, and a continuation cursor where relevant.

### Mutation tool

Use one operation such as:

```python
apply_patch(operations: list[PatchOperation]) -> PatchResult
```

Supported operations may include:

- add file;
- update exact range or exact old text;
- delete file;
- rename file.

Validate the complete proposal before changing the worktree. Reject ambiguous
matches, path escapes, duplicate operations, binary files, oversized patches,
and unexpected current file hashes. Apply the proposal atomically and run Python
syntax validation before accepting the checkpoint.

### Process tool

Prefer:

```python
run_process(
    argv: list[str],
    cwd: str,
    timeout_seconds: int,
    environment: dict[str, str] | None = None,
) -> ProcessResult
```

Do not use `shell=True`. Do not accept pipelines, redirections, command
substitution, or compound shell syntax as a single string. Return structured
exit code, stdout, stderr, elapsed time, timeout status, and output-truncation
metadata.

The process allowlist protects the agent interface. It does not make repository
code safe: running `pytest` executes arbitrary project code. Docker isolation is
therefore a separate and mandatory security boundary.

### Planning

`propose_plan` should return a structure containing:

- problem hypothesis;
- evidence consulted;
- proposed file changes;
- intended verification;
- risks or uncertainties.

The controller records the plan and invokes the approval provider.

---

## 8. Workspace and sandbox safety

Never make agent changes directly in the user's checkout. Recommended lifecycle:

1. Inspect repository status and record the starting commit.
2. Create a disposable Git worktree in a task-specific temporary directory.
3. Mount only that worktree into the container.
4. Apply validated patches to the worktree.
5. Create an internal checkpoint after each accepted iteration.
6. Export a cumulative diff and report.
7. Remove the disposable worktree during cleanup, retaining trace artifacts.

Container defaults:

- network disabled;
- non-root user;
- no host Docker socket;
- no host secrets or broad home-directory mounts;
- read-only root filesystem where practical;
- writable workspace and temporary directory only;
- CPU, memory, process-count, and wall-time limits;
- explicit environment allowlist;
- output byte limits;
- forced cleanup after timeout or cancellation.

Tests must demonstrate enforcement rather than only inspect configuration. Include
attempted network access, filesystem escape, fork/process exhaustion, timeout,
large-output generation, and orphan-container cleanup.

---

## 9. Resolved open questions from the design specification

### 9.1 Editing tests

Allow the agent to create or strengthen ordinary repository tests when the plan
explicitly identifies those changes. Highlight all test-file modifications and
require approval in the default mode. Benchmark evaluator tests must live outside
the writable agent workspace and remain hidden from the model.

Deleting tests, weakening assertions, adding unconditional skips, or changing
benchmark configuration should be rejected or escalated for explicit approval.

### 9.2 Initial budgets

Use configurable starting defaults:

- maximum repair iterations: 3;
- maximum model-requested tool calls per iteration: 25;
- total wall-clock time: 15 minutes;
- process timeout: 120 seconds;
- captured output: 64 KiB per stream per process;
- file read: 256 KiB per call;
- explicit total input/output token and monetary limits.

These are starting hypotheses, not universal constants. Record actual usage in
benchmark results and tune from observed distributions. A budget stop must report
which cap was reached and at which state.

### 9.3 Creating new files

Support new files through the patch operation. Do not expose a general full-file
overwrite tool for existing files.

### 9.4 Multi-file changes

Validate and apply every patch proposal atomically. Retain an accepted checkpoint
between repair iterations so later attempts can refine earlier changes. Never
leave a partially applied proposal after validation or application failure.

### 9.5 Benchmark design

Use two separately reported suites:

1. Eight to ten synthetic tasks for controlled development and regression tests.
2. Three to five frozen real issues from small Python repositories for external
   credibility.

Do not merge the suites into one headline score. Freeze repository revisions,
dependencies, task statements, evaluator versions, and reference fixes.

### 9.6 Approval UX

Support:

- `interactive`: show plan and wait for confirmation;
- `plan-file`: write a plan artifact and require an explicit follow-up command;
- `none`: non-interactive mode for controlled CI/benchmark use.

Use professional terminology such as `--approval=none`; avoid naming the mode
“yolo.” Always record approval mode in the trace.

### 9.7 Definition of success

A successful task must satisfy all of the following:

- required fail-to-pass tests now pass;
- previously passing tests selected by the evaluator still pass;
- verification actually ran and completed;
- the final patch is syntactically valid;
- no unresolved policy or sandbox violation occurred;
- the workspace diff is available and within configured hard limits.

Report but do not initially hard-fail on diff size, files changed, iterations,
test-file changes, cost, token consumption, and latency. These become quality
metrics and possible future gates.

---

## 10. Event model and observability

Use an append-only sequence of typed events as the source of truth. At minimum:

```text
RunStarted
BaselineRecorded
StateTransitioned
ModelRequestStarted
ModelResponseReceived
ToolRequested
PolicyDecisionRecorded
ToolCompleted
PlanProposed
ApprovalRecorded
PatchApplied
CheckpointCreated
VerificationCompleted
BudgetExceeded
RunFinished
```

Each event should include a run ID, monotonic sequence number, timestamp, state,
iteration, schema version, and event-specific payload. Redact secrets before
persistence. Preserve raw model response identifiers and token-usage metadata
where appropriate, but keep the normalized local event format independent of one
provider.

Generate both machine-readable JSON and a human-readable Markdown report from the
same events. A run should be explainable without reading application logs.

---

## 11. Evaluation strategy

### Per-task ground truth

Each benchmark case should include:

- frozen source revision;
- task statement;
- setup command or image identifier;
- public tests visible to the agent, if any;
- hidden evaluator tests unavailable to the agent;
- reference patch;
- expected fail-to-pass and pass-to-pass test sets;
- resource budget;
- tags describing bug type and difficulty.

Validate every benchmark case without an LLM:

1. The broken revision fails the intended evaluator.
2. The reference patch passes it.
3. The task is solvable using the v1 tools and environment.

### Metrics

Record:

- task success rate;
- pass rate by bug category;
- mean and percentile cost;
- token usage;
- wall-clock latency;
- tool calls and repair iterations;
- policy rejections;
- invalid-patch rate;
- regression rate;
- test-modification rate;
- run-to-run variance.

Run the evaluation multiple times with the same model snapshot, prompt version,
tool version, and environment. Report the number of trials and raw per-task
results, not only an aggregate percentage.

### Baselines and ablations

Include at least one baseline and one ablation:

- baseline: one-shot patch generation with the same model and evaluator;
- ablation: bounded custom tools versus a broader process interface;
- possible later ablation: approval/plan stage present versus absent;
- possible later ablation: syntax guard enabled versus disabled.

The goal is to show what the harness contributes independently of model choice.

### Failure taxonomy

Classify failures such as:

- repository exploration missed relevant code;
- incorrect problem hypothesis;
- malformed or stale patch;
- correct fix but incomplete regression coverage;
- environment or dependency failure;
- budget exhaustion;
- policy false positive;
- verification misconfiguration;
- model/API failure;
- benchmark defect.

Publish representative failed traces. Honest limitations strengthen this project.

---

## 12. Revised implementation sequence

### Milestone 0 — Contracts and threats

Build domain types, state transition table, event schemas, error taxonomy, threat
model, and architecture-decision records for the most consequential choices.

Verify with exhaustive transition tests and review every trust boundary.

### Milestone 1 — Thin vertical slice

Using a deterministic fake model and a real fixture repository, perform a full
read-plan-approve-patch-verify-report flow. Use a temporary worktree from the
start. Docker may initially run only a fixed verification command.

This milestone replaces an extended all-stub phase.

### Milestone 2 — Robust repository tools

Implement normalized bounded reads, paginated search, atomic patch validation,
syntax checks, diff generation, checkpoints, and rollback tests.

### Milestone 3 — Hardened execution

Implement structured process requests, process policy, Docker lifecycle, resource
limits, network isolation, redaction, cancellation, and cleanup. Add adversarial
integration tests.

### Milestone 4 — Real model loop

Integrate the OpenAI Responses API through strict custom function schemas. Handle
multiple tool calls, malformed arguments, incomplete responses, retries, API
errors, usage accounting, and custom-tool budgets.

Run one real end-to-end task and preserve the trace.

### Milestone 5 — Repair loop and verification contract

Implement baseline recording, failure feedback, plan revision, reapproval rules,
iteration checkpoints, regression detection, and every terminal outcome.

### Milestone 6 — Benchmark harness

Build the synthetic suite, hidden evaluators, deterministic environment setup,
raw result storage, repeated trials, metric aggregation, and failure taxonomy.
Then add the frozen real-issue suite.

### Milestone 7 — Portfolio release

Publish the CLI, architecture diagram, threat model, ADRs, benchmark methodology,
raw results, successful and failed traces, a short recorded demo, and an honest
retrospective describing at least one design mistake discovered during the build.

---

## 13. Testing expectations

### Unit tests

- transition legality and terminal reasons;
- budget accounting;
- path normalization and symlink escape rejection;
- patch validation and atomic failure;
- process-policy decisions;
- event serialization and redaction;
- verifier result classification.

### Integration tests

- Git worktree creation, checkpointing, diff export, and cleanup;
- container resource and network enforcement;
- timeout and cancellation cleanup;
- process output truncation;
- test baseline and regression detection.

### End-to-end tests

- deterministic fake model solves a fixture task in CI;
- approval rejection leaves the source repository unchanged;
- budget exhaustion produces the correct report;
- invalid model tool requests fail closed;
- one opt-in real-API smoke test is available but not required in ordinary CI.

Tests should assert behavior, not implementation details. Security configuration
must be proven by attempted violations.

---

## 14. Portfolio deliverables

The repository should contain evidence a reviewer can inspect quickly:

1. A README opening with a reproducible command and measured result.
2. An architecture diagram with trust boundaries.
3. Five or six concise ADRs for consequential choices.
4. A threat model and adversarial test matrix.
5. One complete successful trace and at least one failed trace.
6. Raw benchmark results plus the aggregation script.
7. Cost, latency, variance, and regression metrics alongside pass rate.
8. A baseline and one controlled ablation.
9. A two-minute terminal demo.
10. A retrospective describing a real implementation error and what changed.

Avoid leading with market-size, funding, or vague capability claims. Lead with a
reproducible result and explain the engineering decisions that made it possible.

---

## 15. Definition of v1 complete

V1 is complete when all of the following are true:

- a new user can install and run the CLI from documented commands;
- the original repository is never modified during an agent run;
- the approval gate demonstrably prevents preapproval mutation;
- all model-requested processes are policy checked and traced;
- sandbox limits are validated by adversarial tests;
- a real task is solved end to end and its trace is published;
- at least eight synthetic and three frozen real tasks are evaluated;
- results from multiple trials include cost, latency, and variance;
- CI passes without an API key by using the deterministic fake model;
- limitations and unsuccessful cases are documented honestly;
- the author can explain the controller, workspace, policy, sandbox, verifier, and
  evaluation methodology without relying on generated prose.

Anything beyond this should be justified by evidence from actual v1 failures or
user needs.

---

## 16. Guidance for an implementing LLM

Before each implementation request:

1. Inspect the current repository, tests, Git status, and relevant ADRs.
2. State which milestone and acceptance criterion the requested work advances.
3. Preserve existing user changes and avoid unrelated refactors.
4. Prefer the smallest vertical implementation that can be demonstrated.
5. Add failure-path tests, not only happy-path tests.
6. Do not weaken security boundaries to make an end-to-end demo pass.
7. Do not claim success based on plausible output; run the relevant verification.
8. Record new consequential decisions or deviations as proposed ADR updates.
9. Surface conflicts or missing author decisions instead of silently guessing.
10. End with changed files, verification performed, remaining risks, and the next
    smallest useful step.

The implementing LLM may suggest improvements, but scope expansion requires the
user's approval.
