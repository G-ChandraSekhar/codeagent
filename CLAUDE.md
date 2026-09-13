# CodeAgent — Project Instructions for Claude Code

This file is read automatically at the start of every Claude Code
session in this repository. Follow it without being reminded.

## Before any implementation work

1. Inspect the current repository, tests, git status, and any existing
   ADRs before proposing or making changes. Never assume state — check.
2. State which milestone (see `docs/CODEAGENT_IMPLEMENTATION_GUIDE.md`
   §12) and which acceptance criterion the requested work advances.
3. Preserve existing user changes; avoid unrelated refactors.
4. Prefer the smallest vertical implementation that can be demonstrated
   end-to-end over a broad partial build.
5. Add failure-path tests, not only happy-path tests.
6. Never weaken a security or sandbox boundary just to make a demo pass.
7. Never claim something works based on plausible-looking output — run
   the actual verification (real tests, the real CLI, the real sandbox)
   before reporting success.
8. Record new consequential decisions or deviations as a proposed ADR
   update — don't silently bake an undiscussed decision into code.
9. Surface conflicts or missing author decisions instead of guessing.
   Ask, using AskUserQuestion where appropriate for real branching
   decisions, rather than picking silently.
10. End every turn with: files changed, verification actually performed,
    remaining risks, and the next smallest useful step.

## Scope discipline

Do not expand v1 scope (`docs/PROJECT_BRIEF.md`'s "Explicitly out of
scope" list, the frozen Stage 1 baseline — supported by
`docs/CODEAGENT_LLM_HANDOFF.md`'s "Excluded" list and
`docs/CODEAGENT_IMPLEMENTATION_GUIDE.md` §2 "Out of scope") without the
user's explicit approval, even when a library or the model makes a
broader feature trivially easy to add.

## Reference documents, in order of authority

The current explicit user request always outranks every document listed
below — none of these documents authorize overriding what the user
actually asked for in this conversation. The authority order below
applies only when reconciling these repository documents against each
other, not against the user.

1. `docs/CODEAGENT_LLM_HANDOFF.md` — top authority. Where it conflicts
   with `CODEAGENT_IMPLEMENTATION_GUIDE.md` or an open question in
   `DESIGN_SPEC.md`, the handoff's decision wins (see its own "Stage 4
   — Reconcile the handoff package").
2. `docs/CODEAGENT_IMPLEMENTATION_GUIDE.md` — authoritative below the
   handoff. Where it conflicts with an open question in
   `DESIGN_SPEC.md`, this guide's decision wins (see its own §9,
   "Resolved open questions").
3. `docs/DESIGN_SPEC.md` — original product spec and architecture
   rationale. Still the source for *why*, even where *what* has since
   been refined by the handoff and the implementation guide.
4. `docs/BUILD_PLAN.md` — original 7-phase plan. Superseded by the
   Milestone 0-7 sequence in the implementation guide (§12); kept for
   the phase-level rationale, not as the current source of truth for
   sequencing.

## Documentation ladder

Match the record to the decision — don't over- or under-document:

- **ADR** (`docs/adr/`): a durable, cross-cutting decision with credible
  alternatives and a meaningful reversal cost. Created only when a
  consequential decision actually meets this bar — not on a fixed
  schedule or a target count.
- **ENGINEERING_LOG.md**: meaningful session-level trade-offs, evidence,
  corrections, and lessons — including decisions that don't rise to an
  ADR.
- **Code comment**: a non-obvious local invariant.
- **Issue/risk register** (e.g. the threat model's residual-risk
  entries): unresolved or unbuilt work.
- Do not permanently document routine edits, test additions, status
  changes, or ordinary pushes as if they were engineering decisions.

## Current status

Milestone 0 ("Contracts and threats") core deliverables are done:

- `src/codeagent/domain.py` — `RunState`/`Trigger`/`TerminalReason`
  state-transition table (fail-closed), tool-legality-per-state,
  budget/approval vocabulary.
- `src/codeagent/events.py` — 16 typed, cross-validated event schemas
  for the append-only run log.
- `src/codeagent/errors.py` — stable `ErrorCode`/`ErrorDomain`
  taxonomy, explicitly excluding expected domain outcomes.
- `docs/adr/0001-no-model-callable-process-execution.md` — Accepted.
- `docs/adr/0002-single-generic-budget-exceeded-trigger.md` — Accepted.
- `docs/threat-model.md` — Accepted (2026-09-12).
- `ENGINEERING_LOG.md` — session-level decision trail, per the
  documentation ladder above.
- `tests/unit/{test_domain,test_events,test_errors}.py` — Milestone 0
  contract tests.

Further ADRs are not inherently required — per the documentation ladder
above, another ADR is created only when a future consequential decision
actually meets the ADR threshold, not as a standing checklist item.

Milestone 1 ("First real vertical slice") is complete. Its exact
acceptance criterion (implementation guide §12: "Using a deterministic
fake model and a real fixture repository, perform a full
read-plan-approve-patch-verify-report flow") is now demonstrated for
real by `tests/integration/test_slice_c.py`'s end-to-end test — the
controller previously performed plan-approve-patch-verify-report
without ever recording a real repository read; that gap is closed
below.

- **Slice A** — deterministic `RunController` (`src/codeagent/
  controller.py`) driving the domain FSM against fake collaborators
  only (`ModelClient`/`ApprovalProvider`/`Verifier`/`PatchApplier`/
  `Clock` Protocols; fakes live under `tests/support/fakes.py`, never
  in production code).
- **Slice B** — real Git worktree lifecycle (`src/codeagent/
  workspace.py`: `GitWorktree`) and real controlled patch application
  with a real checkpoint commit (`src/codeagent/patch.py`:
  `GitPatchApplier`), exercised against a real throwaway fixture repo
  (`tests/support/fixture_repo.py`, `tests/fixtures/retry_worker/`).
- **Slice C** — real Docker-based verification (`src/codeagent/
  executor.py`: `DockerVerifier`), an inspectable
  create/start/inspect/cleanup container lifecycle with bounded
  streamed output, and a typed-event-derived run report
  (`src/codeagent/report.py`). One real end-to-end demonstration is
  covered by `tests/integration/test_slice_c.py`: temporary worktree →
  real failing baseline in Docker → fake model plan/approval → real
  patch + checkpoint → real passing verification in Docker → report.
  Model and approval collaborators are still fake — Docker sandbox
  claims are explicitly provisional pending the Stage-2 isolation/
  interruption spikes below.
- A pre-commit correction pass on slice C (committed) fixed
  seven real defects found by review: an unsafe cleanup-confirmation
  check (`docker ps -a` exact-name match replacing a `docker inspect`
  exit code, which couldn't distinguish "confirmed absent" from "the
  daemon is broken"); a cleanup-ordering restructure so cleanup always
  runs and its unconfirmed-removal result always overrides any
  provisional outcome, including COMMAND_START_FAILURE; `Verifier`
  gaining a `command` property as the single source of truth (removing
  `RunConfig.verify_command`, which could disagree with what actually
  ran); a public `events.validate_verification_outcome_shape()`
  replacing two underscore-prefixed cross-module calls from
  controller.py; `report.build_report()` validating trace shape
  (single run_id, contiguous sequence, exactly one start/finish,
  finish last) and aggregating multiple `PatchApplied` events
  correctly; `DockerVerifier.__init__` validating its own configuration
  (real directory, digest-pinned image, nonempty command, finite
  positive timeout); and `test_slice_c.py` switched from a fake
  stepping clock to `SystemClock` so real Docker durations aren't
  fabricated. See `ENGINEERING_LOG.md`'s "correction pass" entry.
- **Read completion slice** (committed) — closes Milestone 1's
  actual acceptance gap: a narrow `RepositoryReader` Protocol and
  `ReadResult` type (`src/codeagent/controller.py`), a real bounded,
  path-validated worktree reader (`src/codeagent/reader.py`:
  `WorktreeFileReader` — one UTF-8 file, small fixed byte limit,
  absolute/`..`/symlink-escape rejection, structured failures, no
  listing/pagination/search), and a genuinely two-step `ModelClient`
  Protocol (`request_read_path` then `propose_plan(read_result)`) so
  the deterministic fake model is evidence-driven rather than
  proposing a plan independently of the read it triggers.
  `RunController` now dispatches READ_FILE in EXPLORE (full
  ToolRequested/PolicyDecisionRecorded/ToolCompleted audit trail, a
  bounded result summary and the read's tool_call_id as
  `PlanProposed.evidence_refs` — never raw file content) before ever
  asking the model to propose a plan; a failed read aborts the run as
  UNRECOVERABLE_ERROR without consuming repair/revision budget, same
  placeholder disposition as a failed patch. `tests/integration/
  test_slice_c.py`'s real E2E test now demonstrates the complete real
  sequence: real worktree → real READ_FILE → real content passed to a
  marker-gated fake model that only proposes its plan after seeing the
  fixture's actual `# BUG:` comment → fake approval → real patch +
  checkpoint → real Docker verification → report — with the original
  checkout and Docker cleanup guarantees intact. This is explicitly
  the narrowest slice that satisfies the guide's wording, not
  Milestone 2's general repository-read/tool-loop system.
- A pre-commit correction pass fixed the reader's
  size enforcement (bounded `read()` instead of stat-then-read-whole-
  file), documented the reader's remaining TOCTOU race honestly rather
  than claiming safety it doesn't have (real fix assigned to Milestone
  2's descriptor-relative resolution), tightened `report.build_report()`
  to require `RunStarted` first (not merely present), and corrected a
  misleadingly-named reader test. See `ENGINEERING_LOG.md`'s
  "pre-commit correction pass" entry.
- Full suite: 931 tests passing as of the last full run (`git diff
  --check` clean); the 3 real-Docker tests in `test_slice_c.py` skip
  cleanly on a machine without a Docker daemon rather than weakening
  what they check, and executed (none skipped) and passed against a
  real local daemon in the last run.
- A narrow Linux CI slice (`.github/workflows/ci.yml`) now runs this
  suite on GitHub-hosted `ubuntu-24.04` x86_64 with Python 3.12. Its
  first real run — commit `a845cb3`, run
  [34734760525](https://github.com/G-ChandraSekhar/codeagent/actions/runs/34734760525)
  — concluded `success`: Docker preflight passed, the pulled
  verification image confirmed `linux/amd64`, the 3 real Docker tests
  passed with 0 skipped, the full suite passed all 873, and no
  `codeagent-verify` containers were left behind. This is evidence for
  ubuntu-24.04 x86_64 specifically, not a general Linux or ARM64
  claim; see `ENGINEERING_LOG.md`'s "narrow Linux CI slice" entry.

Stage 2 (of the four-stage planning process in
`docs/CODEAGENT_LLM_HANDOFF.md`) spikes:

- **S1 — temporary worktree + Docker + pytest + cleanup: PASSED**
  (Linux; 3 trials, exit 0, consistent per-test results, no orphaned
  containers or worktrees after cleanup). See `spikes/s1/S1_RESULT.md`.
- **S3 — multi-file patch atomicity: DONE, decision accepted.** Three
  distinct guarantees found, not one: prevalidation atomicity is real
  (experiment 1); a handled mid-application failure produces a
  genuinely observable partial state before any rollback runs, and
  in-place `git checkout` rollback was demonstrated only for one
  tracked, previously-clean file (experiment 2); a SIGKILL produces a
  partial worktree that is detectable only by convention, not by any
  structural safeguard (experiment 3, deliberately not called "crash
  consistency" — see the corrected terminology in
  `spikes/s3/S3_RESULT.md`). Author decision, recorded as
  **`docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`
  (Accepted)**: Milestone 2's patch mechanism must discard and
  recreate the disposable worktree from the last accepted checkpoint
  on a handled failure (not per-file rollback), and must check
  HEAD-equals-checkpoint plus a clean tree/index before ever trusting
  or resuming a worktree. True crash-consistent filesystem mutation is
  explicitly not guaranteed in v1 — the claim is recovery plus
  interruption detection/rejection at the controller boundary. No
  implementation exists yet; the ADR records required Milestone 2
  failure-path testing obligations (disposal/recreation failure,
  additions, deletions, renames, unexpected dirty/index states) that
  S3's evidence does not cover.
- **S4 — executor/container isolation**: original isolation evidence
  exists on both macOS/Docker Desktop and Linux/x86_64. Commit
  `00063d4` resolved the two open questions it raised (`--memory-swap`
  now caps combined memory+swap; a Docker-confirmed OOM kill now
  classifies as `ENVIRONMENT_FAILURE`/`EXECUTOR_OOM_KILLED`), and a
  post-hardening follow-up passed all checks on both platforms. Linux
  `cap_sys_admin` remains `INCONCLUSIVE` (unrelated). Full record:
  `spikes/s4/S4_RESULT.md`.
- **S5 — interruption without orphaned containers**: spike evidence is
  complete on both macOS/arm64 and Linux/x86_64 (six scenarios total:
  five lifecycle outcomes — normal completion, cooperative
  cancellation, SIGINT, SIGTERM, SIGKILL — plus fresh-process
  reconciliation, followed by a separate second-pass idempotency check,
  all against real Docker and a real throwaway worktree; macOS evidence
  was committed at `d2a6f63`, and Linux workflow run `34783737248`
  passed with independently clean workflow diagnostics; see
  `spikes/s5/S5_RESULT.md`, status DRAFT). No S5
  mechanism (labeling, manifest/registry, locking, cancellation,
  startup reconciliation) is implemented in production, and no
  candidate architecture decision from this spike has been accepted.
- One Stage-2 spike is unstarted: Responses API strict function tools
  and multiple tool calls. (A sixth spike, JSONL replay into the first
  frontend view, is also listed in the handoff and unstarted.)

The flagship fixture repository used by slice B/C
(`tests/fixtures/retry_worker/`) is a narrow, hand-built stand-in for
this and has not yet been replaced by the empirically validated
flagship fixture repository named in `docs/PROJECT_BRIEF.md`.
