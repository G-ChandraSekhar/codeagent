# CodeAgent — v1 Completion Ledger

Status: **PROPOSED, revision 2, for joint review** (2026-10-03). Audited at
`main` = `0f8aa6b314e6cb750c279889a940a2be7525509b` (Linux CI run
[37158716812](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37158716812),
success).

Revision 2 applies the joint-review decisions J-1 to J-11 (§1.3) and, in its
precision pass, the resolutions of C-1 to C-7 (§1.5). This
document records evidence and plans only. It changes no ADR, threat-model
entry or accepted decision.

Once approved, this ledger is the single source of truth for **what remains
before v1**. ADRs remain the source for *how* each mechanism works;
`CLAUDE.md` and `ENGINEERING_LOG.md` remain the history.

Labels used throughout:
- **[code]** verified by reading the cited source;
- **[test]** a named test exists (not rerun for this audit);
- **[CI]** confirmed in Linux CI;
- **[absent]** confirmed absent by repository search;
- **[inference]** judgment, not proof.

---

## 0. Executive summary

| Item | Assessment |
|---|---|
| Current milestone | **Milestone 3 (hardened execution), late.** Milestones 2 and 5 are partial; Milestones 4, 6 and 7 are not started (§5). |
| Milestone 3 substrate progress | **~65–80%** [inference]. This measures the lifecycle/executor *substrate* only and **must not be read as product progress**. |
| Total product completion | **~25–35%** [inference]. **None** of the 11 frozen v1 criteria (§1.2) has objective evidence through the operator path; several are partially supported by internal mechanisms. |
| Strongest completed capability | **Mechanism-verified** resource lifecycle safety: a real worktree, an owned checkpoint ref, and a lifecycle-labelled Docker verifier with durable write-ahead state. Automatic reconciliation covers the documented single-resource and worktree→ref crash shapes, proven with real Git, Docker and SIGKILL tests in Linux CI. It is reachable only through test-called internal code. |
| Largest missing product capability | **No model drives a run.** There is no model adapter [absent], no multi-tool loop, and no list/search tools. The patch applied is **supplied by the caller at construction** (`_lifecycle_run.run_lifecycle_aware(patch_operations=...)`), not proposed by a model. |
| Next release-blocking dependency | ADR 0004 §11 abandonment with the narrow `codeagent reconcile` maintenance command. It is the first item of the Amendment 14 operator-wiring gate. |
| Remaining effort, frozen scope | **~274–554 focused hours to public v1** (§7). These are planning ranges, not commitments. |
| Readiness | **Not operator-ready.** There is no CLI or console script [absent]. The only end-to-end path, `_lifecycle_run.run_lifecycle_aware`, is imported by no production module (verified call graph, §2.1). |

**Gates, which must not be conflated:**

- **Gate O — operator-wiring gate** (ADR 0004 Amendment 14, preserved, J-5).
  No CLI or operator path may run the lifecycle-aware composition until all of
  the following exist:
  - a dead-run checkpoint-ref row (**done**, Amendment 16);
  - a worktree-plus-container row (**not done**);
  - abandonment (**not done**);
  - ADR 0005 cancellation (**not done**).

  The `codeagent reconcile` maintenance command (D1) is the recovery
  prerequisite itself and never runs the composition.
- **Gate α — internal alpha**: brief criteria 1–7 met with objective evidence.
  This is **not** a release and is never published as "v1".
- **Gate v1 — public v1**: **all** brief criteria 1–11 (§1.2) met, each with
  objective evidence recorded in §1.2's evidence column. Public v1 is not
  declared before that.

---

## 1. Frozen v1 definition

### 1.1 Sources and authority

- **Authority order:** the current user request (including J-1 to J-11),
  then accepted ADRs, then the handoff and the guide, then the spec, then the
  build plan (`CLAUDE.md`; handoff "Stage 4").
- **Scope baseline:** `docs/PROJECT_BRIEF.md` is the frozen Stage-1 scope
  baseline.
- **J-1 overrides the brief's tiering.** The brief calls criteria 1–7 "the
  committed bar" and allows the viewer and pilot to be "at risk". J-1 makes
  all of 1–11 the public v1 bar.

### 1.2 Frozen v1 criteria (PROJECT_BRIEF 1–11, as refined by J-1 to J-11)

| # | Criterion (abridged; the brief is authoritative) | Gate | Objective evidence today |
|---|---|---|---|
| 1 | `codeagent solve --repo --task --verify` exits "solved", code 0, on the flagship, from a clean checkout | α | none: no CLI [absent] |
| 2 | Final report contains the diff, the full event trace, the verification result, and cost/latency | α | none: report is in-memory only (`report.py`, imported by no production module) |
| 3 | Flagship ≥3 pinned trials, all published, ≥2 solved | α | none |
| 4 | Harness runs ≥5 synthetic tasks unattended, raw pass-rate report over ≥2 runs | α | none: no `benchmarks/` [absent] |
| 5 | A preserved real-model trace with a genuine, unforced repair iteration | α | none |
| 6 | Every command executed is traced with its policy decision (see C-2) | α | partial mechanism: `VerificationCompleted` carries `command`; no persisted trace |
| 7 | Original checkout unmodified, by a computed per-file hash and status snapshot | α | partial mechanism: `test_slice_c.py` compares `git status --porcelain` and HEAD only |
| 8 | 8–10 synthetic tasks with the same reporting | v1 | none |
| 9 | **Replay viewer** loads a saved trace and renders timeline, plan, tools, policy and diff (J-2) | v1 | none |
| 10 | **Exactly one** vetted real-issue pilot through the full pipeline, trace preserved, with a reproducible preparation method sufficient for that pilot (J-8, J-10) | v1 | none |
| 11 | README, diagram, ≥5 ADRs, threat model with adversarial matrix, raw results (4, 8, 10), two-minute demo, retrospective | v1 | ADRs (6) and threat model exist; the rest none |

**Refinements every criterion inherits (J-3 to J-8):**
- **Patch operations (J-3).** All four, add/update/delete/rename, are
  required and model-supplied, with a Python syntax guard. They follow the
  **transactional patch protocol** (ADR 0003), which means exactly:
  - the complete proposal is validated before the first write;
  - a validation failure before mutation leaves the worktree unchanged;
  - a handled failure after mutation has begun makes the worktree
    contaminated.

  The protocol does **not** claim filesystem atomicity, crash consistency, or
  that intermediate writes were never visible (ADR 0003 point 8).
- **Test-file rules.** Changes are allowed only when named in the plan.
  Deletion, weakening and skips are refused or escalated (handoff). ADR 0006's
  `.gitattributes` refusal is retained.
- **Approval modes (J-4).** All three are required: `interactive` (default),
  explicit `none` (curated CI/benchmark only, never selectable by repository
  config: T-I1/T-I4), and `plan-file` (T-I2).
- **Failure recovery (J-6, ADR 0003).**
  - A pre-mutation validation failure needs no recreation.
  - After a handled failure once mutation has begun, disposal of the
    contaminated worktree is confirmed.
  - If the run continues, a clean replacement is created from the last
    accepted checkpoint as a new worktree incarnation (C-1).
  - An unconfirmed disposal or recreation terminates the run with a
    structured operational error.
- **Success (J-7).** Runtime success is command-level: the verify command
  fails at baseline and passes after the patch. The **benchmark
  independently** evaluates correctness with hidden checks outside the
  workspace, and evaluates original-checkout integrity.
- **Benchmark (J-8).** Mandatory: the synthetic suite, cost/latency/variance
  reporting, repeated trials, replay artifacts, and one real pilot.
  Baseline/ablation analysis is **deferred beyond v1**.
- **Platforms.** macOS (Docker Desktop) and Linux (`ubuntu-24.04` x86_64) are
  separate evidence domains (A9). Full-workflow acceptance is Linux CI with
  the fake model, plus a recorded macOS real-model run.

### 1.3 Joint-review decisions applied (2026-10-03)

| # | Decision |
|---|---|
| J-1 | Public v1 = brief criteria 1–11. "v1 useful" (1–7) = internal alpha only. |
| J-2 | Replay viewer mandatory for v1; live UI deferred. |
| J-3 | All four patch operations required. |
| J-4 | All three approval modes required. |
| J-5 | Amendment 14's operator-wiring gate preserved. |
| J-6 | ADR 0003 discard-and-recreate preserved (after mutation has begun; pre-mutation failures need none). |
| J-7 | Command-level runtime success; the benchmark independently checks correctness, hidden checks and checkout integrity. |
| J-8 | Baseline/ablation deferred; synthetic suite, cost/latency, trials, replay artifacts and one real pilot mandatory. |
| J-9 | `retry_worker` kept only if empirically validated; replaced if too trivial, unstable or unrepresentative. |
| J-10 | General dependency preparation deferred; a reproducible method sufficient for the chosen pilot is mandatory. |
| J-11 | Bounded status-document refresh approved; historical evidence never rewritten. |

### 1.4 Conflicts already resolved by records or by J-decisions

| Conflict | Resolution |
|---|---|
| Model `run_command`/`run_process` (spec, guide) | Removed by ADR 0001. Verification commands are controller-owned. |
| `edit_file`/`write_file` (spec) vs typed patch | Typed `apply_patch` under the transactional patch protocol (§1.2), four operations (guide §7; handoff; J-3; ADR 0003) |
| Test editing: hard-block (spec) vs allow with highlighting | Handoff rule |
| Build phases vs Milestones 0–7 | Milestones govern (`CLAUDE.md`) |
| Guide §2 "no web UI" vs handoff/brief run inspector | Replay viewer in v1 (J-2); the guide's exclusion is superseded for the replay viewer only |
| Guide §15 / §9.5 "three (to five) frozen real tasks" vs brief "exactly one pilot" | One pilot (J-8, brief criterion 10); 3–5 → v1.1 |
| Guide §11/§14 baseline and ablation | Deferred (J-8) |
| Guide §9.7 per-test fail-to-pass at runtime | Command-level runtime; per-test via the benchmark's hidden evaluators (J-7) |
| `--yolo` | `--approval=none` (guide §9.6) |
| Separate budget terminal reasons | ADR 0002 |
| Brief "v1 useful = committed bar; viewer/pilot at risk" | Superseded by J-1/J-2 |

### 1.5 Authority conflicts C-1 to C-7 — joint-review resolutions (2026-10-03)

| # | Conflict | Evidence | Resolution (accepted) |
|---|---|---|---|
| **C-1** | ADR 0003 points 2, 4, 5 and ADR 0004 §9 ("dispose of the contaminated worktree … then recreate the worktree from the pinned checkpoint") vs `StateRoot.reserve_worktree_leaf`'s docstring ("operates within an already-registered worktree, never by re-entering this creation primitive"). The reservation object is single-consumer. | `state_root.py` (`reserve_worktree_leaf` docstring); ADR 0004 §9; `lifecycle_store._validate_worktree_edge` already permits `absent → creating` (Amendment 10 incarnations) | **The ADRs win; the docstring is stale** and is corrected when D5 lands. See the recreation steps and the required amendment below this table. |
| C-2 | Brief criterion 6 ("every command the agent executes … policy-allowed or policy-blocked") vs ADR 0001 (the agent executes no commands) | ADR 0001; `domain.ToolName` | Every **controller-executed** command (baseline, verification, evaluator) receives a `PolicyDecisionRecorded` event plus its completion event in the persisted trace. |
| C-3 | Brief dependency support matrix (`requirements.txt` with hashes / PEP 621 "Supported") vs J-10 | Brief "Open items" | v1 supports the **selected pilot's reproducible preparation method** only. General dependency support, including that matrix, is v1.1. |
| C-4 | Brief criterion 1 `--verify "<test command>"` (one string) vs structured argv, no shell (guide §7, ADR 0001) | Brief; guide | Accepted with the structured-argv semantics below this table. |
| C-5 | Handoff frontend stack (FastAPI + React/TS + Vite + SSE) vs J-2's replay-only scope | Handoff "Frontend decision" | **Static replay viewer** for v1, reading persisted JSONL. FastAPI and SSE are deferred with the live UI. |
| C-6 | Guide §9.2 file read 256 KiB vs `reader.MAX_READ_BYTES` = 64 KiB | `reader.py:51` | **64 KiB per-call default** (current code), with **ranged reads and pagination** for larger files (D4). |
| C-7 | Stale status text: "PRE-BUILD" / "no implementation exists" (handoff, guide, spec, brief); threat-model executive summary ("only Milestone 0"); T-H2 "none yet" despite S4 PASS; `executor.py` docstring ("isolation and interruption spikes … unrun") | Those files | **Bounded status refresh approved** (D10, J-11): status lines only, no historical evidence rewritten. |

**C-1: what recreation means** (on the continue path, after a handled
post-mutation failure):

1. Confirm disposal of the contaminated worktree: registration absent, Git
   admin entry absent, and its leaf observed absent. The worktree publisher
   reaches `disposing → absent`.
2. Obtain a **fresh** `_WorktreeLeafReservation` from a new call to
   `reserve_worktree_leaf()` at the same deterministic lifecycle path. The
   consumed reservation object is **never reused or reclaimed**.
3. Publish a **new worktree incarnation** through `absent → creating →
   present`, with `expected_head` set to the last accepted checkpoint SHA.
4. Keep the checkpoint ref unchanged (ADR 0004 §8–9).

The **required ADR 0004 amendment** (before D5's recreation code) defines:
- incarnation continuity within one lifecycle (what persists across
  incarnations and what resets);
- fresh-reservation rules;
- the publisher transitions;
- crash reconciliation during recreation (each window between the steps
  above), with abandonment as the exit for any shape it refuses.

**C-4: the `--verify` structured-argv semantics.**
- The string is converted to argv by **POSIX-style lexical splitting only**:
  whitespace separates words; single quotes, double quotes and backslash
  escapes are honoured as quoting; quotes are removed.
- It is **never evaluated by a shell**. There is no variable expansion, glob
  expansion, tilde expansion, command substitution, pipeline, redirection or
  control-operator execution.
- **Refused before any lifecycle mutation:**
  - unbalanced quotes or a trailing backslash;
  - an empty result;
  - any word containing NUL;
  - any **unquoted** occurrence of `|`, `&`, `;`, `<`, `>`, `(`, `)`,
    `` ` `` or `$`, which in a shell would be pipeline, control, redirection,
    subshell or substitution syntax. Refusing them prevents a silent meaning
    change.
- **Passed through literally:** quoted occurrences of those characters, and
  unquoted `*`, `?`, `[`, `~` (glob and tilde are simply not expanded).
- The resulting argv is recorded verbatim in the trace (`RunStarted` /
  `VerificationCompleted`).

---

## 2. User-visible v1 workflow

### 2.1 Production call graph (verified)

- **Roots.** `_lifecycle_run.run_lifecycle_aware` is the only composition
  root, and **no production module imports it**. There is no other entry
  point.
- **What the root calls:**
  - `lifecycle_store.prepare_lifecycle` → `reconciliation.reconcile_repository`;
  - `StateRoot.reserve_worktree_leaf` → `workspace.GitWorktree`;
  - `controller.RunController`, wired with `executor.DockerVerifier`,
    `patch.GitPatchApplier(path, patch_operations)`,
    `reader.WorktreeFileReader`, `checkpoint_session.CheckpointSession` and
    `evidence.FilesystemEvidenceSink`.
- **Model and approval** come from the caller (the tests' fakes). Nothing in
  `src/` implements `ModelClient` or `ApprovalProvider`.
- **`report.py`** is imported by no production module.

### 2.2 Steps

| # | Step | Current owner | State |
|---|---|---|---|
| 1 | Install | `pyproject.toml` (`0.0.0`, no `[project.scripts]`) | [absent] |
| 2 | Invoke CLI | none | [absent] |
| 3 | Load repo/task/config | `repo_identity` discovery real; `RunConfig` in code only | Parsing [absent] |
| 4 | Admission and reconciliation | `prepare_lifecycle` → `reconcile_repository` | Mechanism-verified; internal |
| 5 | Isolated workspace | `GitWorktree` (reservation plus publisher) | Mechanism-verified; internal |
| 6 | Baseline | `RunController` → `DockerVerifier.run_baseline` | Mechanism-verified; internal |
| 7 | Explore | `WorktreeFileReader.read_file` (one file, chosen by a fake) | List/search [absent] |
| 8 | Propose plan | `ModelClient` protocol, fakes only | Real model [absent] |
| 9 | Approval | `ApprovalProvider` protocol, fakes only | No modes implemented [absent] |
| 10 | Patch and checkpoint | `GitPatchApplier` (one preconfigured replacement); `CheckpointSession` | Checkpoint mechanism-verified; patch not model-driven |
| 11 | Docker verification | `DockerVerifier.run` | Mechanism-verified; internal |
| 12 | Repair loop | `RunController._run_after_start` (repair/revision budgets) | No failure feedback; four of six budgets [absent]; no recreate after a post-write failure (it terminates) |
| 13 | Cleanup, cancel, recovery | `_terminate`; `_lifecycle_run`; next-run reconciliation | Cancellation and abandonment [absent] |
| 14 | Report and trace | `report.build_report` (unused in production); `EventLog` in-memory | JSONL sink and redaction [absent] |

---

## 3. Capability matrix

### 3.1 State model (corrected)

The two axes are separate.

**v1 state** uses only the ledger enum: `NOT_STARTED`, `DESIGNED_ONLY`,
`IMPLEMENTED_UNWIRED`, `WIRED_INTERNAL_ONLY`, `OPERATOR_REACHABLE`,
`VERIFIED_LOCAL`, `VERIFIED_LINUX_CI`, `VERIFIED_REAL_WORKFLOW`,
`COMPLETE_FOR_V1`, `DEFERRED_TO_V2`.
- It describes the capability as the **operator** will use it.
- `COMPLETE_FOR_V1` requires operator reachability plus objective proof on
  the intended operator path.
- **No row is `COMPLETE_FOR_V1` today.**

**Mechanism maturity** describes the internal substrate only:
- `NONE`;
- `DESIGNED` — accepted design, no code;
- `PARTIAL` — some code, materially incomplete for v1;
- `MECHANISM_VERIFIED` — the mechanism itself is complete for its v1 job and
  verified by real-substrate tests in Linux CI, but not operator-reachable.

`MECHANISM_VERIFIED` is **not** product completion.

### 3.2 Matrix

"Mech." is the mechanism-maturity axis and "v1" is the v1-state axis.
"Mechanism evidence" lists tests that prove the mechanism, not the operator
path.

| Capability | Mech. | v1 | Mechanism evidence | Missing for v1 | Blockers | Done when | Deliverable |
|---|---|---|---|---|---|---|---|
| Installation/packaging | NONE | NOT_STARTED | `pyproject.toml` | Console script, runtime deps, fresh-venv install | — | Fresh-venv install + `codeagent --help` in CI and README | D1, D10 |
| CLI entry point | NONE | NOT_STARTED | [absent] | `reconcile`, `solve` | Gate O for `solve` | CLI e2e in CI; exit codes pinned | D1, D3, D6 |
| Task/config parsing | NONE | NOT_STARTED | `RunConfig` validation | argv → config, C-4, T-I4 | D6 | Invalid input refused before any lifecycle mutation | D6 |
| OpenAI Responses adapter | NONE | NOT_STARTED | S2 spike unstarted | Adapter, strict schemas, S2 | T-G1 (D6) | Recorded-response replay in CI + opt-in live smoke | D7 |
| Tool schemas / response parsing | DESIGNED | NOT_STARTED | `domain.ToolName`; `events.ToolRequested` [test `test_events.py`] | Schemas, malformed-argument handling | D4 | Illegal/malformed calls fail closed with events | D4, D7 |
| Budgets, usage, retries, API failures | PARTIAL | IMPLEMENTED_UNWIRED | Repair and plan-revision budgets (`controller.py`) [test `test_controller.py`] | Tool-call, wall-clock, token, cost; retries | D4, D7 | Each `BudgetKind` ends a run correctly, tested | D4, D7 |
| Repository identity / Git safety | MECHANISM_VERIFIED | WIRED_INTERNAL_ONLY | `repo_identity.py`, `_git_safety.py` (ADR 0006), real SHA-256 tests [CI] | Operator path | Gate O | Reached and proven via `solve` | D6 |
| File reading/search | PARTIAL | WIRED_INTERNAL_ONLY | `WorktreeFileReader` (one file, 64 KiB, escape refusal) [test `test_reader.py`] | `list_directory`, ranged read, `search_text`, pagination | — | Bounded, path-safe, escape-tested | D4 |
| Patch application (4 ops, transactional protocol) | PARTIAL | WIRED_INTERNAL_ONLY | `GitPatchApplier`: one exact replacement, ADR 0006 hardening [test `test_patch.py`] | add/update/delete/rename; whole-proposal pre-write validation; syntax guard; test-file rules; model-supplied integration | Integration: D4 | Pre-write refusal leaves the worktree unchanged; each op tested; rules enforced; model-supplied ops through D4 | D5 |
| ADR 0003 discard-and-recreate | DESIGNED | NOT_STARTED | ADR 0003; controller terminates on patch failure (compliant only for a terminating run) | Fresh-reservation incarnation on the continue path; mid-recreate crash reconciliation | C-1 ADR 0004 amendment | Post-write failure → confirmed disposal + leaf absence → fresh reservation → new incarnation at the last checkpoint → repair continues (real Git) | D5 |
| Checkpoint create/advance/delete | MECHANISM_VERIFIED | WIRED_INTERNAL_ONLY | `checkpoint_ref.py`, `checkpoint_session.py`; T30, T34 [CI] | Operator path | Gate O | Proven via `solve` | D6 |
| Approval gate (3 modes) | PARTIAL | WIRED_INTERNAL_ONLY | FSM legality [test `test_domain.py`]; fake provider | `interactive`, `none` (T-I4), `plan-file` (T-I2) | D6 | Live CLI run proves no pre-approval mutation; rejection leaves source byte-identical | D6 |
| Orchestration and repair loop | PARTIAL | WIRED_INTERNAL_ONLY | `RunController` FSM; exception boundary (Amendment 9) [CI] | Multi-tool loop, failure feedback, reapproval on material change | D4 | Fake CI: fail → feedback → revised patch → pass | D4 |
| Evidence and reporting | PARTIAL | IMPLEMENTED_UNWIRED (`report.py` unused) | `FilesystemEvidenceSink` [CI]; `report.py` [test] | JSONL sink, redaction, persisted reports | — | Redacted trace + JSON/Markdown report every run; redaction-scan test | D6 |
| Docker verification | MECHANISM_VERIFIED | WIRED_INTERNAL_ONLY | `DockerVerifier`; `test_slice_c.py` (4) [CI] | Operator path; cancellation | D3, Gate O | Proven via `solve` | D3, D6 |
| Command execution policy | MECHANISM_VERIFIED (ADR 0001 narrowing) | WIRED_INTERNAL_ONLY | No model process tool; structured argv | C-2 tracing; C-4 parsing | D6 | Every controller command traced with a policy decision | D6 |
| Network/resource/timeout enforcement | MECHANISM_VERIFIED (spike-level) | WIRED_INTERNAL_ONLY | `executor._SECURITY_FLAGS`; `spikes/s4/S4_RESULT.md` (memory INCONCLUSIVE; predates Amendment 15's uid change) | Production-suite adversarial tests on both platforms | — | Attempted violations fail in the CI suite and on macOS | D10 |
| Lifecycle state root and locks | MECHANISM_VERIFIED | WIRED_INTERNAL_ONLY | `state_root.py`, `state_locks.py`; T40 [CI] | Operator path | Gate O | Proven via `solve` | D6 |
| Owner/container/worktree/ref publication | MECHANISM_VERIFIED | WIRED_INTERNAL_ONLY | Shared cursor + 4 publishers (Amendments 6–11) [CI] | Operator path | Gate O | Proven via `solve` | D6 |
| Lifecycle-aware composition | PARTIAL | WIRED_INTERNAL_ONLY | `run_lifecycle_aware`, T30–T47 [CI]; **caller-supplied patch** | Model-driven tools; operator path | D4–D6, Gate O | `solve` drives it with model-supplied patches | D6 |
| Reconciliation, documented shapes | PARTIAL | WIRED_INTERNAL_ONLY | Rows: all-absent, containers, A12, A13, A16, A17 [CI]; T33–T35, T37, T47 | Container + worktree (+ ref) | D2 | See §4 | D2 |
| Live-container → worktree → ref | NONE | NOT_STARTED | T36 asserts BLOCKED [test] | Chained row | — | T36 → RECONCILED, full cleanup | D2 |
| Abandonment / explicit reconcile | DESIGNED | NOT_STARTED | ADR 0004 §10–11; no `ABANDONED` handling in `src/` [absent] | Whole feature | — | Every blocked entry has an operator exit: `ABANDONED` only after confirmed absence; `ABANDONED_UNRESOLVED` only by explicit acknowledgement, never `CLEAN`; no resource deleted; exit codes pinned | D1 |
| ADR 0005 cancellation | DESIGNED | NOT_STARTED | ADR 0005 "not implemented"; no `CancellationToken` or `signal.signal` in `src/` [absent] | Whole feature, including CLI-owned handlers | D1 (CLI package) | Real SIGINT/SIGTERM tests → `CANCELLED`, resources confirmed absent | D3 |
| Operator lifecycle wiring | NONE | NOT_STARTED | `_lifecycle_run` AST-pinned as unimported (T41) | `solve` | Gate O | `solve` is the only run caller | D6 |
| Deterministic fake-model e2e CI | PARTIAL | WIRED_INTERNAL_ONLY | T30 with a fake model and **preconfigured patch** [CI] | Through the CLI, model-driven | D4–D6 | CI drives `codeagent solve` with a scripted fake model emitting the patch | D6 |
| Real-model smoke / flagship trials | NONE | NOT_STARTED | — | Adapter, trials, J-9 fixture validation | D7 | Criteria 1, 3, 5 | D7 |
| macOS acceptance | — | VERIFIED_LOCAL (mechanisms only) | Per-slice local runs | Full workflow | D7 | Recorded macOS real-model flagship run | D7 |
| Linux acceptance | — | VERIFIED_LINUX_CI (mechanisms only) | `ci.yml`; run 37158716812 | CLI e2e | D6 | CI runs `solve` fake e2e | D6 |
| Synthetic benchmark | NONE | NOT_STARTED | [absent] | Harness + 8–10 tasks | D6 | Criteria 4, 8 | D8a, D8b |
| Real-issue pilot + prep method | NONE | NOT_STARTED | [absent] | Pilot, J-10 method | D8a | Criterion 10 | D8b |
| Cost/latency/variance | DESIGNED | NOT_STARTED | `RunFinished` fields exist | Aggregation | D8a | Per-task raw + aggregates | D8a |
| Replay viewer | NONE | NOT_STARTED | S6 spike unstarted | Viewer | D6 (trace format) | Criterion 9 | D9 |
| README, diagram, demo, retrospective | NONE | NOT_STARTED | Material in `ENGINEERING_LOG.md` | All | D7–D9 | Criterion 11 | D10 |
| Baseline / ablation | — | DEFERRED_TO_V2 | — | — | — | Beyond v1 (J-8) | — |
| Live UI, general dependency prep, multi-provider | — | DEFERRED_TO_V2 | — | — | — | — | — |

---

## 4. Threat and failure-closure matrix

Automated tests are **not** a security review. Every row is automated-test
evidence at most.

| Threat / failure | v1 obligation | Current evidence | Missing | Blocks |
|---|---|---|---|---|
| T-E1 concurrent runs | **Fail closed** | Repository lock for the whole internal run; T40 | Operator path | Gate O / criterion 1 |
| T-F1 orphaned containers | **Automatic recovery for the verification-phase shape** (Gate O row); for any other refused shape, abandonment is the operator exit (it records a disposition and never removes resources) | 3B-5 row; `test_slice_3b6.py` SIGKILL [CI] | Container + worktree (+ ref) row | Gate O |
| T-F2 orphaned worktree | **Fail closed**; abandonment is the operator exit for refused shapes (a recorded disposition, never resource removal) | A12/A13/A17 rows; T35/T37 [CI] | Abandonment; mid-recreate crash shape (C-1) | Gate O; D5 |
| T-M1 shared `.git` metadata | **Fully mitigate original-checkout integrity**; document ref visibility | Exact `worktree remove`, no prune (AST-pinned); CAS ref delete | Per-file hash snapshot (criterion 7); benchmark integrity check (J-7) | 7 |
| Cancellation | **Implement** (ADR 0005) | None; T34's `KeyboardInterrupt` path reconciles on the next run | Whole ADR | Gate O |
| Abandonment | **Implement** (ADR 0004 §11) | None | Whole section | Gate O |
| Container-escape identity (T-K1, A5) | **Accepted limitation, documented** | Amendment 15: the container runs as the effective host uid:gid; root refused | README statement that an escape maps to the invoking user | 11 |
| Same-user A4 races | **Accepted limitation, documented** | Narrowed by fresh gates and CAS (A16/A17); not closed | Operator-visible statement | 11 |
| T-G1 redaction | **Gate: before real-model integration or real-content persistence** | None | Redactor + scan test | D7 |
| T-H2 network revocation | **Proven by attempted violation** | S4 PASS on both platforms (threat-model entry stale, C-7) | Production-suite test | 11 |
| T-I1/T-I2/T-I4 approval | **Fail closed** | Mode enum only | Providers, guard, plan-file integrity | 1, D6 |
| T-A4 test weakening | **Refuse or escalate** | None | Patch rules | D5 |
| T-F3 truncated JSONL | **Tolerant reader, documented** | No sink | Sink behavior | D6, D9 |
| Memory limit (S4 INCONCLUSIVE) | **Document** | OOM classified (`EXECUTOR_OOM_KILLED`) | Documented limit | 11 |

---

## 5. Milestone status

| Milestone | Completed | Incomplete | False friends | Status |
|---|---|---|---|---|
| M0 Contracts and threats | `domain.py`, `events.py`, `errors.py`, ADRs 0001–0006, threat model | Stale status text (C-7) | — | **Complete** |
| M1 Thin vertical slice | Fake read → plan → approve → patch → verify → report in a real worktree and Docker (`test_slice_c.py::test_real_docker_e2e_failing_baseline_then_passing_verification`) | — | Preconfigured patch: fine for M1, not evidence for M2/M4 | **Complete** |
| M2 Robust repository tools | Bounded single read; checkpoints; ADR 0006 | List/search/pagination; 4-op transactional patch protocol; syntax guard; recreate (C-1) | `patch.py` is hardened but supports one replacement | **Partial** |
| M3 Hardened execution | Docker lifecycle; S4 limits; bounded control plane; lifecycle store; reconciliation rows; internal composition | Cancellation, abandonment, redaction, container chaining, production adversarial tests, operator path | `_lifecycle_run` resembles an entry point but is uncalled | **Substrate nearly complete; product partial** |
| M4 Real model loop | — | All | `ModelClient` is the fake-shaped two-step protocol | **Not started** |
| M5 Repair loop and verification contract | Repair/revision budgets; baseline; terminal reasons | Failure feedback, reapproval, recreate-and-continue | `VerificationCompleted.fail_to_pass` always `()` | **Partial** |
| M6 Benchmark harness | — | All | — | **Not started** |
| M7 Portfolio release | ADRs, threat model | Everything else | — | **Not started** |

---

## 6. Critical path (corrected order)

1. **D1 — Repository recovery.** Abandonment plus the narrow
   `codeagent reconcile` maintenance command. It never runs a task.
2. **D2 — Live-container chaining:** container → worktree → ref
   reconciliation.
3. **D3 — CLI ownership boundary plus ADR 0005 cancellation.** The
   `codeagent` entrypoint owns the signal handlers, so signal ownership is
   real. **Gate O is satisfied only after D1–D3.**
4. **D4 — Model-driven exploration and repair loop**, with the fake model:
   tool protocol, list/read/search, budgets, failure feedback, reapproval.
5. **D5 — Transactional patch protocol.**
   - **Substrate:** four operations, whole-proposal pre-write validation,
     syntax guard, test-file rules, and ADR 0003 discard-and-recreate. It
     waits for the C-1 ADR amendment, but is independent of D4.
   - **Integration:** model-supplied operations through D4's tool loop.
     This part depends on D4.
6. **D6 — Operator vertical path:** `codeagent solve`, config, three approval
   modes, persisted redacted trace, reports, checkout-integrity snapshot,
   fake-model CLI e2e in CI. The first permitted operator run.
7. **D7 — Real model adapter and flagship:** Responses adapter (S2), real
   budgets, J-9 fixture validation, 3 trials, a genuine repair trace, macOS
   acceptance.
8. **D8a — Benchmark harness and 5 synthetic tasks**, with independent hidden
   evaluation and integrity checks. **→ Gate α (internal alpha).**
9. **D8b — Remaining synthetic tasks (to 8–10), the pilot preparation method
   and the real pilot.**
10. **D9 — Replay viewer.**
11. **D10 — Release:** packaging, production adversarial suite on both
    platforms, README/diagram/methodology/results/traces/demo/retrospective,
    and the bounded status refresh (J-11, C-7). **→ Gate v1, only once every
    §1.2 row has objective evidence.**

**Rules this order enforces:**
- **No `solve`/operator run wiring before D1–D3.** D1's maintenance command
  is allowed earlier because it *is* the recovery prerequisite.
- **What may run in parallel.** D4, D5's substrate and the C-1 amendment are
  mutually independent and may proceed alongside D2/D3, since they are
  internal and test-only.
- **What must wait.** D5's integration waits for D4. D6 depends on both
  completed deliverables, D4 and D5 (substrate and integration).
- Nothing in D4 or D5 is operator-reachable until D6.
- **D7 cannot start before D6's redaction** (the T-G1 gate).

**Why this differs from the earlier draft:**
- abandonment first;
- chaining before cancellation (per joint review);
- the CLI boundary introduced with cancellation;
- "operator wiring" deferred until the model-driven path exists, so the first
  operator run never applies a caller-supplied patch.

---

## 7. Deliverables (consolidated)

Hours are focused-engineering **planning ranges** [inference], including the
review/correction passes this project consistently runs. Every deliverable
must keep the failure boundaries of the mechanisms it touches; its
acceptance tests must include the named failure paths.

| ID | Deliverable | Acceptance tests (minimum) | Failure boundaries | Depends | User-visible | Hours |
|---|---|---|---|---|---|---|
| **D1** | Abandonment (ADR 0004 §11) + explicit reconcile + `codeagent reconcile [--dry-run] [--abandon ID [--acknowledge-unresolved --reason]]`; console-script packaging skeleton | (a) A blocked entry (invalid run directory, T36 shape, partial-removal leftover) makes `reconcile` report `BLOCKED`. (b) Normal `--abandon` **succeeds only** when fresh inspection confirms no attributable resource or registration remains: it records `ABANDONED`, reconcile reports `CLEAN`, and admission proceeds. (c) Normal `--abandon` with resources remaining, or with inspection failing, is **refused**: no marker, still `BLOCKED`. (d) Only `--abandon ID --acknowledge-unresolved --reason ...` records `ABANDONED_UNRESOLVED`: later admission proceeds **with the required warning** on CLI and report, and reconcile returns `UNRESOLVED_ACKNOWLEDGED`, **never `CLEAN`**. (e) `--dry-run` writes nothing. (f) A busy repository or lifecycle lock is refused, nonzero. (g) Exit codes are pinned. | Neither abandonment form deletes or mutates containers, worktrees, refs or registrations. Never runs a task. Refuses an active lifecycle. | — | Yes | 16–32 |
| **D2** | Container (+ worktree, + ref) chained reconciliation | T36 → RECONCILED with full physical cleanup; unproven container owner → REFUSED; write-fault and SIGKILL resume matrix | Order containers → worktree → ref; each phase gated; no atomicity claim | — | Indirect | 10–22 |
| **D3** | CLI ownership boundary (`codeagent` `main` owning SIGINT/SIGTERM per ADR 0005 §2) + cancellation token, safe points, interruptible verifier, `CANCELLED`, `start_new_session` | Real SIGINT/SIGTERM during baseline, verification and approval wait → `CANCELLED`, containers and worktree confirmed absent, next admission clean; repeated signals idempotent | Library modules never call `signal.signal`; patch apply non-interruptible | D1 | Yes | 20–40 |
| **D4** | General multi-tool model protocol + scripted fake; `list_directory`, ranged/paginated `read_file` (64 KiB per call, C-6), `search_text`; dispatcher with policy events; tool-call and wall-clock budgets; failure feedback; reapproval on material change | Fake CI: fail → feedback → revised plan → reapproval → pass; escape, size and pagination refusals; illegal-state calls fail closed; each budget kind ends the run | Model output untrusted (A1); reads path-safe | — | Indirect | 30–55 |
| **D5** | Transactional patch protocol (§1.2). **Substrate:** add/update/delete/rename, whole-proposal pre-write validation, syntax guard, test-file rules, `.gitattributes` refusal kept, ADR 0003 discard-and-recreate on the continue path via a fresh reservation and new incarnation (C-1). **Integration:** model-supplied operations through D4's tool loop. | Each op incl. rename/delete; a pre-write validation refusal leaves the worktree unchanged; a post-write handled failure → contaminated → confirmed disposal and leaf absence → fresh reservation → new incarnation at the last checkpoint → repair continues (real Git); disposal or recreate unconfirmed → structured error; mid-recreate SIGKILL reconciles or blocks with an abandonment exit; scripted-fake model supplies the ops end to end | No claim of filesystem atomicity or invisible intermediate writes; never per-file rollback; never trust a contaminated worktree; never reuse a consumed reservation; ref kept across recreation | Substrate: the C-1 ADR amendment only. Integration: D4. | Yes | 35–65 |
| **D6** | `codeagent solve` (config, C-4 argv parsing, T-I4 guard); `interactive`, `none` and `plan-file` approval; persisted JSONL sink + redactor + JSON/Markdown reports; per-file content-hash checkout snapshot; C-2 command tracing; fake-model CLI e2e in CI | CLI e2e solves the fixture via the scripted fake model; rejection leaves the source byte-identical; repo config cannot select `none`; tampered plan-file refused; redaction scan on seeded secrets; truncated JSONL tolerated | Gate O must already hold; nothing mutates before approval | D1–D3; D4 and D5 complete | Yes | 34–66 |
| **D7** | OpenAI Responses adapter (S2 spike): strict schemas, multiple calls, malformed/incomplete responses, retries, token/cost budgets; recorded-response replay CI; opt-in live smoke; J-9 flagship validation; 3 pinned trials; genuine repair trace; macOS manual acceptance | Criteria 1, 2, 3, 5, 6, 7 evidenced; malformed and incomplete responses fail closed | T-G1 (D6) before any real content; no forced failures | D6 | Yes | 32–65 (+ API cost) |
| **D8a** | Benchmark harness: case format, hidden evaluator outside the workspace, LLM-free case validation, independent correctness + checkout-integrity checks (J-7), raw storage, repeated trials, cost/latency/variance, failure taxonomy; first 5 synthetic tasks | Every case: broken fails, reference passes; two unattended runs; integrity check catches a seeded checkout mutation | Evaluators never visible to the model | D7 | Yes | 35–70 |
| **D8b** | Synthetic tasks to 8–10; reproducible preparation method for the chosen pilot (J-10, T-H1 conditions); one vetted real pilot | Criteria 8 and 10 evidenced; preparation reproduces from a recorded artifact identifier | Network closed before any agent execution | D8a | Yes | 20–50 |
| **D9** | Static replay viewer reading persisted JSONL (C-5) | Criterion 9: renders the D7 trace's timeline, plan, tools, policy and diff | Display only; no orchestration logic | D6 trace format | Yes | 20–45 |
| **D10** | Release: fresh-venv packaging; production adversarial sandbox suite (network, PIDs, timeout, output, filesystem escape) in CI and on macOS; README, diagram, methodology, raw results, traces, demo, retrospective; bounded status refresh (J-11) | Criterion 11; cold-read review; documented commands reproduce | No historical evidence rewritten | D7–D9 | Yes | 22–44 |

That is **11 deliverables**, all mandatory for public v1. The precision
corrections in this revision change wording, dependencies and acceptance
tests, not scope, so **the estimate is unchanged at 274–554 hours**.

**Cumulative planning ranges** [inference]:

| Gate reached | Deliverables | Hours |
|---|---|---|
| Gate O satisfied | D1–D3 | **46–94** |
| First operator run (fake model, model-driven tools) | + D4–D6 | **145–280** |
| Internal alpha (Gate α, criteria 1–7) | + D7, D8a | **212–415** |
| Public v1 (Gate v1, criteria 1–11) | + D8b, D9, D10 | **274–554** |

**Uncertainty drivers:**
- real-model behaviour, cost and the chance a genuine repair iteration
  appears (D7);
- the C-1 amendment's scope (D5);
- author-owned task authoring and pilot discovery (D8);
- the viewer's front-end scope (D9).

---

## 8. Stop conditions and anti-drift rules

1. **Map to the ledger.** No new slice without mapping it to one deliverable
   (§7) and one criterion (§1.2) or gate. Work that maps to none is v2 or
   needs an explicit scope decision.
2. **No completion claim off the operator path.** No capability is
   `COMPLETE_FOR_V1` or `OPERATOR_REACHABLE` unless it is reachable and proven
   through the intended operator path. `MECHANISM_VERIFIED` is never reported
   as product completion.
3. **Gates are explicit.** No CLI or operator run wiring before Gate O.
   Internal alpha is never called v1. Public v1 is declared only when every
   §1.2 row has objective evidence.
4. **Update the ledger with status changes.** The ledger is updated in the
   same commit as any status-changing implementation; a CI-evidence
   follow-up updates only the affected row.
5. **No duplicated CI evidence.** Post-CI timings and counts are recorded once,
   in the owning ADR amendment's evidence section. `CLAUDE.md` gets one line
   with the run link; the ledger gets none unless a criterion changes.
6. **ADR amendments only for invariants.** None for a purely mechanical
   change. One is required when an accepted invariant, gate or ownership
   boundary changes (e.g. C-1).
7. **Real acceptance tests for safety controls.** Every new safety control
   needs a real-substrate acceptance test (attempted violation, real
   Git/Docker/SIGKILL/signal); fakes supplement, never replace.
8. **Deferred limitations must be visible.** Every deferred or accepted
   limitation names its operator-visible behavior (exit code, message, report
   field).
9. **v2 cannot block v1.** `DEFERRED_TO_V2` items cannot block v1 and are not
   started before Gate v1 without explicit approval.
10. **Bound correction passes.** A slice's review/correction passes stop when
    no correctness or overclaim finding remains.
11. **Freeze the boundary.** §1.2 is frozen. Changing it requires an explicit,
    dated ledger amendment. Unresolved C-items are decided before the
    deliverable that depends on them starts.

---

## 9. Immediate recommendation

**Next: D1 — ADR 0004 §11 abandonment and explicit reconcile, with the
narrow `codeagent reconcile` maintenance command and console-script
skeleton.**

Why it is on the critical path:
- **It is the only exit from blocks that exist today and can never clear
  themselves:**
  - a crash in `prepare_lifecycle` before the first projection (named residual
    since Slice 3B-1);
  - partial-removal leftovers (Amendment 13);
  - locked registrations;
  - T36.
- **It is the first item of Gate O**, and its design is fully accepted (ADR
  0004 §10–11).
- **It creates the CLI package that D3 needs** for real signal ownership. It
  does so through a command that never runs a task and never mutates a
  container, worktree or ref, so it is permitted before Gate O.

Why the others come after:
- **D2** reduces how often blocks happen, but cannot replace an exit from them.
- **D3** needs the entrypoint D1 creates.
- **D4 and D5's substrate** are the largest product gap and may proceed
  independently. D5's integration depends on D4. Neither reaches an operator
  until D6, which requires Gate O.

---

## 10. Evidence index (abridged)

- **No CLI, package entry, README or benchmark:** `pyproject.toml`; no
  `argparse`/`__main__`/`def main` in `src/`; no `README*`; no `benchmarks/`.
- **Call graph:** `_lifecycle_run` imported by no `src/` module; `report`
  imported by no `src/` module; no `src/` implementation of `ModelClient` or
  `ApprovalProvider`.
- **Caller-supplied patch:**
  `_lifecycle_run.run_lifecycle_aware(patch_operations=...)`;
  `patch.PatchOperation` (one exact replacement); `patch.py` docstring.
- **Fake-shaped model protocol:** `controller.ModelClient` docstring.
- **In-memory events:** `controller.EventLog` docstring.
- **Empty per-test sets:** `controller.py` (`fail_to_pass=()`).
- **Recreate conflict (C-1):** `state_root.StateRoot.reserve_worktree_leaf`
  docstring vs ADR 0003 decision points 2, 4, 5 and ADR 0004 §9;
  `lifecycle_store._validate_worktree_edge`.
- **Operator gate:** ADR 0004 Amendment 14 "Gate"; `_lifecycle_run.py`
  module docstring; T41.
- **Lifecycle evidence:** `tests/integration/test_lifecycle_run.py` T30–T47
  (T36 BLOCKED, T37 RECONCILED); `tests/unit/test_reconciliation.py`; CI run
  37158716812.
- **Isolation:** `spikes/s4/S4_RESULT.md`; `executor._SECURITY_FLAGS`;
  Amendment 15.
