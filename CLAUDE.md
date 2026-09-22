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
  S3's evidence does not cover. **Amendment 1 (Accepted 2026-09-15)**
  makes checkpoints durably reachable through one hidden ref per
  lifecycle, `refs/codeagent/runs/<lifecycle_id>/checkpoint`, changed
  only by compare-and-swap `git update-ref --no-deref`; a commit is
  accepted only once that ref advances. Milestone 2 owns the ref
  mechanics and tests; ADR 0004 (Milestone 3) owns durable write-ahead
  and dead-run reconciliation of orphaned refs.
  **Milestone 2 slice A is implemented**: `src/codeagent/checkpoint_ref.py`
  is the trusted ref primitive — compare-and-swap create/advance/delete
  run inside a `git update-ref --stdin` transaction that re-observes the
  ref while `prepare` holds its lock (closing the symbolic-substitution
  race), every invocation strips all `GIT_*` variables and passes
  `-c core.hooksPath=/dev/null` so neither a hostile `GIT_DIR`/
  `GIT_NAMESPACE` nor a repository-configured hook can redirect it or
  run host code, object format is detected with no SHA-1 assumption,
  linked worktrees are refused, and every mutation outcome is
  classified by observation while preserving its precise cause — with
  125 focused tests. This module is deliberately **not integrated**: no
  controller, patch, lifecycle-store, reconciliation, cancellation, or
  CLI wiring yet.
  **Milestone 2 slice 2B-1 is implemented and is also deliberately
  unwired**: `checkpoint_ref.py` now publishes a `MutationOutcome`
  (`APPLIED`/`UNCHANGED`/`UNEXPECTED`/`SYMBOLIC`/`UNKNOWN`) on every
  `CheckpointRefError` — the categorical reason alone could not
  distinguish "ref confirmed still in its pre-state" from "ref state
  unknown", which are opposite decisions for a write-ahead record —
  and `src/codeagent/checkpoint_session.py` holds ADR 0004 section 5's
  `checkpoint_ref` transition record **in memory only** (ADR 0003
  Amendment 1's Milestone 2 boundary), collapsing it strictly by
  `(operation, outcome)` with no second observation.
  `TRANSACTION_CLEANUP_UNCONFIRMED` dominates every classification and
  never permits a collapse — an unconfirmed child may still hold the
  ref lock, so no observation around it is authoritative. Every
  transitional state refuses every further operation, `removing`
  included: the in-memory record cannot distinguish a
  confirmed-unchanged failure from an unknown lock outcome, so a
  deletion retry would be guessing, and ADR 0004 section 8
  reconciliation (Milestone 3) owns the fresh inspection that could.
  `new_lifecycle_id()` accepts no seed or input, which binds that
  function only — `CheckpointRef` still accepts any correctly shaped
  id, so 2B-2 must make the trusted composition root mint exclusively
  through it. 106 focused tests.
  **At the time slice 2B-1 was committed, this made no production
  lifecycle guarantee**: nothing imported `checkpoint_session`, it
  wrote no file, took no lock, emitted no event, and performed no Git
  call of its own. **`checkpoint_session` is now integrated by slice
  2B-2 below** — `RunController` constructs and drives it as a required
  collaborator. The entry gate, create/advance ordering, workspace
  ownership, worktree-before-ref teardown, event ordering and error
  identity described here remain slice 2B-1's own contribution; ADR
  0004's durable store remains Milestone 3.
  **Milestone 2 Slice 2B-2 is implemented (2026-09-17) per
  `docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`
  Amendment 2 and `docs/adr/0006-git-safety-policy-for-filters-hooks-
  and-content-fidelity.md` Amendment 4 — verified locally on macOS with
  a real Docker daemon, and confirmed on Linux CI (commit `fafefbe`,
  run [35306212472](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35306212472),
  success: 1863 passed, 1 intentional Darwin-only skip, all 3
  real-Docker tests passed, no leftover verification containers).
  SHA-256 object-format coverage is still pending.**
  - `src/codeagent/evidence.py` (new): `FilesystemEvidenceSink`
    implements the accepted durable-evidence-artifact design in full —
    strictly observational `status`+`diff` capture sharing one
    `enumerate_filter_neutralization()` result, the framed
    magic/header/payload binary format, component-aware containment,
    owner-only `0700`/`0600` permissions independent of umask, atomic
    same-directory no-replace hard-link publication, the 1 MiB hard
    bound with immediate child termination on overflow (no fabricated
    totals), and the four `EvidenceCaptured` shapes
    (complete/incomplete/durability-unconfirmed/capture-failed or
    collision). 37 tests, including real hostile external-diff/
    textconv/clean-filter/process-filter positive controls proving the
    production capture path — not a scratch probe — suppresses each.
  - `src/codeagent/_git_safety.py` gained `run_git_bounded_preview` (a
    bounded-preview counterpart to `run_git_bounded`: overflow stops
    draining, confirms the child terminated, and returns a truncated
    `complete=False` result instead of raising and discarding it;
    `run_git_bounded`'s own contract is unchanged). 12 new tests.
  - `src/codeagent/workspace.py`: `GitWorktree` gained `initial_commit`,
    `entry_gate()`, `dispose()`, `preserve()`, and a tri-state
    `active`/`disposed`/`preserved` `__exit__` dispatch. **Every
    `shutil.rmtree`/repository-wide `git worktree prune` fallback is
    removed** — an unconfirmed exact disposal now raises
    `GitWorktreeCleanupError` loudly instead of degrading to a broader
    sweep. 18 new tests, including a static AST-level proof that
    `rmtree` and `"prune"` no longer appear anywhere in the module.
  - `src/codeagent/executor.py`: `events.ContainerCleanupStatus`
    (`NOT_APPLICABLE`/`CONFIRMED_ABSENT`/`UNCONFIRMED`, no production
    default anywhere) threaded through `_attempt`/`_execute`;
    `create_attempted` is set immediately before the `docker create`
    call, so a bare launch failure still requires cleanup confirmation
    rather than being reported `NOT_APPLICABLE`. `NOT_APPLICABLE`
    itself is not reachable through any path in today's `_attempt` (no
    pre-create validation exists yet) — verified directly against the
    pure classification function instead of fabricating a Docker
    scenario for it.
  - `src/codeagent/controller.py`: `RunConfig.lifecycle_id` required
    and validated (32 lowercase hex); `initial_checkpoint_id` removed
    (the workspace's `initial_commit` is now the sole authority, and
    seeds `_last_checkpoint_id` at construction). `RunController` takes
    three new required collaborators (`workspace`, `session`,
    `evidence_sink`). `_dispatch_apply_patch` now runs the full ADR
    0003 Amendment 1/2 order (entry gate → lazy `establish()` only from
    `ABSENT` → apply → approved-path postcondition → `advance()` → only
    then `ToolCompleted(success=True)`/`CheckpointCreated`/
    `PatchApplied`), with `_map_checkpoint_error` implementing the
    exact reason+`MutationOutcome` → `ErrorCode` mapping. A new
    `_terminate` method replaces every direct `_transition`+`_finish`
    call site: it always attempts evidence capture first, then gates
    worktree disposal/checkpoint-ref deletion on verifier-cleanup and
    disposal confirmation, then applies the six-tier terminal
    precedence (any cleanup unconfirmed → evidence failure → the
    original result), overriding the trigger to `UNRECOVERABLE_ERROR`
    when precedence selects anything but the original outcome
    (`domain.TerminalReason` has no separate slot for "succeeded but
    teardown didn't").
  - `tests/support/fakes.py` gained `FakeWorkspace`,
    `FakeCheckpointSession` (raising the real
    `CheckpointRefError`/`CheckpointSessionError` types so the
    controller's mapping logic is exercised identically to the real
    collaborator), and `FakeEvidenceSink`.
  - `tests/integration/test_slice_b.py` and `test_slice_c.py` now wire
    real `CheckpointRef`/`CheckpointSession`/`FilesystemEvidenceSink`
    end to end (the latter with a real Docker daemon): both confirm the
    checkpoint ref and worktree are genuinely absent after teardown,
    the evidence artifact is genuinely published with correct framing/
    hash/content, and (slice C) no container is left behind.
  - **Full suite: 1864 passed** (current; up from 1804 before this
    slice, through two subsequent correction passes — see
    `ENGINEERING_LOG.md`'s "correction pass" and "ambient-symlink
    hardening" entries for the intermediate totals those runs actually
    produced at the time), including the 3 real-Docker tests with
    `CODEAGENT_REQUIRE_DOCKER=1` — all on macOS/Git 2.54.0.
    `src/codeagent/evidence.py`'s ambient-symlink exception
    (`/tmp`/`/var`/`/etc`) is now gated to a verified macOS/Darwin
    target match, never trusted by name alone or on another platform;
    `tests/unit/test_evidence.py` is now 37 tests. Linux CI validation
    passed (see the run cited above). **Not yet done**: SHA-256
    object-format exercise (ADR 0003's own
    required-test list); a dedicated test for every scenario in the
    original 2B-2 task's exhaustive list (several are exercised only
    incidentally via the real end-to-end tests, not each via its own
    dedicated unit test); ADR 0004's durable lifecycle store, locks, reconciliation,
    abandonment, and CLI/frontend work remain entirely Milestone 3, as
    already scoped.
  **`docs/adr/0006-git-safety-policy-for-filters-hooks-and-content-fidelity.md`
  (Accepted) is now implemented for `src/codeagent/_git_safety.py` and
  `src/codeagent/workspace.py`**: the shared foundation module
  (Git >= 2.45 / `--no-lazy-fetch` preflight, sanitized `GIT_*`
  environment, the hardened baseline argv, bounded/chunked/NUL-safe
  filter-driver enumeration and tracked-path attribute inspection, and
  the driver-set-dependent `filter` safety rule) is wired into
  `GitWorktree`. Worktree creation now registers with `--no-checkout`,
  populates the index via `read-tree`, inspects every tracked path's
  `filter` attribute, and refuses the whole run before a single file
  is materialized if any path is unsafe or ambiguous — only then does a
  real, hardened `checkout` run. `snapshot_source()`'s `status` call
  carries the enumerate-and-neutralize backstop. `git worktree add`'s
  outcome is treated as potentially mutating regardless of how it
  concludes (nonzero, an infrastructure error, or an ambiguous
  report): a failure at any point after the attempt performs exact
  registration removal plus tempdir cleanup, confirmed by observation,
  with **no repository-wide sweep and no `prune`** on this new
  enter-time path; an unconfirmed cleanup raises a sanitized
  `GitWorktreeCleanupError` chaining the original failure as its cause
  rather than silently dropping it. Failure messages never contain raw
  stderr, argv, repository/worktree paths, or environment values.
  Verified with 116 focused `_git_safety` tests and 49 focused
  `workspace` tests (1170 in the full suite), including real hostile
  hooks/filters (clean/smudge/process), the `unset`/`unspecified`
  driver-name collision (finding 16), ambient global/system-level
  filter configuration, a real `git://` daemon partial-clone
  lazy-fetch refusal, and hostile `GIT_*` environment variables — on
  macOS only; **Linux CI validation is still pending** as of this
  commit.
  **`patch.py`'s ADR 0006 hardening slice is implemented and passing
  its own tests locally (macOS, Git 2.54.0) — see ADR 0006
  Amendments 1–3 — with Linux CI validation pending.** Two correction
  passes found and fixed real gaps before this slice was considered
  final: attribute checks initially covering only `filter` (now the
  full six ADR 0006 attributes); several call sites that could let a
  raw `GitSafetyError` escape `apply()` as an unhandled exception (now
  all wrapped and mapped); a silent-nonzero-exit `run_git_bounded`
  contract (now fails categorically); `list_tracked_paths` claiming
  "bounded" while using unbounded `capture_output` (now genuinely
  bounded); a real deadlock in the bounded subprocess cleanup path from
  closing a child's stdin concurrently with an in-flight writer-thread
  `write()` call, and a second bug joining a writer thread that never
  started (both fixed); and unsanitized
  `Path.resolve`/`is_symlink`/`is_file`/`UnicodeEncodeError` failures
  (now sanitized). Most significantly (Amendment 3): a nested
  `.gitattributes` file's own attribute classification can be masked by
  that file's own staged content — a real, reproduced probe — so there
  is currently no trusted way to validate a `.gitattributes` target's
  safety independently of its own content. **`.gitattributes` patch
  targets (top-level or nested) are therefore refused categorically**
  (`PATCH_UNSUPPORTED_GIT_SUBSTRATE`) before any mutation, not claimed
  as supported. Verified with 184 focused `_git_safety` tests and 43
  focused `patch` tests (1300 in the full suite) on macOS. Until Linux
  CI passes, T-M3 and ADR 0006 remain **not complete project-wide**,
  and checkpoint-ref integration with patch application still waits on
  that work.
  `GitWorktree.__exit__`'s legacy `shutil.rmtree` +
  repository-wide `git worktree prune` fallback (used only when the
  ordinary `git worktree remove` itself fails) is unchanged and remains
  an open ADR 0004 gap, separate from the new, stricter enter-time
  cleanup path.
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
  `spikes/s5/S5_RESULT.md`, evidence complete). Author decisions are
  **Accepted** in `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`
  (lifecycle IDs, state root and repository namespaces, ownership
  labels, locks, attribution, container → worktree → checkpoint-ref
  cleanup, fail-closed pre-run reconciliation, explicit reconcile and
  abandonment) and `docs/adr/0005-cancellation-and-signal-ownership.md`
  (cancellation token, entrypoint-owned SIGINT/SIGTERM handlers,
  `VerificationOutcome.CANCELLED`). **None of it is implemented in
  production yet.** Order: Milestone 2 first — ADR 0003's hidden
  checkpoint-ref amendment is now Accepted — then these
  mechanisms as Milestone 3 lifecycle work. S5 spike code is not reused
  in production.
- **Milestone 3 Slice 3A-1** (trusted lifecycle state-root, repository
  identity, and repository/generic lock primitives), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 1 (Accepted 2026-09-18)", **is implemented, locally
  validated on macOS (2026-09-22), and confirmed on GitHub-hosted
  Linux CI** (commit `8aefa5f`, run
  [35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790),
  `ubuntu-24.04` x86_64, Python 3.12, success — see the 3A-2 entry
  below for the exact totals this same run also covers). It is still
  **not wired into production**. `src/codeagent/_lifecycle_fs.py`
  (shared fd-based filesystem-safety primitives, including
  `resolve_state_root_path()`), `state_root.py` (state-root init/
  validation), `state_locks.py` (the generic verified nonblocking lock
  primitive plus `acquire_repository_lock`), and `repo_identity.py`
  (trusted repository identity/context discovery and `repo.json`
  persistence) all exist, each with a matching focused test module. A
  correction pass fixed a real in-process test regression — a shared-
  module `importlib.reload` that desynchronized exception-class
  identity across these four modules once collected together, plus
  several mid-test `monkeypatch.undo()` sites — with a subprocess-
  isolated capability test and `monkeypatch.context()` scoping; that
  specific fix changed no production behavior. Separately, earlier
  hardening passes in this same slice *did* change production
  behavior: fail-closed operation-time capability loading (the
  `fcntl` module is now lazily imported and never crashes the module
  at import time), descriptor ownership/cleanup, private-file
  validation, lock-cleanup classification, state-root probing, and
  `repo.json` validation were all corrected before final verification.
  Verified: the four focused test files collected and passed together
  as 199/199 in both forward and reverse file order; the full suite
  passed 2063/2063 (including the 3 real-Docker tests with
  `CODEAGENT_REQUIRE_DOCKER=1`); and a post-run check confirmed no
  leftover `codeagent-verify` containers, extra worktrees,
  `refs/codeagent` refs, child processes, or temp state roots.
  **Unwired**: no `RunController` or CLI integration exists yet —
  Slice 3A-2 (below) implements the lifecycle-lock wrapper itself, but
  wiring either slice's primitives into a real entry point remains
  later Milestone 3 work. A focused security review of these four
  files, limited to high/medium exploitable findings at an >=8/10
  reporting threshold, produced no reportable finding — two candidates
  were independently rejected at 3/10 and 2/10; this is not a claim
  that the slice or codebase is vulnerability-free or fully audited.
  See `ENGINEERING_LOG.md`'s dated entry for both candidates and why
  each was rejected.
- **Milestone 3 Slice 3A-2** (durable lifecycle storage), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s §16
  steps 8–11, **is implemented, locally validated on macOS
  (2026-09-22), and confirmed on GitHub-hosted Linux CI** (commit
  `8aefa5f`, run
  [35794177790](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35794177790),
  `ubuntu-24.04` x86_64, Python 3.12, success — see below for the
  exact totals). It is still **not wired into production**.
  `src/codeagent/lifecycle_store.py` implements the exact
  accepted composition — `check_git_preflight()`, repository discovery,
  the trusted state root, the repository lock, `repo.json`, a fresh
  exclusive `runs/<lifecycle_id>/` directory, the lifecycle lock, and
  an atomically published initial `PREPARING` `lifecycle.json` — all
  strictly before any Docker container, disposable worktree, or
  checkpoint ref exists. Two small additions to Slice 3A-1's own
  modules back it: `_lifecycle_fs.py` gained
  `create_exclusive_directory_at()` (refuses, never adopts, a
  pre-existing entry) and `publish_private_file_atomically_at()`
  (same-directory temp file, write, `fsync`, `os.replace`, directory
  `fsync`, exact-temp-file-only cleanup on failure — with a dedicated
  `LifecycleFsFailure.INSTALLED_DURABILITY_UNCONFIRMED` reason so a
  post-replace directory-`fsync` failure is never conflated with a
  pre-installation `FSYNC_FAILED`, a real ambiguity a review caught and
  this slice's own correction pass fixed); `state_locks.py` gained
  `acquire_lifecycle_lock()`. The initial `lifecycle.json`'s
  `checkpoint_ref` field reuses `checkpoint_session.CheckpointIntent`/
  `CheckpointTransition` directly rather than a second copy of ADR
  0004 §5's combination table. `LifecycleLease` retains the state-root
  descriptor, run-directory descriptor, repository lock, and lifecycle
  lock for the caller's lifetime, releasing them on `close()` in the
  exact required order (lifecycle lock, run-directory descriptor,
  repository lock, state-root descriptor), attempting every stage
  regardless of an earlier stage's outcome.
  `validate_lifecycle_json_schema()` is object-format-aware for every
  non-null persisted Git object id in `checkpoint_ref` (exact
  lowercase-hex SHA-1/SHA-256 matching the repository's actual
  format, never an arbitrary nonempty placeholder or the all-zero OID
  — the latter is refused at the shared `checkpoint_session.
  _require_oid_shape()` boundary `CheckpointTransition` itself uses,
  per ADR 0004 §5's "the zero OID appears only in Git argv," not by a
  second check in `lifecycle_store.py`), and is **deliberately
  narrowed** to the one `worktree`/`failure` shape this slice actually
  produces (`absent`/`null`) rather than describing a partial future
  validator as complete — the ADR does not yet give those two fields a
  combination table as precise as containers' or checkpoint_ref's.
  Verified: `py_compile` on all seven changed/new files (including
  `checkpoint_session.py`); the directly affected test files
  (`test_checkpoint_session.py`, `test_checkpoint_ref.py`,
  `test_lifecycle_store.py`, `test_lifecycle_fs.py`,
  `test_state_locks.py`) collected and passed together, 474 passed;
  the five focused test files (`test_lifecycle_fs.py`,
  `test_repo_identity.py`, `test_state_locks.py`, `test_state_root.py`,
  `test_lifecycle_store.py`) collected and passed together, 311
  passed, in both forward and reverse file order; the full local
  suite (macOS): 2,180 passed. With a real Docker daemon and
  `CODEAGENT_REQUIRE_DOCKER=1` (macOS), the 3 dedicated real-Docker
  tests: 3 passed, 0 skipped; the complete suite: 2,180 passed, 0
  skipped, with no leftover `codeagent-verify` containers afterward.
  **GitHub-hosted Linux CI** (commit `8aefa5f`, run `35794177790`,
  `ubuntu-24.04` x86_64, Python 3.12): the pinned verification image
  was pulled and confirmed `linux/amd64`; the dedicated mandatory
  real-Docker step, 3 passed, no skips; the complete-suite step, 2,177
  passed, 3 skipped — those 3 skips are exactly the real-Docker tests
  already exercised for real in the dedicated step immediately before
  it, not a Docker-unavailability skip; no leftover
  `codeagent-verify` containers afterward. This is GitHub-hosted
  `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
  ARM64 claim. A real two-process test confirms both the repository
  lock and the lifecycle lock are cross-process exclusive, and a real
  SIGKILL test confirms a fresh process can still acquire both locks
  afterward, creating a new, separate lifecycle_id/run directory
  rather than adopting the dead run's own directory. **Unwired**: no
  `RunController` or CLI integration exists yet. **Not implemented**:
  automatic pre-run reconciliation, abandonment, the maintenance
  trace, and any container/worktree/checkpoint-ref attribution or
  mutation (a static AST-based test confirms none is reachable from
  this module) — all later Milestone 3 work. **T-E1 is not mitigated
  by this slice alone** (`docs/threat-model.md`): nothing yet calls
  `prepare_lifecycle()` before a real run starts.
- One Stage-2 spike is unstarted: Responses API strict function tools
  and multiple tool calls. (A sixth spike, JSONL replay into the first
  frontend view, is also listed in the handoff and unstarted.)

The flagship fixture repository used by slice B/C
(`tests/fixtures/retry_worker/`) is a narrow, hand-built stand-in for
this and has not yet been replaced by the empirically validated
flagship fixture repository named in `docs/PROJECT_BRIEF.md`.
