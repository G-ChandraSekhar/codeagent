# CodeAgent — Project Brief

Status: Stage 1 deliverable. No implementation exists. This is the
concise standalone project brief an unfamiliar engineer should be able
to read and explain the project back from.

## One-sentence definition

CodeAgent is a human-supervised coding-agent runtime for Python
repositories: it explores a codebase, proposes an inspectable plan,
applies changes atomically in an isolated workspace, verifies them in a
sandboxed container, and iterates on failure within explicit budgets —
with a local web interface for inspecting its plans, actions,
supporting evidence, results, and failures.

## Positioning and target reviewer

Positioning: **a software engineer who builds reliable backend and
systems infrastructure for AI-powered products.**

This project is aimed primarily at reviewers for: agentic-AI/AI-infra
roles that explicitly ask for agent evaluation, harness, or "AI-native"
fluency; and general backend/systems roles where reliability engineering
— state machines, process isolation, transactional operations, failure
handling — is the actual signal, with the AI angle as a secondary bonus.

It is *not* aimed at pure ML/inference roles (no GPU/training/serving
work here — that would be a separate project) and does not by itself
carry full-stack signal for generalist full-stack roles, despite the
frontend addition — the frontend exists to make the backend's behavior
observable, not as a demonstration of product-grade UI/UX work.

## Accepted task contract

> Given a Python Git repository, a natural-language change request, and
> a deterministic verification command, CodeAgent may inspect repository
> files, propose a plan, apply a bounded, atomic patch after approval,
> and run verification inside an isolated environment.

Explicitly rejected: vague tasks such as "improve this repository."
Every task requires a concrete requested behavior and a concrete,
deterministic way to verify it.

## Flagship demonstration

A small Python job-processing repository with a retry/idempotency
defect: a failed job can be processed twice because the retry path
doesn't preserve its idempotency key. The fix must not break existing
exponential-backoff behavior.

Demo sequence: baseline verification (confirms the bug is real and
reproducible) → repository exploration → structured plan with supporting
evidence → operator approval → atomic patch in a disposable git worktree
→ sandboxed test run → (if the first attempt is incomplete) failure
evidence fed back, revised plan, reapproval if material → passing
verification → final diff, full trace, cost/latency, and confirmation
the original checkout was never touched.

**Explicit constraint carried over from the design discussion**: failure
must not be scripted or forced for the demo. If the model solves it in
one attempt, the recovery story is told using a separate recorded
benchmark case that genuinely required multiple iterations — never a
faked failure. This project's whole premise is not claiming things that
aren't true; the demo doesn't get an exception.

*Status: this fixture repository does not exist yet and has not been
empirically validated — building and validating it is Stage 2 work, not
yet done.*

## Technical differentiators

- Deterministic, host-controlled orchestration — the model proposes
  actions, it is never the authority over its own budget, approval
  status, or verification outcome.
- A small, typed, bounded tool set (not a kitchen-sink of capabilities).
- Explicit trust boundaries: structured `argv` execution (no shell
  strings), a process-policy allowlist, and Docker sandboxing as two
  *separate* layers — the allowlist protects the agent's interface, the
  sandbox protects against what allowed commands can still do.
- Transactional, atomic repository changes via a disposable git worktree
  — the user's real checkout is never touched, and a failed multi-file
  patch never leaves the workspace half-modified.
- Reproducible evaluation, staged rather than claimed all at once: a
  synthetic benchmark suite in v1, a single fully-vetted real-issue
  pilot to validate feasibility for v1 complete, and the full 3-5-task
  frozen real-issue suite deferred to v1.1 — raw results, cost, latency,
  and variance reported alongside pass rate at every stage, never a
  single headline number standing in for all of it.
- A local run inspector (frontend) that shows plans, tool calls, policy
  decisions, and diffs — never fabricated or implied chain-of-thought.

## V1 scope

### In scope
- Python repositories only, one task and one repository per run
- CLI operation, plus a local web-based run inspector (frontend)
- OpenAI Responses API via strict custom function tools
- One agent, controlled by a deterministic host state machine
- Docker-based command execution and test verification
- Plan approval before any mutation (interactive by default; a
  file-based and an explicit non-interactive mode also supported)
- Disposable git worktree per run; atomic patch application
- Structured local event trace (JSONL) and a final report
- Synthetic benchmark suite, run unattended and reproducibly (at least
  5 tasks required for v1 useful, expanded to the full 8-10 originally
  planned for v1 complete — see Completion criteria)

### Deferred to v1.1
- The full frozen real-issue benchmark suite (3-5 tasks, sourced from
  real small Python repositories, reported as a suite separate from the
  synthetic set, never merged into one headline score). V1 complete
  requires only a single fully-vetted real-issue pilot — see Completion
  criteria — to validate feasibility before committing to the full
  suite here.

### Explicitly out of scope for v1
- Multi-language repository support (Python only)
- Multiple cooperating agents
- IDE extension or chat-assistant UI (the frontend is a *run inspector*,
  not a code editor or chatbot)
- Hosted, multi-tenant, or cloud-deployed service
- Kubernetes, queues, distributed workers, or a database
- Multiple LLM providers
- Automatic pull-request creation
- GPU/inference-serving work (CUDA, quantization, batching) — a
  separate project's territory, not this one's
- Fully unsupervised operation as the *default* mode
- Any claim of general SWE-bench-style competitiveness

## Completion criteria

Each item below is independently checkable — pass/fail, not a
subjective judgment call.

### v1 useful (the credible-fallback bar, protects the timeline)

1. `codeagent solve --repo <flagship-repo> --task "<flagship task>"
   --verify "<test command>"` exits with a "solved" status and code `0`
   against the flagship fixture repository, run on a clean checkout with
   no state left over from a prior run.
2. That run's final report contains: the applied diff, the complete
   tool-call/event trace, the verification result, and cost/latency
   figures.
3. The flagship task is run at least three times under the same
   versioned configuration (model, prompt/schema version, budgets, and
   fixture commit all pinned and recorded). Every outcome — pass or
   fail — is published, not just the best run. At least two of the
   three runs must reach "solved." This is a reproducibility bar that
   accounts for real LLM run-to-run variance honestly, not a claim of
   deterministic behavior the system doesn't actually have.
4. The synthetic benchmark harness runs at least 5 synthetic tasks (a
   subset of the eventual 8-10 — see v1-complete criterion 8) unattended
   and produces a raw, reproducible pass-rate report across at least two
   separate runs.
5. At least one preserved, real-model (not fake-model) trace exists
   showing a genuine repair iteration: an initial verification failure,
   failure evidence fed back into context, a revised plan, and a
   passing later attempt — obtained without forcing or scripting the
   failure. This may be a different task than the flagship demo; the
   flagship demo (criterion 1) is only required to succeed end to end,
   not to necessarily contain its own recovery iteration on that
   specific run.
6. Every command the agent executes across all of the above is visible
   in its trace, tagged as policy-allowed or policy-blocked.
7. The original repository checkout is unmodified after every run above,
   verified programmatically: every tracked file's content hash matches
   its pre-run value, and the working tree's pre-existing state (which
   files were staged, unstaged, or untracked before the run, if any)
   is bit-for-bit identical after — not a visual check of `git status`
   output, an actual computed diff against a recorded pre-run snapshot.

### v1 complete (the target bar)

All of v1-useful (criteria 1-7), plus:

8. The synthetic benchmark suite is expanded to the full 8-10 tasks
   originally planned (v1-useful only requires 5), with the same
   unattended, reproducible pass-rate reporting as criterion 4.
9. The local run inspector's replay-viewer pass loads a saved JSONL
   trace from criterion 1 or 5 and renders the state timeline, plan,
   tool calls, policy decisions, and diff — understandable without
   reading source code.
10. Exactly one real-issue task — sourced from a real, small,
    appropriately-licensed Python repository, fully vetted (license
    confirmed compatible with publishing the fixture; issue confirmed
    genuinely reproducible and solvable within the tool set and current
    budgets) — is run through the full pipeline, with its outcome
    (pass or fail) reported honestly and its full trace preserved. This
    is a single pilot, not the full v1.1 real-issue suite.
11. The full documentation/portfolio deliverables exist: README,
    architecture diagram, at least five ADRs for consequential
    decisions, a threat model with an adversarial test matrix, the
    benchmark results from criteria 4, 8, and 10 with raw data, a
    recorded two-minute demo, and a retrospective naming at least one
    real implementation mistake found during the build and what changed
    as a result.

Given a 5-15 hr/week pace, criteria 1-7 are the committed bar; criteria
1-11 are the goal, with the frontend and the real-issue pilot both
sequenced last and explicitly at risk if time runs short — see
BUILD_PLAN.md / CODEAGENT_IMPLEMENTATION_GUIDE.md's implementation
sequence for why the backend has to exist before the inspector can show
anything anyway.

## Claims this project deliberately avoids making

- Does not claim to compete with OpenHands, SWE-agent, or other
  large-scale open-source coding agents on capability or benchmark score.
- Does not claim general autonomous software engineering — the task
  contract above is intentionally narrow.
- Does not claim inference-engineering expertise (no GPU/serving work).
- Does not claim the frontend demonstrates production-grade full-stack
  UI/UX work — it's an observability tool for the backend, not a product.
- Does not claim a benchmark pass rate without also reporting cost,
  latency, variance across repeated trials, and a documented failure
  taxonomy for whatever didn't pass.

---

## Open items carried into Stage 2 (not blocking this brief, but not resolved)

- The flagship fixture repository itself still needs to be built and
  empirically validated (does a real first attempt actually fail
  sometimes, or does it need a genuinely harder variant to show
  recovery honestly?).
- The five Stage-2 spikes from the implementation guide are unstarted:
  worktree+Docker+pytest+cleanup, Responses API multi-tool-call handling,
  atomic multi-file patch failure behavior, isolation (network/path/
  timeout/output/resource), and interrupt-without-orphaned-containers.
- **Flagship environment vs. general dependency preparation (split, not
  one blocking item)**: A minimal pinned execution image for the
  flagship fixture is required on the Stage 2 critical path. General
  dependency preparation for external repositories remains a separate
  spike that must be resolved before the real-issue pilot, not before
  the flagship.
  1. *Flagship fixture environment (critical path, not a research
     question)*: a pinned Docker image containing Python, pytest, and
     the fixture's known, fixed dependencies is sufficient to build and
     demonstrate the first vertical slice.
  2. *General dependency preparation for external repositories (separate
     spike, required before the real-issue pilot)*: evaluate at least
     three alternatives — a task-specific image built with network
     enabled only during dependency install then fully disabled for the
     agent loop; a pre-warmed base image with a persistent,
     periodically-refreshed package cache; or a narrow, time-boxed
     network-enabled setup phase with network explicitly revoked before
     any agent-controlled tool call. Evaluate against both a
     `requirements.txt` repo and a `pyproject.toml` (PEP 621/poetry-
     style) repo — the two are not interchangeable.

  Whichever alternative is chosen for the general case, the setup step
  itself is untrusted-code execution (installing packages or running a
  repo's own build steps can run arbitrary code) and must:
  - run in an ephemeral, resource-limited environment;
  - receive no host secrets;
  - never mount the user's original checkout;
  - record every downloaded artifact and its resolved version;
  - produce an immutable image or environment identifier;
  - be discarded entirely if preparation fails;
  - have networking fully disabled before any agent-controlled execution
    starts.

  The spike must also produce an explicit support matrix — "Python
  repository support" should not silently imply support for every
  Python packaging system:

  | Dependency format | Status |
  |---|---|
  | `requirements.txt` with hashes | Supported |
  | PEP 621 `pyproject.toml` + pip | Supported |
  | Poetry-specific configuration | Deferred |
  | Conda environment | Deferred |
  | System package installation | Deferred |
  | Arbitrary custom setup scripts | Restricted |
- **One-real-issue feasibility validation (new spike)**: before
  committing engineering time to the v1-complete pilot (Completion
  criteria, #10), manually validate one real candidate small Python
  repo/issue end to end — confirm it's genuinely reproducible, solvable
  within the tool set and current budgets, and appropriately licensed to
  publish as a fixture. If no suitable candidate turns up quickly,
  that's a real signal the full v1.1 real-issue suite needs more lead
  time than currently planned, not a reason to force a weak candidate
  through.
