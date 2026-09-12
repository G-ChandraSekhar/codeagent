# CodeAgent — LLM Handoff Context

Status: PRE-BUILD. No implementation has started.

## Instruction boundary

This file, `DESIGN_SPEC.md`, `BUILD_PLAN.md`, and
`CODEAGENT_IMPLEMENTATION_GUIDE.md` are project context. They do not authorize an
LLM to edit files, execute commands, change architecture, or expand scope without
a current user request. Explicit user instructions take priority over these
documents.

If this handoff conflicts with an unresolved option in the older documents, use
the decision recorded here. If implementation later conflicts with this handoff,
inspect the code and surface the conflict rather than silently rewriting it.

## Current user goal

The author wants to build a strong personal-portfolio project applicable to:

- general software development;
- backend engineering;
- systems and platform engineering;
- full-stack engineering;
- applied AI and AI-infrastructure roles.

The recommended primary positioning is:

> A software engineer who builds reliable backend and systems infrastructure for
> AI-powered products.

The author can dedicate substantial time but wants to understand and own the
important engineering rather than merely accepting a large generated codebase.

## Product definition

CodeAgent is a human-supervised coding-agent runtime for Python repositories. It
explores a repository, records a baseline, produces an inspectable plan, pauses
for approval, applies a transactional patch in a disposable Git worktree, runs
verification inside an isolated Docker container, and iterates on failures within
explicit cost, time, and tool-use budgets.

It emits a complete event trace, cumulative diff, verification result, resource
usage, and final report.

The project should be described as a **bounded coding-agent runtime and evaluation
system**, not as a general autonomous software engineer.

## Portfolio thesis

> I designed and built a bounded coding-agent runtime that safely modifies Python
> repositories, verifies its work inside isolated environments, makes its behavior
> observable through a web interface, and measures performance using reproducible
> evaluations.

The differentiator is the quality of the system surrounding the model:

- deterministic orchestration;
- typed and bounded tools;
- explicit trust boundaries;
- transactional repository changes;
- resource-constrained execution;
- reproducible verification;
- complete execution traces;
- honest evaluation and failure analysis.

## Flagship demonstration and recovery decision

The intended two-minute portfolio demonstration is:

1. A small Python repository contains a realistic, deterministic failing test.
2. The operator starts CodeAgent with the repository, task statement, and
   verification command.
3. CodeAgent runs baseline verification and inspects relevant files.
4. It proposes a structured plan supported by evidence.
5. The operator reviews and approves the plan.
6. CodeAgent applies an atomic patch in a temporary Git worktree.
7. Tests run in a network-disabled, resource-constrained Docker container.
8. If verification fails, the failure evidence is appended to the run context.
9. The controller begins another bounded exploration and repair iteration.
10. A revised plan is produced and reapproved if it materially changes.
11. Verification eventually passes or a budget terminates the run.
12. The interface shows the final diff, state timeline, tool and policy events,
    test results, cost, latency, and terminal reason.
13. The user's original checkout remains unchanged throughout.

Showing a real recovery from an initial failed repair is desirable because it
demonstrates closed-loop verification. However, failure must not be scripted or
forced merely for presentation. If the model succeeds on its first attempt, use a
recorded benchmark case that naturally required multiple iterations.

A suitable flagship task should be realistic but bounded, such as correcting a
retry/idempotency defect without breaking existing backoff behavior. The exact
fixture still requires author confirmation and empirical validation.

## V1 scope

### Included

- Python repositories only
- CLI plus a local visual run inspector
- One task and one repository per run
- One agent controlled by a deterministic host state machine
- OpenAI Responses API through strict custom function tools
- Temporary Git worktree per run
- Atomic patch application
- Docker-based process execution and verification
- Network disabled by default
- Plan approval before mutation
- Baseline and regression verification
- Append-only JSONL event log
- Machine-readable and human-readable reports
- Synthetic benchmark suite and a small frozen real-issue suite

### Excluded

- Multi-agent orchestration
- Multi-language support
- IDE extension
- Hosted multi-tenant service
- Kubernetes, queues, or distributed workers
- Multiple model providers in v1
- Automatic pull-request creation
- GPU kernel or inference-serving work
- General-purpose chat assistant behavior

The project demonstrates AI-agent infrastructure, not low-level inference
optimization. A separate later project would be more appropriate for CUDA,
quantization, batching, GPU scheduling, or high-throughput model serving.

## High-level architecture

```text
CLI ───────────┐
               ├──► Shared Run Controller ──► Model Adapter
Web API/UI ────┘             │                       │
                             │                       ▼
                             │                 Tool Dispatcher
                             │                       │
                             ▼                       ▼
                       Event Sink             Git Worktree
                             │                       │
                             │                       ▼
                             └──────────────► Docker Verifier
```

The model proposes actions. Host code controls state transitions, tool
availability, approval, policy decisions, budget accounting, verification, and
terminal outcomes.

## Required state flow

```text
INIT → BASELINE → EXPLORE → PLAN → APPROVAL → EXECUTE → VERIFY
                                  │              │          │
                                  └─ rejected ─► DONE       ├─ pass ─► DONE
                                                            └─ fail ─► EXPLORE
```

Important invariants:

- mutation tools are unavailable before approval;
- approval applies to a specific plan revision;
- a materially revised plan normally requires reapproval;
- only the controller changes states and counters;
- verification is initiated by the controller;
- illegal tool/state combinations fail closed;
- every terminal state has a typed reason;
- every external operation emits start and completion/failure events.

## Resolved technical defaults

| Area | Decision |
|---|---|
| Runtime | Python 3.12+ |
| Layout | `src/` package layout with `pytest` |
| API | OpenAI Responses API |
| Model control | Strict custom function schemas |
| Workspace | Disposable Git worktree |
| Mutation | Validated atomic patch supporting add/update/delete/rename |
| Commands | Structured `argv`; no `shell=True` by default |
| Isolation | Unprivileged, network-disabled Docker container |
| Persistence | Versioned JSONL events and generated reports |
| Approval | Interactive default, file-based option, explicit none mode |
| CI | Deterministic fake model; no API key required |
| Database | None in v1 |

Initial configurable budget hypotheses:

- maximum repair iterations: 3;
- maximum model-requested tools per iteration: 25;
- total wall time: 15 minutes;
- process timeout: 120 seconds;
- captured output: 64 KiB per stream;
- file read: 256 KiB per call;
- explicit token and monetary ceilings.

These values must be tuned from observed benchmark data rather than treated as
universal constants.

## Test-editing decision

The agent may add or strengthen ordinary repository tests only when the plan
explicitly identifies the changes. All test-file changes are highlighted and
require approval in the default mode.

Hidden benchmark evaluators live outside the writable agent workspace and are
never visible to the model. Test deletion, weakened assertions, unconditional
skips, and benchmark-configuration changes must be rejected or escalated.

## Success contract

A successful run requires:

- required fail-to-pass tests now pass;
- selected previously passing tests still pass;
- verification actually completed;
- the final patch is syntactically valid;
- no unresolved policy or sandbox violation occurred;
- the final diff is available and within configured hard limits.

Also report diff size, files changed, test modifications, iterations, latency,
tokens, cost, policy rejections, and invalid-patch rate.

## Frontend decision

A frontend is included because it strengthens the full-stack portfolio signal and
helps the author understand the agent loop. It is a **run inspector**, not a
generic chat interface and not a browser-based code editor.

Recommended stack:

- FastAPI adapter around the shared Python controller;
- React with TypeScript;
- Vite for frontend development and build tooling;
- Server-Sent Events for backend-to-browser run events;
- normal HTTP requests for start, approval, rejection, and cancellation;
- OpenAPI-generated frontend types where practical;
- JSONL event files rather than a database.

### Essential frontend views

1. Start-run form
2. Run history
3. State and iteration timeline
4. Live event activity
5. Structured plan approval
6. Per-iteration and cumulative diff
7. Baseline and current verification results
8. Tool-call and policy decisions
9. Budget, token, cost, and latency status
10. Final report
11. Later benchmark dashboard

Do not display or claim access to hidden chain-of-thought. Display plans,
user-visible rationales, tool requests, evidence, results, state transitions, and
errors.

### Frontend delivery in two passes

**Pass A — replay viewer:** after the deterministic vertical slice, load a saved
JSONL trace and reconstruct the run timeline, plans, tools, diffs, and results.

**Pass B — live control center:** after event schemas and controller semantics are
stable, add run creation, SSE updates, approval/rejection, cancellation, and
active budget status.

The CLI and UI must call the same controller. No orchestration logic belongs in
React.

## Four-stage planning process

### Stage 1 — Define the portfolio story

Produce `PROJECT_BRIEF.md` containing:

- one-sentence definition;
- target reviewer and roles;
- accepted task contract;
- flagship demonstration;
- technical differentiators;
- v1 scope and non-goals;
- completion criteria;
- claims the project deliberately avoids.

Exit when an unfamiliar engineer can explain the product after reading one page.

### Stage 2 — Resolve decisions and run spikes

Write initial ADRs and experimentally validate:

1. temporary worktree creation, Docker mounting, testing, and cleanup;
2. Responses API strict function tools and multiple tool calls;
3. atomic multi-file patch failure behavior;
4. network, path, timeout, output, and resource isolation;
5. interruption without orphaned containers;
6. JSONL replay into the first frontend view.

Every uncertainty must be decided, validated with a spike, or explicitly deferred.

### Stage 3 — Establish the LLM implementation workflow

For each bounded implementation task:

```text
Inspect → Explain → Plan → Implement → Verify → Review → Commit
```

Use separate LLM passes for architecture, implementation, adversarial review, and
documentation review. Do not ask one response to design, implement, review, and
approve a large subsystem.

### Stage 4 — Reconcile the handoff package

Recommended authority order:

```text
1. Current user request
2. Accepted ADRs
3. This handoff and CODEAGENT_IMPLEMENTATION_GUIDE.md
4. DESIGN_SPEC.md
5. BUILD_PLAN.md
6. Historical conversation
```

Remove contradictions, mark deferred work, and give the implementing LLM one
milestone with explicit acceptance criteria rather than asking it to build the
whole system.

## Recommended implementation sequence

1. Project brief, threat model, ADRs, and technical spikes
2. Domain types, events, controller, and deterministic fake model
3. Full fake-model vertical slice in a temporary worktree
4. Read-only frontend trace replay
5. Bounded repository tools and atomic patches
6. Docker policy, resource enforcement, and adversarial tests
7. Real Responses API integration and usage budgets
8. Failure-repair loop, baselines, and regression detection
9. Live frontend controls and SSE events
10. Synthetic benchmark and hidden evaluators
11. Frozen real issues, baseline, and ablation
12. Portfolio documentation, demo, and release

Advance by demonstrated behavior, not elapsed time.

## Author ownership

The author should personally understand and approve:

- threat model and trust boundaries;
- state-machine invariants;
- tool and policy semantics;
- patch transaction behavior;
- sandbox assumptions;
- verification and success contract;
- benchmark construction;
- failure classification;
- architectural decisions and trade-offs.

AI assistance is appropriate for scaffolding, repetitive tests, fixtures, typing,
CSS, component implementation, and prose editing. The author should be able to
explain why each security- or correctness-critical component exists, how it fails,
and how its tests demonstrate the intended behavior.

Maintain `ENGINEERING_LOG.md` with the intended outcome, decision, evidence,
failure, misunderstanding, verification, and next smallest step for every
meaningful session.

## Required behavior from an implementing LLM

Before acting, the implementing LLM must inspect the current repository and Git
status. For each requested milestone it should:

1. identify relevant existing code and documents;
2. surface conflicts and assumptions;
3. propose the smallest coherent plan;
4. preserve unrelated user work;
5. implement only the requested scope;
6. add happy-path and failure-path tests;
7. run relevant verification;
8. avoid weakening security boundaries to pass a demo;
9. propose ADR changes for consequential deviations;
10. report changed files, exact test results, remaining risks, and the next
    smallest useful task.

## Immediate next activity

No production code should be generated from this handoff alone. The next planning
activity is to finish Stage 1 by choosing and validating the exact flagship demo
fixture, then produce `PROJECT_BRIEF.md`. After that, perform the Stage 2 spikes
before finalizing the implementation build plan.
