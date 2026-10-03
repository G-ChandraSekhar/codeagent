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
  SHA-256 object-format coverage was still pending at this slice's own
  completion (2026-09-17). **Status correction (2026-10-02, documentation-
  only reconciliation, not a change to this historical slice's own
  record)**: real SHA-256-repository coverage has since been added by
  later slices and now exists — confirmed by direct inspection of
  `tests/unit/test_checkpoint_ref.py::test_sha256_repository_object_format`
  (a real `git init --object-format=sha256` repository, exercising
  `CheckpointRef`'s full `create`/`advance`/`delete` compare-and-swap
  cycle against it), `tests/unit/test_git_safety.py::
  test_detect_object_format_sha256` (real sha256-repository object-
  format detection), `tests/unit/test_repo_identity.py::
  test_discover_repository_identity_sha256_or_skipped` (real sha256
  repository, full identity discovery), `tests/unit/
  test_lifecycle_store.py::test_prepare_lifecycle_sha256_or_skipped`
  (real sha256 repository, the complete `prepare_lifecycle()` flow),
  and `tests/unit/test_patch.py::
  test_apply_runs_end_to_end_in_a_sha256_repository` (real sha256
  repository, a full patch-apply end-to-end run) — each with a graceful
  `pytest.skip()` on a Git build lacking `--object-format=sha256`
  support. This is distinct from the schema/value-shape tests added by
  the worktree-attribution substrate slice (e.g. `test_worktree_
  present_valid_sha256`, `test_worktree_sha1_length_refused_against_
  sha256_repo`), which pass `object_format="sha256"` and construct
  64-character synthetic OIDs (`"b" * 64`) without any real repository
  — those prove the validator's own logic, not real-repository
  evidence, and are not themselves being cited as closing this gap.**
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
    passed (see the run cited above). **Not yet done at this slice's
    own completion**: SHA-256 object-format exercise (ADR 0003's own
    required-test list) — **status correction (2026-10-02,
    documentation-only): this gap has since been closed by later
    slices; see the corresponding correction on this file's earlier
    SHA-256 statement above for the exact current test files.** Also
    not yet done at this slice's own completion: a dedicated test for
    every scenario in the
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
  passed, 3 skipped. **Corrected 2026-09-23**: this step also ran with
  `CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon (confirmed
  from `.github/workflows/ci.yml`, unmodified since its single
  introducing commit), so those 3 skips were **not** the real-Docker
  tests — those had already passed again within this same step. The
  narrowest claim this run's own log supports is: three platform/host-
  specific tests skipped; they were not the real-Docker tests. Source
  inspection (not proven from this run's own log, which does not expose
  skip names) identifies the likely candidates as three tests that
  unconditionally skip on a Linux, case-sensitive-filesystem runner:
  `test_evidence.py`'s Darwin-only ambient-`/tmp`-symlink test,
  `test_lifecycle_fs.py`'s Darwin-only case-canonicalization test, and
  `test_repo_identity.py`'s case-insensitive-filesystem-dependent alias
  test. No leftover `codeagent-verify` containers afterward. This is
  GitHub-hosted `ubuntu-24.04` x86_64 evidence specifically, not a
  general Linux or ARM64 claim. A real two-process test confirms both
  the repository lock and the lifecycle lock are cross-process
  exclusive, and a real
  SIGKILL test confirms a fresh process can still acquire both locks
  afterward, creating a new, separate lifecycle_id/run directory
  rather than adopting the dead run's own directory. **Not
  implemented** (at the time this bullet was written; see the 3B-1
  bullet below for what has since been added): automatic pre-run
  reconciliation, abandonment, the maintenance trace, and any
  container/worktree/checkpoint-ref attribution or mutation (a static
  AST-based test confirms none is reachable from this module).
- **Milestone 3 Slice 3B-1** (initial-shape automatic reconciliation),
  per `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  Amendment 2, **is implemented and locally validated on macOS
  (2026-09-22)**. `src/codeagent/reconciliation.py` implements
  `reconcile_repository()`, wired into `lifecycle_store.
  prepare_lifecycle()` at the exact documented insertion point — after
  `load_or_create_repo_json()`, before a new `lifecycle_id` is ever
  minted — while the repository lock is already held. It recognizes
  and reconciles only entries whose durable state is `PREPARING` or
  `RECONCILING` with the complete initial all-absent attribution shape
  (both containers, worktree, and `checkpoint_ref` all `absent`,
  `failure=null`): after a real, freshly performed `docker ps -a`
  listing, a real `git worktree list --porcelain` listing, and a real
  `CheckpointRef.observe()` all confirm absence, it writes
  `RECONCILING → RECONCILED`. It **never removes or mutates** a
  container, worktree, or checkpoint ref, and it never writes
  `LifecycleState.RECONCILIATION_FAILED` (reserved for a later slice
  with a real external mutation to give up on). A positively observed
  wrong state (malformed name, symlink, wrong type/owner/permissions,
  an unrecognized inner entry, a non-absent or corrupt/identity-
  mismatched projection, or a resource actually found present) is
  `REFUSED`; a genuine inspection or infrastructure failure is
  `SUBSTRATE_UNAVAILABLE`; a busy lifecycle lock is `SKIPPED_ACTIVE`; a
  recognized clean-final entry is `SKIPPED_TERMINAL` with zero lock or
  inspection calls. Any of these blocks admission of the new run
  (`LifecycleStoreError(RECONCILIATION_BLOCKED)`) before a lifecycle_id
  is minted or a run directory is created. `lifecycle_store.py` gained
  `load_lifecycle_projection()` (fd-relative, `O_NOFOLLOW`, exact
  private-file validation including link-count, size-bounded, strict
  JSON, object-format-aware schema validation, trusted-identity
  comparison) and `_publish_projection_state()` (reusing the existing
  atomic-publish primitive for the `RECONCILING`/`RECONCILED`
  transitions). A one-exclusive-file-per-pass maintenance trace
  (`repos/<repo_key>/maintenance/<maintenance_id>.jsonl`) records
  `ReconciliationStarted`/`ReconciliationEntryRecorded`/
  `ReconciliationFinished` events, each individually `fsync`ed;
  inability to create, write, `fsync`, directory-`fsync`, or close this
  trace also blocks admission in this slice (no controller, CLI, or
  event sink yet exists through which an in-memory warning could
  otherwise reach an operator). `reconciliation.attempts_total`
  increments exactly once per fresh (non-resuming) `RECONCILING`
  transition; a resumed `RECONCILING` (found by a later pass) redoes
  inspection from scratch without incrementing again. A subsequent
  correction pass (2026-09-22) fixed four confirmed defects without
  changing this scope or vocabulary: unbounded Docker/Git inspection
  (both now byte-capped with confirmed child termination on timeout or
  overflow; worktree listing now NUL-delimited via `-z`, immune to a
  registered path containing a newline); untrusted recognized inner
  entries (`lifecycle.json`/`lifecycle.lock`/temp-leftover are now each
  fd-relative, no-follow validated before being trusted, and a
  recognized leftover's presence now actually reaches the maintenance
  trace's `has_recognized_temp_leftover` field); three raw-error-
  escape/descriptor-leak bugs in the maintenance-trace open path and
  the pass-level directory opens; and an eroded REFUSED-vs-
  SUBSTRATE_UNAVAILABLE distinction at three boundaries, now restored
  via two shared classification helpers. See `ENGINEERING_LOG.md`'s
  dated correction-pass entry for exact detail. Verified: the
  eight-file focused set (`test_lifecycle_fs.py`, `test_repo_
  identity.py`, `test_state_locks.py`, `test_state_root.py`,
  `test_lifecycle_store.py`, `test_checkpoint_session.py`,
  `test_checkpoint_ref.py`, `test_reconciliation.py`) collected and
  passed together, 616 passed, in both forward and reverse file order;
  the full local suite (macOS), with a real Docker daemon and
  `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
  dedicated real-Docker tests (`tests/integration/test_slice_c.py`), 3
  passed, 0 skipped; the complete suite, 2,249 passed, 0 skipped; no
  leftover `codeagent-verify` containers, extra worktrees,
  `refs/codeagent` refs, child processes, or temp state roots
  afterward. A real cross-process test SIGKILLs a process immediately
  after it durably publishes its initial `PREPARING` projection; a
  fresh process's own `prepare_lifecycle()` call then performs genuine
  Docker/Git/ref absence inspection, reconciles the dead entry to
  `RECONCILED`, and only then mints its own separate lifecycle_id. A
  second real test confirms `SKIPPED_ACTIVE` via a genuinely
  inconsistent fixture (a process holding only the lifecycle lock,
  never the repository lock — the only way to reach that state, since
  a correct owner always holds the repository lock too while it holds
  the lifecycle lock). **Not implemented**: any container/worktree/
  checkpoint-ref *removal*, abandonment, the explicit `codeagent
  reconcile`/`--abandon` CLI, retention, and `RunController`/CLI wiring
  of `prepare_lifecycle()` itself — all later Milestone 3 work.
  **Named residual risk**: a crash inside `prepare_lifecycle()` between
  run-directory creation and initial-projection publish leaves a run
  directory with no valid `lifecycle.json`, which this slice correctly
  refuses (`REFUSED`) rather than adopts or repairs — that repository
  then stays blocked until abandonment (not yet implemented) exists;
  three pre-existing `test_lifecycle_store.py` tests that previously
  asserted "a fresh retry succeeds" after leaving exactly this leftover
  state were updated to assert the new, correct `RECONCILIATION_BLOCKED`
  outcome instead. **T-E1 is now partially mitigated**
  (`docs/threat-model.md`): a dead prior run in the current repository
  is now recovered automatically before a new run is admitted, but
  nothing yet calls `prepare_lifecycle()` before a real run starts, so
  concurrent-run refusal still depends only on the repository lock's
  ordinary `BUSY` behavior. **GitHub-hosted Linux CI confirmed** (commit
  `048314e8713777f3401a2e64445e3e8da9507cc1`, run
  [35814528028](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35814528028),
  `ubuntu-24.04` x86_64, Python 3.12, success): the dedicated mandatory
  real-Docker step, 3 passed, 0 skipped; the complete-suite step, 2,246
  passed, 3 skipped. **Corrected 2026-09-23**: this step also ran with
  `CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
  skips were **not** the real-Docker tests, which had already passed
  again within this same step. Narrowest supportable claim: three
  platform/host-specific tests skipped; they were not the real-Docker
  tests. Source inspection (not CI-log-proven) identifies the likely
  candidates as the same three Linux-unconditional skips named in the
  Slice 3A-1/3A-2 entry above. No leftover `codeagent-verify`
  containers afterward. This is GitHub-hosted `ubuntu-24.04` x86_64
  evidence specifically, not a general Linux or ARM64 claim, and is
  implementation/test-suite evidence only — it does not constitute or
  substitute for a security review.
- **Milestone 3 Slice 3B-2** (locked, authoritative lifecycle-projection
  writer), per `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  Amendment 3, **is implemented and locally validated on macOS
  (2026-09-23)**. `LifecycleLease` gained a trusted `object_format`
  field (from `RepositoryIdentity.object_format`, set once in
  `prepare_lifecycle()`) and `open_projection_writer()`, the only
  sanctioned way to obtain a `_LifecycleProjectionWriter` — it refuses
  an incomplete or already-`close()`d lease categorically (no raw
  `TypeError`/`AttributeError`/invalid-descriptor error escapes) and
  performs the same complete lock-scope check (held, `LIFECYCLE`-kind,
  exact `repo_key`/`lifecycle_id` match) every write method uses before
  the caller ever gets a writer. Every write method loads and fully
  validates the currently installed authoritative projection fresh,
  refuses a stale caller-supplied `expected` before any publication I/O
  (`STALE_EXPECTED_PROJECTION`, deliberately distinct from
  `ILLEGAL_TRANSITION`), and never overwrites a corrupt or identity-
  mismatched file. `advance_lifecycle_state()` implements the owner
  state graph (`PREPARING→ACTIVE→CLEANING→COMPLETE`, the last edge
  gated by the shared `is_projection_fully_absent_shape()` clean-final
  predicate — moved from `reconciliation.py`, now public in
  `lifecycle_store.py` and imported explicitly, not accidentally
  re-exported); `RECONCILING`/`RECONCILED`/`RECONCILIATION_FAILED`
  remain refused unconditionally, including identical-state requests,
  since those stay `reconciliation.py`'s own private write path.
  `record_container_transition()` implements ADR 0004 §7's persisted-
  combination table per role, including the Amendment 3 clarification
  that `(creating,null)→(absent,null)` is legal only when the caller
  has independently confirmed, by real Docker inspection this writer
  never performs itself, that no container was ever created.
  `record_checkpoint_ref_transition()` adds full cross-transition SHA
  continuity beyond `CheckpointTransition`'s own per-record shape
  validation (e.g. `advancing→present` must land on either the prior
  or the proposed SHA, never an arbitrary third value), trusting
  `transition` as the already-decided output of `checkpoint_session.
  CheckpointSession`'s own collapse logic. `PROJECTION_DURABILITY_UNCONFIRMED`
  requires the new explicit `refresh()` recovery read before any
  further write — the writer never blindly retries a stale `expected`
  itself. **`checkpoint_session.py` is deliberately untouched**:
  direct inspection confirmed `establish()`/`advance()`/`delete()` set
  their transitional intent and call `CheckpointRef` on the very next
  line with no seam between them, so this writer's checkpoint-ref
  method, while fully correct and tested standalone, is **not yet a
  usable durable write-ahead boundary in production** — nothing can
  call it at the ADR-required moment for a real checkpoint-ref
  mutation today; that seam is later Milestone 3 work. Populated
  `failure` and non-absent worktree writing remain deferred, unchanged
  from Slice 3A-2/3B-1's own narrowing (both still refused at the
  schema-validation layer before this slice's transition logic would
  ever see them). A correction pass (2026-09-23) fixed three real gaps
  found by review before this slice was considered final: neither
  resource-transition method gated on the authoritative lifecycle
  state, so a `COMPLETE` or reconciler-owned (`RECONCILING`/
  `RECONCILED`/`RECONCILIATION_FAILED`) projection could still be
  dirtied by a container or checkpoint-ref transition — including an
  exact no-op — violating the clean-final rule and I11/I15; both
  methods now require `PREPARING`/`ACTIVE`/`CLEANING` immediately after
  the lock/stale checks and before any resource-shape or no-op logic.
  `record_container_transition()`'s request-shape validation ran before
  `_require_current()`, so an invalid `role` could mask a wrong lock
  scope or a stale `expected`; call order is now uniform across all
  three write methods (lock → authoritative read/stale check → state
  gate → request validation → edge → publish).
  `record_checkpoint_ref_transition()` accessed `transition`'s fields
  without a type check, so `None` or a wrong type raised a raw
  `AttributeError` instead of a sanitized `LifecycleStoreError`; now
  sanitized (`ILLEGAL_TRANSITION`) immediately after the state gate.
  Verified (post-correction-pass totals): the eight-file focused set
  collected and passed together, 666 passed, in both forward and
  reverse file order; the full local suite (macOS), with a real Docker
  daemon and `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a
  failure): the 3 dedicated real-Docker tests, 3 passed, 0 skipped; the
  complete suite, 2,299 passed, 0 skipped; no leftover
  `codeagent-verify` containers,
  extra worktrees, `refs/codeagent` refs, child processes, or temp
  state roots afterward. **Not implemented**: any Docker/Git call of
  any kind, `workspace.py`/`executor.py` changes, controller/CLI
  wiring, resource removal, abandonment — all later Milestone 3 work.
  This slice performs no Docker operations of its own; T-E1's
  mitigation status is unchanged (nothing here is wired into a real
  run). **GitHub-hosted Linux CI confirmed** (commit
  `0bf66f65b8cbe37ea897af3eb00ca8741522a8da`, run
  [35826244500](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35826244500),
  `ubuntu-24.04` x86_64, Python 3.12, success): the dedicated mandatory
  real-Docker step, 3 passed, 0 skipped; the complete-suite step, 2,296
  passed, 3 skipped. **Corrected 2026-09-23**: this step also ran with
  `CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
  skips were **not** the real-Docker tests, which had already passed
  again within this same step. Narrowest supportable claim: three
  platform/host-specific tests skipped; they were not the real-Docker
  tests. Source inspection (not CI-log-proven) identifies the likely
  candidates as the same three Linux-unconditional skips named in the
  Slice 3A-1/3A-2 entry above. No leftover `codeagent-verify`
  containers afterward. This is GitHub-hosted `ubuntu-24.04` x86_64
  evidence specifically, not a general Linux or ARM64 claim, and is
  implementation/test-suite evidence only — it does not constitute or
  substitute for a security review.
- **Milestone 3 Slice 3B-3** (durable checkpoint-ref transition-
  publication seam), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  Amendment 4, **is implemented and locally validated on macOS
  (2026-09-23)**. `checkpoint_session.py` gains a structural
  `CheckpointTransitionPublisher` Protocol (`publish(transition) ->
  None`) and an optional, keyword-only `transition_publisher`
  constructor parameter on `CheckpointSession`, defaulting to `None` —
  every existing caller remains source-compatible and behaviorally
  unchanged when the publisher is omitted, and the pre-existing
  behavioral tests in `test_checkpoint_session.py` continue to pass
  (the file's own static import-boundary assertion was deliberately
  updated to admit the new `typing` import).
  Assignment-first ordering is preserved deliberately: each
  `establish()`/`advance()`/`delete()` transitional intent is assigned
  to `self._transition` and published *before* the corresponding
  `CheckpointRef` mutation runs — if publication raises there, the Git
  call is never reached, the exception propagates unchanged, and the
  in-memory transition stays exactly the transitional value just
  assigned. After a confirmed Git outcome, the collapse is assigned and
  published; a failure there doesn't undo the Git result, and the
  in-memory transition stays the decided collapse regardless. For
  `establish()`/`advance()`'s confirmed-`UNCHANGED` recovery path, the
  recovery collapse is published before the original `CheckpointRefError`
  is re-raised; if that publication itself fails, its exception is
  raised **explicitly** `from` the original error (deliberate chaining,
  not incidental `__context__`) — named **projection-consistency
  failure dominance**, a distinct rule from this repository's existing
  cleanup-dominance convention. `lifecycle_store.py` gains
  `LifecycleCheckpointRefPublisher`, wrapping a
  `_LifecycleProjectionWriter` and tracking its own `current` expected
  `LifecycleProjection`: `publish()` does not catch or translate
  anything, so `current` replaces its stored value only after a
  confirmed successful write and every `LifecycleStoreError` the writer
  or its fresh authoritative-projection load can raise — including but
  not limited to `STALE_EXPECTED_PROJECTION`, `WRONG_LOCK_SCOPE`,
  `ILLEGAL_TRANSITION`, both publication-failure reasons,
  `CLEANUP_UNCONFIRMED`, `SCHEMA_INVALID`, and `SUBSTRATE_UNAVAILABLE` —
  propagates unchanged with `current` left untouched,
  `PROJECTION_DURABILITY_UNCONFIRMED` never silently treated as
  success; an explicit `refresh()` method
  (never called automatically) recovers the currently installed
  authoritative projection after a durability-unconfirmed result.
  Neither side retries anything automatically. Proven real: a full
  end-to-end integration test uses a real temporary repo, real
  `prepare_lifecycle()`, a real lease/writer/adapter, a real
  `CheckpointRef`, and a real `CheckpointSession` — `establish()`/
  `advance()`/`delete()` each durably publish, and the durable
  `lifecycle.json` is reloaded and compared against the real ref's
  actual state after each call, with no mocking of the writer anywhere
  in that test. Verified: `test_checkpoint_session.py` alone, 133
  passed (up from 111); `test_lifecycle_store.py` +
  `test_checkpoint_session.py` + `test_reconciliation.py` together, 349
  passed; the eight-file focused set collected and passed together,
  696 passed, in both forward and reverse file order; the full local
  suite (macOS), with a real Docker daemon and
  `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
  dedicated real-Docker tests, 3 passed, 0 skipped; the complete suite,
  2,329 passed, 0 skipped; no leftover `codeagent-verify` containers,
  extra worktrees, `refs/codeagent` refs, child processes, or temp
  state roots afterward. **Not implemented**: any `RunController`
  wiring or error translation (the controller today catches only
  `CheckpointRefError`/`CheckpointSessionError`; a publisher's
  `LifecycleStoreError` is a new family future wiring must explicitly
  translate — this slice does not claim readiness for that
  integration), `executor.py`/`workspace.py` changes, container/
  worktree writing, resource removal, abandonment, CLI. No new legal
  checkpoint-ref transition edge — Amendment 3's tables are unchanged.
  `docs/threat-model.md` is unchanged: nothing here is wired into a
  real run. This slice performs no Docker operations of its own; T-E1's
  mitigation status is unchanged. **GitHub-hosted Linux CI confirmed**
  (commit `bc8cb770bcfeea9a8161c102536e9b69896f24ef`, run
  [35889103564](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35889103564),
  `ubuntu-24.04` x86_64, Python 3.12, success): the pinned verification
  image was pulled and confirmed `linux/amd64`; the dedicated mandatory
  real-Docker step, 3 passed, 0 skipped; the complete-suite step, 2,326
  passed, 3 skipped. **Corrected 2026-09-23**: this step also ran with
  `CODEAGENT_REQUIRE_DOCKER=1` against a ready Docker daemon, so those 3
  skips were **not** the real-Docker tests, which had already passed
  again within this same step. Narrowest supportable claim: three
  platform/host-specific tests skipped; they were not the real-Docker
  tests. Source inspection (not CI-log-proven) identifies the likely
  candidates as the same three Linux-unconditional skips named in the
  Slice 3A-1/3A-2 entry above. No leftover `codeagent-verify`
  containers afterward. This is GitHub-hosted `ubuntu-24.04` x86_64
  evidence specifically, not a general Linux or ARM64 claim, and is
  implementation/test-suite evidence only — it does not constitute or
  substitute for a security review.
- **Milestone 3 Slice 3B-4** (bounded Docker control-plane execution
  and validated container-ID capture), the smallest dependency-correct
  prerequisite identified before any complete lifecycle-aware container
  producer slice, **is implemented and locally validated on macOS
  (2026-09-23)**. It introduces **no lifecycle publisher, no
  lifecycle-store adapter, no deterministic lifecycle-derived
  container name, no ownership label, and no controller wiring** —
  container naming remains the legacy UUID-suffixed scheme
  (`codeagent-verify-<label>-<uuid12>`), and no lifecycle projection is
  written anywhere in `executor.py`. New shared module
  `src/codeagent/_bounded_subprocess.py`: `run_bounded_stdout()` owns
  the complete launch/monitor/read/wait/kill/confirm lifecycle for a
  structured-argv, no-shell, no-stdin subprocess behind one call,
  rather than exposing separate drain/kill primitives a caller could
  combine incorrectly — the same discipline `codeagent._git_safety`'s
  own `_read_bounded` already established for Git subprocesses,
  generalized here. Never returns truncated output as complete; a
  monitoring failure, timeout, or output-bound overflow always kills
  the child and confirms it was reaped before raising
  `BoundedProcessError`, and if that confirmation itself fails,
  `TERMINATION_UNCONFIRMED` dominates, raised explicitly `from` a
  fully-formed `BoundedProcessError` representing the original failure
  (never the internal-only `_BoundedFailure` signal type, and never
  left to incidental `__context__`). `codeagent.reconciliation.py` is
  refactored to use this shared runner in place of its own former
  private copy of the identical logic (`_drain_bounded`/
  `_kill_and_confirm`/`_BoundedReadFailure`, now removed) — every
  existing `test_reconciliation.py` test passes unmodified in
  behavior, proving the extraction changed nothing observable.
  `codeagent.executor.py`: every non-streaming Docker control-plane
  command (`create`/`inspect`/`rm`/`ps -a`) now runs through this
  shared runner with its own fixed, documented byte limit and timeout
  (`_CREATE_ID_MAX_BYTES=65`, `_INSPECT_STATE_MAX_BYTES=64KiB`,
  `_RM_OUTPUT_MAX_BYTES=4096`, `_CLEANUP_LISTING_MAX_BYTES=1MiB`, all
  at `_DOCKER_CONTROL_PLANE_TIMEOUT_SECONDS=30.0`) — previously **these
  four commands had no timeout and no output bound at all**, a real,
  independent gap this slice closes; `docker start --attach`'s own
  separately-bounded streaming collection (`_MAX_STREAM_BYTES`,
  Milestone 1) and its own timeout (the caller-configured verification
  budget) are unchanged. A new `_DockerControlPlaneFailure` exception
  (distinct from `_DockerLaunchError`) classifies any post-launch
  categorical failure on any of these four commands (a timeout, an
  output-bound overflow, a monitoring failure, an unconfirmed
  termination, or an unconfirmed descriptor cleanup) as
  `ENVIRONMENT_FAILURE`, the same treatment a malformed or failed
  inspection already received before this slice — no new `ErrorCode`.
  `_parse_create_id()` strictly validates a successful (zero-exit)
  `docker create`'s stdout as exactly 64 lowercase hex characters plus
  one trailing LF (65 bytes total), rejecting every other shape
  (empty, missing/extra newlines, CRLF, uppercase, abbreviated,
  non-hex, embedded NUL, whitespace, trailing data) categorically —
  never trusting or acting on an ID from a nonzero create result, and
  never starting the container on a malformed one (cleanup still
  targets the exact generated name in that case, since a real
  container may exist). Once a valid ID is captured, `docker start
  --attach`, the final-state `docker inspect`, and `docker rm --force`
  all target that immutable ID instead of the mutable generated name —
  closing a rename/name-reuse race that could otherwise redirect a
  later mutation to a different container. Final cleanup confirmation
  now performs one fresh, unfiltered, `--no-trunc`
  `docker ps -a --format '{{.ID}}\t{{.Names}}'` listing, strictly
  parsed (`_parse_cleanup_listing()`: exactly one ID/name record per
  nonempty line, ID exactly 64 lowercase hex, name nonempty, no
  duplicate ID or name — any malformed row or ambiguity means
  unconfirmed, never partially trusted), and requires **both** the
  exact generated name and (when a valid ID exists) that exact ID to
  be absent — closing the ambiguity where a container's ID reappears
  live under a different name. `docker rm`'s own return code remains,
  as before, never authoritative for removal; a control-plane failure
  on `rm` itself still proceeds to the independent listing
  confirmation rather than short-circuiting.
  **A correction pass (2026-09-23) fixed eight real gaps found by
  review before this slice was considered final**, none of them
  changing the naming/labeling/lifecycle-integration scope above:
  (1) each individual `os.read()` call is now capped to the caller's
  *remaining* allowance (`min(_BOUNDED_READ_CHUNK, limit + 1 -
  len(buffer))`), never a flat 64 KiB chunk regardless of how close to
  the limit the buffer already was — previously a 65-byte-limited
  command could read up to ~64 KiB before overflow was even detected;
  (2) kill-confirmation now uses a grace deadline computed fresh at the
  moment of the kill attempt (`time.monotonic() +
  _KILL_CONFIRM_GRACE_SECONDS`), never derived from or extended by the
  original command deadline — previously an early failure on a
  30-second command could grant a ~32-second kill-confirmation wait;
  (3) a cancellation-style `BaseException` (`KeyboardInterrupt`,
  `SystemExit`, `GeneratorExit`) is never translated into a categorical
  `BoundedProcessError` — cleanup is still attempted, but the exact
  original exception instance propagates unchanged on success; if
  cleanup instead fails, the result is `BoundedProcessError(
  TERMINATION_UNCONFIRMED)` (reap not confirmed) or `BoundedProcessError(
  CLEANUP_UNCONFIRMED)` (reap confirmed but a descriptor close was not),
  chained from that original exception;
  (4) `run_bounded_stdout` now fully validates every call argument
  (rejecting a bare `str`/`bytes` argv, a non-string or NUL-containing
  element, a `bool`/wrong-typed/non-positive `stdout_limit`, and a
  `bool`/wrong-typed/non-finite/non-positive `timeout_seconds`) before
  any deadline is computed or process launched, with fixed sanitized
  `ValueError` text that never echoes the caller's actual argv or
  value; (5) a genuine `os.read()`/pipe I/O error is now classified
  `MONITORING_FAILED`, not conflated with `TIMED_OUT`, and any
  non-`TimeoutExpired` `wait()` failure (during either ordinary
  completion confirmation or termination confirmation itself) is still
  categorical rather than a raw exception escaping; (6) a new
  `BoundedProcessFailure.CLEANUP_UNCONFIRMED` reason means a
  descriptor-close failure (the selector, either stdout close) is never
  silently swallowed — attempted on every path including an otherwise-
  fully-successful drain, dominating a clean result and chaining from
  whatever read or termination failure preceded it; (7)
  `executor._parse_cleanup_listing()` now validates each listing row's
  name against Docker's own container-name grammar
  (`_CONTAINER_NAME_RE`: ASCII-alphanumeric first character, then
  alphanumeric/underscore/period/hyphen) rather than accepting any
  merely-nonempty string, so whitespace, a carriage return, NUL,
  Unicode, or leading/embedded punctuation in a name row now makes the
  whole listing untrusted; (8) `run_bounded_stdout`'s own docstring is
  corrected to state explicitly that its deadline governs monitoring/
  read/wait only *after* a child process object is returned by `Popen`
  — it does not and cannot bound the synchronous `Popen()` call itself.
  Two test-infrastructure bugs were also found and fixed during this
  same pass, neither a production defect: globally patching `os.read`
  in several new tests also intercepted `subprocess.Popen`'s own
  internal errpipe read on some code paths (used to detect a child's
  `exec()` failure), corrected to filter by the target subprocess's own
  fd; and one test's assertion assumed a `wait()` failure classifies as
  `MONITORING_FAILED` without accounting for the same failure also
  dominating to `TERMINATION_UNCONFIRMED` when it recurs during
  termination cleanup, corrected to make `wait()` fail only once.
  Verified (post-correction-pass totals): the three directly affected
  files (`test_executor.py`, `test_reconciliation.py`, `test_bounded_
  subprocess.py`) together, 227 passed (up from 178: `test_executor.py`
  113, up from 95; `test_bounded_subprocess.py` 47, up from 16;
  `test_reconciliation.py` unchanged at 67); the established
  eight-file focused set plus these three collected and passed
  together, 854 passed (up from 805), in both forward and reverse file
  order; the full local suite (macOS): 2,432 passed (up from 2,383).
  With a real Docker daemon and `CODEAGENT_REQUIRE_DOCKER=1` (a skip
  treated as a failure): the 3 dedicated real-Docker tests, 3 passed,
  0 skipped (confirming the tightened container-name grammar and
  read-bound fix don't affect real Docker naming/behavior); the
  complete suite, 2,432 passed, 0 skipped; no leftover
  `codeagent-verify` containers, extra worktrees, `refs/codeagent`
  refs, child processes, or temp state roots afterward.
  `docs/threat-model.md`'s T-G4 is updated to distinguish the
  pre-existing, unchanged verification-command stream bound from this
  slice's newly-bounded Docker control-plane calls — no claim of
  lifecycle attribution, crash recovery, or T-F1 mitigation is made;
  none of that exists yet.
  **A second, narrower correction pass (2026-09-23) fixed one further
  real gap the first pass's own fix had introduced**: when `_drain()`
  raised `CLEANUP_UNCONFIRMED` chained from a deeper read failure (e.g.
  `OUTPUT_LIMIT_EXCEEDED`), `run_bounded_stdout`'s outer handler built
  the public `inner_cause` but never attached it to `original` before
  possibly using `original` as a `from` target if `_terminate_and_
  confirm` then also failed — silently losing the deepest failure from
  the public chain in exactly that double-failure case. Fixed by
  attaching `inner_cause` to `original.__cause__` before it is ever
  used. Two docstring overclaims were also corrected: the cancellation-
  preserving cleanup-failure path can produce `CLEANUP_UNCONFIRMED` as
  well as `TERMINATION_UNCONFIRMED` (both were previously described as
  only the latter), and `executor._run_docker()`'s own docstring
  wrongly claimed its deadline covers "launch through confirmed
  termination" — corrected to state the deadline governs only
  monitoring/read/wait after the `docker` process is launched, matching
  the shared runner's own accurate wording; its failure-list wording was
  also loosened from an incomplete enumeration to durable categorical
  phrasing. Verified: `test_bounded_subprocess.py` 49 passed (up from
  47; 2 new three-level chain-preservation regressions, one via a
  repeated `CLEANUP_UNCONFIRMED`, one via a dominant
  `TERMINATION_UNCONFIRMED`, both asserting every node in the raised
  chain is a public `BoundedProcessError` with no internal
  `_BoundedFailure` exposed); the three directly affected files
  together, 229 passed; the established eight-file focused set plus
  these three, 856 passed, in both forward and reverse file order; full
  local suite, 2,434 passed; with `CODEAGENT_REQUIRE_DOCKER=1`, the 3
  dedicated real-Docker tests, 3 passed, 0 skipped, and the complete
  suite, 2,434 passed, 0 skipped; no leftover containers, worktrees,
  refs, processes, or temp state roots afterward.
  **GitHub-hosted Linux CI confirmed** (commit
  `1a15485575f460b43fa87e7fd159f73c5234fd7e`, run
  [35920856512](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35920856512),
  `ubuntu-24.04` x86_64, Python 3.12, success): the pinned verification
  image was pulled and confirmed `linux/amd64`; the dedicated mandatory
  real-Docker step, 3 passed, 0 skipped; the complete-suite step, 2,431
  passed, 3 skipped. This step also ran with `CODEAGENT_REQUIRE_DOCKER=1`
  against a ready Docker daemon, so those 3 skips were **not** the
  real-Docker tests, which had already passed again within this same
  step. Narrowest supportable claim: three platform/host-specific tests
  skipped; they were not the real-Docker tests. Source inspection (not
  proven from this run's own log, which uses `pytest -q` and does not
  expose skip names) identifies the likely candidates as three tests
  that unconditionally skip on a Linux, case-sensitive-filesystem
  runner: `test_evidence.py`'s Darwin-only ambient-`/tmp`-symlink test,
  `test_lifecycle_fs.py`'s Darwin-only case-canonicalization test, and
  `test_repo_identity.py`'s case-insensitive-filesystem-dependent alias
  test (`test_discover_repository_identity_case_alias_containment`,
  which calls `pytest.skip()` at runtime when `os.path.exists()` on a
  differently-cased alias path returns `False`, as it does on a
  case-sensitive filesystem). No leftover `codeagent-verify` containers
  afterward. This is GitHub-hosted `ubuntu-24.04` x86_64 evidence
  specifically, not a general Linux or ARM64 claim, and is
  implementation/automated-test evidence only — it does not constitute
  or substitute for a security review, and this slice adds no lifecycle
  publisher, lifecycle projection write, ownership label, deterministic
  lifecycle-derived container name, controller wiring, or crash-
  reconciliation behavior of any kind.
- **Milestone 3 Slice 3B-5** (safe reconciliation and removal of
  ADR-attributable Docker containers), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 5 (Accepted 2026-09-23)", **is implemented, locally
  validated on macOS (2026-09-23), committed
  (`923d99a6596771c37defb801237e4642c56a3305`), and confirmed on
  GitHub-hosted Linux CI** (run
  [35954619345](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35954619345),
  `ubuntu-24.04` x86_64, Python 3.12, success — see the closing
  paragraph below for the exact totals this run covers; this is
  implementation/automated-test evidence only, not a security review,
  and is `ubuntu-24.04` x86_64 evidence specifically, not a general
  Linux or ARM64 portability claim). Extends Slice 3B-1's automatic
  reconciliation with the
  container half of ADR 0004 §7's persisted-combination table:
  eligibility widens from `PREPARING`/`RECONCILING`-only to also
  `ACTIVE`/`CLEANING` (a dead owner can crash in any owner-writable
  state), with the required shape narrowed to worktree/checkpoint-ref/
  failure absent while either container's own shape is otherwise
  unconstrained (`lifecycle_store.is_projection_reconciliation_eligible_shape`).
  New in `lifecycle_store.py`: `_publish_reconciler_container_transition`
  (a reconciler-owned write-ahead primitive, hard-coded to `RECONCILING`,
  distinct from the live-owner's `record_container_transition` since
  that writer is categorically refused during `RECONCILING`) and its
  own `_RECONCILER_CONTAINER_TRANSITION_EDGES` table — deliberately not
  layered on the live-owner's own table, since reconciliation's
  `creating -> absent` edge proves only a weaker, point-in-time
  "confirmed absent by a fresh observation" claim, never the live
  owner's stronger historical "no container was ever created" claim.
  New in `reconciliation.py`: a strict `docker ps -a --no-trunc
  --format '{{.ID}}\t{{.Names}}'` listing (`_docker_ps_all_id_name_pairs`,
  replacing the old name-only listing; a duplicate id or name anywhere
  untrusts the whole listing) and a strict ownership-proof
  `docker inspect --type container --format
  '{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{json .Config.Labels}}'` by
  immutable id (`_docker_inspect_ownership`, all four required labels
  checked); `_reconcile_locked_entry` is reordered so checkpoint-ref
  and worktree absence are confirmed before containers are inspected,
  computes both roles' complete decisions before mutating either (zero
  mutation on either conflict), publishes every write-ahead transition
  for both roles before any `docker rm` is issued, then removes
  baseline-first — stopping before verification's own removal if
  baseline does not reach durable absence this pass — always
  re-observing by a fresh independent listing after any `docker rm`
  attempt regardless of that attempt's own outcome (never trusting
  `docker rm`'s exit code). `ReconciliationEntryResult` gains
  `baseline_id`/`verification_id`, retained in the (retrospective,
  never write-ahead) maintenance trace even after a successful removal.
  The old 3B-1 static test asserting *zero* removal-shaped calls
  anywhere in `reconciliation.py` is obsolete by design (this slice's
  whole point is one sanctioned `docker rm` path) and is replaced by
  two narrower static proofs: no filesystem-removal primitive
  (`shutil.rmtree`/`os.remove`/`os.unlink`/`os.rmdir`) is reachable
  (worktree/checkpoint-ref removal remain out of scope), and exactly
  one `["docker", "rm", "--force", ...]`-shaped argv literal exists.
  `docs/adr/0004-...md` section 7's own table is corrected in place
  (the old "recorded in the maintenance trace first" parenthetical
  contradicted Amendment 2 §4's already-accepted retrospective-trace
  rule); `docs/threat-model.md`'s T-E1 entry now records dead-run
  container recovery as implemented, not merely planned. Verified: the
  nine-file focused set used since Slice 3A-1 plus
  `test_bounded_subprocess.py`, 875 passed, in both forward and reverse
  file order; the full local suite (macOS), with a real Docker daemon
  and `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): the 3
  dedicated real-Docker tests in `test_slice_c.py`, 3 passed, 0
  skipped; the complete suite, 2,453 passed, 0 skipped (up from the
  2,435-test pre-3B-5 baseline); no leftover `codeagent-verify`
  containers afterward, confirmed directly via `docker ps -a --filter
  name=codeagent-`. Two of the new tests use real Docker fixtures
  created directly with the exact deterministic name and required
  labels (never through `DockerVerifier`): one proves a genuinely
  owned, labeled container is observed and removed end to end; the
  other proves an unlabeled container at the same deterministic name is
  refused and never removed. **Not implemented** (later Milestone 3
  work, unchanged scope from prior slices): any `executor.py` change,
  lifecycle-aware container creation, a producer-side publisher seam
  wired into `DockerVerifier`, deterministic-name container creation,
  `RunController`/CLI wiring, worktree or checkpoint-ref removal,
  abandonment, and signal handling. See `ENGINEERING_LOG.md`'s dated
  entry for the five design points locked in during planning review
  and the full test/verification detail.
  **A correction pass (2026-09-23) fixed eight real gaps found by
  review before this slice was considered final, all confirmed by
  independent re-verification against source, none disputed**: (1)
  `_docker_inspect_ownership` accepted an inspect name with no leading
  `/`, now requiring exactly one; the listing parser switched from
  `str.splitlines()` (which silently absorbs a CRLF-terminated row as
  bare-LF) to a strict `\n`-only split with an explicit trailing-
  newline check; (2) the implementation actually checked the worktree
  before the checkpoint ref, contradicting its own docstring, the ADR,
  and this file's own prior description — reordered to the accepted
  checkpoint-ref-then-worktree order (the earlier bullet's own text was
  already correct; only the code disagreed with it); (3)
  `_remove_and_confirm_absent` trusted a post-removal listing alone to
  conclude `STILL_PRESENT`, never re-proving ownership (I2) — a
  surviving exact id/name pair is now re-inspected by immutable id
  before that conclusion, with a listing-level pairing disagreement
  still `CONFLICT` without a redundant inspect; (4) every `REFUSED`/
  `SUBSTRATE_UNAVAILABLE` container-classification branch now carries
  whatever candidate id it actually observed into `ReconciliationEntryResult.
  baseline_id`/`verification_id` (previously only `OWNED_REMOVE`/
  `CONFIRMED_ABSENT` did), and the container-conflict and write-ahead-
  failure exits in `_reconcile_locked_entry` now populate both roles'
  ids from whatever had already been observed; (5) 26 new direct,
  fd-only unit tests for `_publish_reconciler_container_transition`
  were added to `test_lifecycle_store.py` (previously zero existed —
  it was exercised only indirectly through the full pipeline), plus 3
  explicit ordering tests in `test_reconciliation.py`; (6) two real-
  SIGKILL crash-resume tests were added (a genuinely separate child
  process self-SIGKILLs immediately after its 1st, then separately
  after its 2nd, reconciler-owned container write durably lands against
  a real Docker container; a fresh pass then resumes, removes the real
  container(s), and never re-increments the attempt count across the
  crash), plus two more real-Docker ADR-§7-table-row tests independent
  of the pre-existing `creating`-role coverage (a `present`-role owned
  removal on the *verification* role, and a `present`-role persisted-
  id/live-id conflict on the *baseline* role). At the time this first
  correction pass concluded, the trivial absent/confirmed-absent rows
  and the `absent`+name-present ambiguity row still remained mock-only;
  **the second correction pass below closed the ambiguity row with a
  real-Docker test** — only the trivial "nothing live at all"
  confirmed-absent rows remain deliberately unit-only, per the
  recommendation the second pass's own entry below and
  `ENGINEERING_LOG.md` record;
  (7) every real-Docker fixture now creates containers from this
  repository's own pinned `executor.DEFAULT_IMAGE` (imported, not
  duplicated) instead of a floating, CI-unapproved `alpine:latest`,
  fixture teardown is now load-bearing (`check=True`, a cleanup failure
  fails the test), and a new final test queries both
  `codeagent-baseline-*` and `codeagent-verification-*` name families
  for leftovers, not only the unrelated `codeagent-verify` family.
  Verified (post-correction-pass): `test_reconciliation.py` alone, 94
  passed with `CODEAGENT_REQUIRE_DOCKER=1`, 0 skipped (up from 86); the
  nine-file focused set plus `test_bounded_subprocess.py`, 916 passed
  (up from 875), in both forward and reverse file order; the full local
  suite, 2,494 passed both with and without `CODEAGENT_REQUIRE_
  DOCKER=1` (identical total both times — every real-Docker test ran
  both times, none skipped either way); `git diff --check` clean; no
  leftover `codeagent-baseline-*`/`codeagent-verification-*`
  containers, extra worktrees, `refs/codeagent` refs, or temp state
  roots afterward. See `ENGINEERING_LOG.md`'s dated correction-pass
  entry for full detail.
  **A second, narrower correction pass (2026-09-23) fixed seven more
  real gaps found by review, all confirmed, none disputed**: separated
  `_ContainerDecision.removal_id` (write authorization) from a new
  `observed_id` (retrospective trace evidence only) so an unobserved
  persisted id could no longer leak into the maintenance trace as if
  positively confirmed; both roles' retrospective trace ids are now
  derived immediately from both already-completed decisions before the
  first publication, so an early role's failure can no longer silently
  drop a later role's already-known id; 12 new parser regression tests
  for the inspect-name/listing-split grammar, which in the process
  **found and fixed one further real gap**: `_docker_inspect_ownership`
  did not reject an embedded `\r`, letting a CRLF row's trailing
  carriage return survive into the labels JSON field, where `json.
  loads` silently tolerates it as trailing whitespace — now explicitly
  rejected; 8 new direct unit tests plus 1 parametrized pipeline test
  for `_remove_and_confirm_absent`'s complete post-removal
  classification (still-present/wrong-identity/wrong-labels/inspect-
  failure/listing-conflict, three `docker rm` failure shapes each still
  followed by successful confirmation, and durable `removing(id)`
  retention through every non-absent outcome); both real-SIGKILL tests
  now assert `proc.exitcode == -signal.SIGKILL` as load-bearing
  evidence and kill+join a still-alive child before failing, rather
  than only checking `is_alive()`; 2 more real-Docker ADR-§7-table-row
  tests close the two remaining safe, deterministic rows named in
  review (a `present` role whose real container was genuinely removed
  out of band before reconciliation ran; an `absent` role with a real,
  correctly-labeled container still occupying the deterministic name) —
  the one row left deliberately unit-only (literal absence-of-anything)
  is stated as a recommendation in `ENGINEERING_LOG.md`, not silently
  narrowed into this file or the ADR; and the final-cleanup test's
  regex filter was independently confirmed against a real container to
  actually anchor and alternate as intended, then paired with a second,
  fully parser-independent client-side query, both asserted, a failure
  now naming the actual leftover containers. Verified: `test_
  reconciliation.py` alone, 132 tests collected (up from 94); the
  nine-file focused set plus `test_bounded_subprocess.py`, 954 passed
  (up from 916), forward and reverse; the full local suite, 2,532
  passed both with and without `CODEAGENT_REQUIRE_DOCKER=1` (identical
  total both times, up from 2,494); `git diff --check` clean; no
  leftover containers by either cleanup query, no extra worktrees or
  refs. Docker Desktop was started only via plain `open -a Docker`; no
  administrator-access dialog appeared during this pass. See
  `ENGINEERING_LOG.md`'s dated second-correction-pass entry for full
  detail.
  **A third, narrow consistency pass (2026-09-24) fixed five review
  points, all confirmed**: `_docker_ps_all_id_name_pairs` now rejects
  any blank listing row (leading, internal, or an extra trailing one)
  once output is nonempty, rather than silently skipping it — empty
  output itself remains valid; ADR 0004 Amendment 5 section 5's own
  text is corrected in place to describe the real post-removal
  sequence (a still-present exact id/name pair is re-inspected by
  immutable id before `FAILED` is ever concluded, matching production
  since the first correction pass), with its evidence paragraph
  corrected from "exact totals and CI evidence" to local verification
  totals with "Linux CI is still pending" stated explicitly (this
  slice has never been pushed); and this file's own first-correction-
  pass paragraph above, which had said the `absent`+name-present
  ambiguity row stayed mock-only, is corrected in place to say that was
  true only "at the time this first correction pass concluded" and to
  point at the second pass's own later closure of that row, removing
  the contradiction a top-to-bottom reader would otherwise hit.
  Verified: the listing-parser test group, 11 passed; `test_
  reconciliation.py` alone, 135 passed with `CODEAGENT_REQUIRE_
  DOCKER=1`; the nine-file focused set plus `test_bounded_subprocess.py`,
  957 passed (up from 954), forward and reverse; the full local suite,
  2,535 passed both with and without `CODEAGENT_REQUIRE_DOCKER=1`
  (identical total, up from 2,532); `git diff --check` clean; no
  leftover containers, extra worktrees, or refs. Docker was already
  ready and was not restarted; no administrator-access dialog appeared.
  See `ENGINEERING_LOG.md`'s dated third-correction-pass entry for full
  detail.
  **Finalized and confirmed on GitHub-hosted Linux CI** (commit
  `923d99a6596771c37defb801237e4642c56a3305`, run
  [35954619345](https://github.com/G-ChandraSekhar/codeagent/actions/runs/35954619345),
  `ubuntu-24.04` x86_64, Python 3.12, success): the mandatory Docker
  preflight passed; the pinned verification image was pulled and
  confirmed `linux/amd64`; the dedicated real-Docker step
  (`CODEAGENT_REQUIRE_DOCKER=1` confirmed set), 3 passed, 0 skipped;
  the complete-suite step (`CODEAGENT_REQUIRE_DOCKER=1` also confirmed
  set for this step), 2,532 passed, 3 skipped — since the real-Docker
  tests already passed within this same step under that env var, these
  3 skips are **not** the real-Docker tests; the run used `pytest -q`,
  which prints no test identities, so these are described only as
  three platform/host-specific skips, never a guessed identity. This
  slice's own `codeagent-baseline-*`/`codeagent-verification-*`
  leftover-container assertion ran and passed inside this same
  complete-suite step, not as a separate workflow step; the workflow's
  own separate final cleanup step checks only the unrelated
  `codeagent-verify` family (Milestone 1's), unchanged by this slice,
  and confirmed empty. This is implementation/automated-test evidence
  only — it does not constitute or substitute for a security review,
  and is `ubuntu-24.04` x86_64 evidence specifically, not a general
  Linux or ARM64 portability claim. It does not claim
  `prepare_lifecycle()` is wired into any real entry point, nor that
  producer-side lifecycle publication, controller/CLI wiring, worktree/
  checkpoint-ref removal, abandonment, or signal handling exist —
  T-E1's existing partial-mitigation boundary in `docs/threat-model.md`
  is unchanged by this slice. Pushed as an ordinary fast-forward
  (`c8b66b3..923d99a`), no force push; local `main` and `origin/main`
  confirmed synchronized post-CI.
- **Milestone 3 Slice 3B-6** (lifecycle-aware `DockerVerifier` container
  production), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 6 (Accepted 2026-09-27)", **is implemented, locally
  validated on macOS (2026-09-27), committed
  (`388e80a496fe39075c8261ab9c407d16c7130b82`), and confirmed on
  GitHub-hosted Linux CI** (run
  [36338011857](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36338011857),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see the
  closing paragraph below for the exact evidence this run supports).
  Entirely opt-in: `DockerVerifier` behaves exactly as before
  when constructed without a `lifecycle_context`, and adopts
  deterministic naming, labeling, and durable write-ahead publication
  only when one is supplied. Two new dependency-light leaf modules:
  `src/codeagent/container_lifecycle.py` (stdlib-only —
  `ContainerRole`, `ContainerIntent` — re-imported by
  `lifecycle_store.py` under this same name for source compatibility,
  no longer defined there — `ContainerTransitionPublisher` Protocol,
  `ContainerPublicationFailure`/`ContainerPublicationError`,
  `deterministic_container_name()`, `required_labels()`/
  `labels_match()`) and `src/codeagent/_docker_ownership.py`
  (generalizes the strict `docker ps -a`/`docker inspect` parsing
  grammar previously duplicated three times across `executor.py` and
  `reconciliation.py`, exposing both pure parsers and full run-and-
  parse convenience functions). `executor.py` never imports, catches,
  compares, or annotates with `lifecycle_store.LifecycleStoreError`/
  `LifecycleStoreFailure` — `lifecycle_store.
  LifecycleContainerPublisher` is the *only* place a `LifecycleStoreError`
  is ever caught and translated into a `ContainerPublicationError`
  (explicit `raise ... from exc` chaining), via an exhaustive,
  subscript-access (never `.get(..., default)`) mapping covering every
  current `LifecycleStoreFailure` member —
  `tests/unit/test_lifecycle_store.py` asserts the map's keys equal the
  complete real enum. `executor.py` catches only
  `ContainerPublicationError`, never a blanket `Exception`/
  `BaseException`.
  `DockerVerifier` gains an optional `lifecycle_context:
  DockerVerifierLifecycleContext | None` parameter (eagerly validated
  `lifecycle_id`/`state_root_id` against the canonical 32-lowercase-hex
  grammar, redefined independently in `container_lifecycle.py` rather
  than importing the persistence stack). When present, a new
  `_execute_lifecycle_aware()` path publishes `CREATING` write-ahead,
  performs a read-only pre-create occupied-name check, recovers a
  proven-owned occupant via `PRESENT(id)->REMOVING(id)->rm(id)->
  confirmed absence->ABSENT` before this invocation's own `docker
  create` ever runs (refusing a foreign/unproven occupant with zero
  mutation), sets the four ADR 0004 section 7 required labels at create
  time (and no `attempt` label — extra/image-provided labels never
  defeat ownership proof), publishes `PRESENT(id)` strictly before
  `docker start --attach`, and publishes `REMOVING(id)`/`ABSENT`
  strictly around a new id-only `_cleanup_by_id` (never the legacy
  name-fallback-capable `_cleanup`, and never a name-targeted `docker
  rm`). `run()` reuses one deterministic `verification`-role container
  name across sequential attempts rather than minting a fresh
  UUID-suffixed name each time. The exact public `ContainerCleanupStatus`
  contract (a binding correction during this slice's own review,
  superseding an earlier internal draft that conflated some rows):
  `NOT_APPLICABLE` only when this invocation's own `docker create` was
  never entered (a `CREATING` publication failure, a foreign/
  conflicting pre-create occupied name, or a pre-create listing/
  inspection failure); once `docker create` has been entered, including
  a bare launch failure, every outcome is `CONFIRMED_ABSENT` (a fresh
  strict observation confirming absence, even though the lifecycle
  projection conservatively remains `CREATING`; or a proven-owned
  candidate recovered successfully) or `UNCONFIRMED` (an owned candidate
  that remains or whose removal is unconfirmed; a foreign/conflicting
  candidate; or an unavailable/malformed observation) — never
  `NOT_APPLICABLE` again. `create_attempted` is retained purely as
  diagnostic evidence and never drives this decision; a new internal
  `_CleanupDisposition` enum does.
  `reconciliation.py` is migrated to the two new shared leaf modules by
  import only — it deliberately still issues its own `run_bounded_stdout`
  call (so its own existing tests, which monkeypatch
  `reconciliation.run_bounded_stdout` directly, continue to intercept
  every Docker call unchanged) and delegates only the parsing step to
  the shared pure parsers; `CONTAINER_LABEL_*`/`_labels_match`/
  `_DockerListingError`/`_DockerInspectError`/`_InspectOwnership` are
  re-exported under their existing private names, a thin-delegator
  migration with no behavior change, confirmed by the pre-existing
  `test_reconciliation.py` suite passing unmodified as part of the
  final thirteen-file focused set reported below (forward and reverse
  file order). `.github/workflows/ci.yml`'s leftover-container
  check is corrected: `docker ps -a --filter name=X` is Docker's own
  substring match, not an anchored family-grammar proof, so the check
  now lists every container name once and applies an anchored
  client-side `grep -E '^codeagent-(verify-|baseline-|verification-)'`
  covering all three families, still strictly detection-only —
  `tests/unit/test_ci_container_leftover_check.py` proves the exact
  pattern detects all three real families and rejects decoy substring
  names.
  **These totals are the final, post-correction-pass authoritative
  ones** (see the correction-pass paragraph below for what changed
  since the initial implementation pass): `py_compile` on every
  changed/new production file; the thirteen-file focused set
  (`test_lifecycle_fs`, `test_repo_identity`, `test_state_locks`,
  `test_state_root`, `test_lifecycle_store`, `test_checkpoint_session`,
  `test_checkpoint_ref`, `test_reconciliation`, `test_bounded_subprocess`,
  `test_container_lifecycle`, `test_docker_ownership`, `test_executor`,
  `test_ci_container_leftover_check`) collected and passed together,
  1,025 passed, in both forward and reverse file order; the directly
  affected set (`test_executor` + `test_lifecycle_store` +
  `test_slice_3b6`) 336 passed; the dedicated real-Docker tests
  (`test_slice_c` + `test_slice_3b6`), 16 passed, 0 skipped; the full
  local suite, with a real Docker daemon and
  `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): 2,616
  passed, 0 skipped, identical total with and without that env var; no
  leftover containers of any CodeAgent family (verified by a strict
  listing covering all three name families), extra worktrees,
  `refs/codeagent` refs, leftover processes, or temp state roots
  afterward; `git diff --check` clean.
  `tests/integration/test_slice_3b6.py` (new, 13 tests — 7 genuine
  real-Docker tests, each individually marked with a per-test
  `requires_docker` marker rather than a module-wide skip, plus 5
  mock-based cleanup-helper regressions and 1 structural test proving
  the marker placement itself, none of which touch Docker at all and
  all of which run and pass even when Docker is unavailable) proves the
  full real path end to end with **no mocking of the writer or the
  Docker CLI in any of the seven real-Docker tests**: a
  real `prepare_lifecycle()` lease, a real `_LifecycleProjectionWriter`,
  a real `LifecycleContainerPublisher`, and a real `DockerVerifier`
  complete a real baseline run (asserted `PASSED`, not merely a cleanup
  side effect) plus two sequential `verification`-role attempts (also
  asserted `PASSED`) that reuse the identical deterministic container
  name; a separate real-Docker test inspects a live container mid-run
  and confirms the exact deterministic name and exactly the four
  required labels (no `codeagent.lifecycle.attempt` label) via `docker
  inspect`; a third pre-creates a real, correctly owned container
  carrying a genuine extra unrelated label, proves that label is
  actually present, then proves occupied-name recovery accepts
  ownership despite it and removes the pre-existing container by
  immutable ID before completing a new run. Three real, independently
  spawned SIGKILL
  child-process tests each self-SIGKILL a genuinely separate process at
  one of the three accepted crash boundaries (immediately after
  durable `CREATING`, before `docker create`; immediately after durable
  `PRESENT(id)`, before `docker start`; immediately after durable
  `REMOVING(id)`, before `docker rm`), confirmed via
  `proc.exitcode == -signal.SIGKILL`, each followed by a fresh
  `reconcile_repository()` pass in a separate process proving the
  correct residual-state resolution (no container anywhere; a real
  created-but-never-started container recovered and removed; a real
  started-and-finished container recovered and removed) — matching
  ADR 0004 Amendment 6 section 5's residual-state table exactly.
  **Not implemented** (later Milestone 3 work, unchanged scope from
  every prior slice): `RunController`/CLI wiring, abandonment, worktree
  or checkpoint-ref removal, signal handling, and model integration.
  `docs/threat-model.md`'s T-E1 entry is unchanged by this slice
  specifically — nothing here changes when or whether
  `prepare_lifecycle()` is called before a real run starts; this slice
  makes only the *producer* side lifecycle-aware, proven here via
  direct construction, not through any existing entry point.
  **A same-day correction pass (2026-09-27, before this slice was ever
  committed) fixed one real correctness bug and hardened several
  weaknesses a review found**: `DockerVerifierLifecycleContext` no
  longer accepts `lifecycle_id`/`state_root_id` as independent
  constructor fields alongside `publisher` — that shape let the two
  disagree (a real, dangerous mismatch a crash could leave
  unattributable to any reconciler). `container_lifecycle.
  ContainerTransitionPublisher` now exposes `lifecycle_id`/
  `state_root_id` as read-only properties, `lifecycle_store.
  LifecycleContainerPublisher` implements them from its own `current`
  projection, and `DockerVerifierLifecycleContext` derives both as
  computed properties from `publisher` alone — the identity mismatch
  this closes is now structurally unrepresentable, not merely refused
  at runtime. A genuine bug this correction's own real end-to-end test
  exposed: a fully successful occupied-name recovery collapses the
  projection back to `ABSENT`, but the code then tried to publish
  `PRESENT` for its own container directly — there is no
  `ABSENT->PRESENT` edge in the live-owner's own table, only
  `ABSENT->CREATING` — so a real writer refused it
  (`ILLEGAL_TRANSITION`), leaving a real, created-but-never-started
  container behind; fixed by re-publishing `CREATING` immediately after
  a successful recovery, before the next `PRESENT`. The mock-based
  `_FakeContainerPublisher` (which had not caught this, since it never
  validated edges) now enforces the identical transition-edge table
  locally. `tests/integration/test_slice_3b6.py` also gained: a
  properly closed `_reconcile_fresh()` test helper (it previously
  leaked an open `StateRoot`/repository-lock pair on every call — fixed
  by releasing the lock before closing the state root, both attempted
  regardless of an earlier failure, matching `LifecycleLease.close()`'s
  own convention — with a load-bearing regression proving the lock is
  immediately reacquirable afterward); load-bearing fixture cleanup
  (`_force_remove_container()` now confirms absence and raises if it
  cannot, rather than silently ignoring `docker rm`'s exit code; the
  label-inspection test now asserts its worker thread genuinely
  finished before the lease closes); a real replacement for the
  previously unproven "may contain labels" extra-label test (a
  deterministic pre-created container with a genuine extra label is
  proven present, then genuinely recovered and removed by immutable ID
  during occupied-name recovery); and a strengthened end-to-end
  assertion (a deterministic `("true",)` command, mounting the real
  repository rather than its parent directory, asserting `PASSED` for
  the baseline and both verification attempts, not merely their cleanup
  status). `tests/unit/test_ci_container_leftover_check.py` now reads
  and extracts the actual configured `grep -E` pattern from
  `.github/workflows/ci.yml` itself, rather than testing a hand-copied
  duplicate that could silently drift from the real workflow.
  **A second, narrower same-day correction pass** fixed a fail-open gap
  in this same integration test file's own cleanup helpers:
  `_container_exists()` previously treated *any* nonzero `docker
  inspect` exit — including a genuinely unavailable Docker daemon — as
  "container absent," so `_force_remove_container()` and
  `_cleanup_deterministic_name_if_present()` could both report cleanup
  confirmed without positive evidence. All three now derive presence
  exclusively from a fresh, strict, unfiltered listing via the shared
  `_docker_ownership.docker_ps_all_id_name_pairs()` (the same shared
  listing this slice's own production code already uses) — a listing
  failure (`DockerListingError`) now propagates and fails the test
  instead of silently meaning "absent"; `_force_remove_container()`
  additionally refuses a malformed id before ever calling `docker rm`;
  `_cleanup_deterministic_name_if_present()` now obtains its candidate
  id from that same strict name→id listing rather than `docker inspect
  <name>`. Five new mock-based regression tests pin this behavior
  (listing failure not treated as absence; a malformed id never reaches
  `docker rm`; a still-present id fails cleanup; a confirmed-absent id
  succeeds; removal is always by listed id, never by name) — this is a
  test-infrastructure correction only, no production behavior changed.
  **A third, narrower same-day correction pass** fixed a test-gating gap
  those same five mock-only regressions had been given: this file's
  Docker-availability skip had been applied module-wide
  (`pytestmark = pytest.mark.skipif(...)`), so the five mock-only tests
  — which make no Docker call at all — were also silently skipped
  whenever Docker happened to be unavailable locally, defeating their
  own purpose as failure-injection tests. Replaced with a per-test
  `requires_docker` marker applied to exactly the seven genuine
  real-Docker tests (independently re-derived, not merely copied from a
  suggested list — `test_reconcile_fresh_releases_the_repository_lock`
  was confirmed to genuinely need Docker too, since a fresh
  `reconcile_repository()` pass over any eligible entry always issues a
  real `docker ps -a`); the five mock-only tests now carry no marker and
  run unconditionally. A new structural test,
  `test_exactly_the_real_docker_tests_carry_the_requires_docker_marker`,
  introspects the module directly and pins exactly this marker
  placement, so a future test added to this file without deliberately
  choosing whether it needs Docker cannot silently drift either way.
  The prior mandatory-Docker CI hard-failure behavior
  (`CODEAGENT_REQUIRE_DOCKER=1` set but no daemon available fails the
  whole module at collection time, rather than skipping) is unchanged.
  This pass also fixed one stale sentence elsewhere in this same section
  (the `reconciliation.py` migration paragraph above previously cited
  the pre-correction "1015 passed" / "other seven focused files"
  figures, which are corrected to reference the final thirteen-file
  focused set reported at the top of this bullet). See
  `ENGINEERING_LOG.md`'s dated entry for the exact final,
  post-all-correction-passes local totals.

  **Confirmed on GitHub-hosted Linux CI** (commit
  `388e80a496fe39075c8261ab9c407d16c7130b82`, run
  [36338011857](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36338011857),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the
  mandatory Docker preflight passed; the pinned verification image was
  pulled and confirmed `linux/amd64`. Two distinct real-Docker
  surfaces ran, and must not be conflated: the dedicated, separately
  named "Run real Docker verification tests" step ran only `python -m
  pytest tests/integration/test_slice_c.py -v` — this file's own 3
  legacy Milestone-1 tests, exactly, reporting `3 passed in 2.28s`, 0
  skipped; it did **not** run `tests/integration/test_slice_3b6.py`.
  This slice's own seven genuine Docker-dependent tests in
  `test_slice_3b6.py` instead ran inside the separate "Run complete
  test suite" step (`python -m pytest -q`, confirmed
  `CODEAGENT_REQUIRE_DOCKER=1` present in that step's own environment)
  — with Docker available there, every one of those seven tests' own
  per-test `requires_docker` marker was inactive (not skipped), and the
  module's own mandatory-Docker guard (`CODEAGENT_REQUIRE_DOCKER=1` set
  but no daemon found fails collection outright) would have failed the
  whole run rather than letting any of them silently skip. That step
  reported exactly `2613 passed, 3 skipped in 46.03s`. The run used
  `pytest -q`, which prints no test identities, so these 3 skips are
  described only as three unidentified, platform/host-specific skips —
  never guessed at — and are not evidence of Docker unavailability:
  Docker preflight succeeded, the pinned image was pulled, and the
  suite ran with `CODEAGENT_REQUIRE_DOCKER=1` throughout. Local macOS
  evidence was 2,616 passed, 0 skipped; Linux CI accounts for the
  identical 2,616 collected outcomes as 2,613 passed plus these 3
  skips. The final leftover-container check ran the anchored
  `grep -E '^codeagent-(verify-|baseline-|verification-)'` over a
  complete, unfiltered `docker ps -a` name listing, covering all three
  CodeAgent container families; its output was empty and the step
  succeeded — no leftover containers of any family. This is
  implementation/automated-test evidence only — it does not constitute
  or substitute for a security review, and is GitHub-hosted
  `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
  ARM64 portability claim. It does not claim `RunController`/CLI
  entry-point wiring exists, or that T-E1's existing partial-mitigation
  boundary in `docs/threat-model.md` has changed — both remain exactly
  as this slice's implementation section above already states.
- **Milestone 3 Slice 3B-7** (shared lifecycle-projection coordination
  and the controller-facing checkpoint lifecycle-publication boundary),
  per `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 7 (Accepted 2026-09-27)", **is implemented, locally
  validated on macOS (2026-09-27), committed
  (`6a452b1d095584d754cbc51f291d1ebd5de9f3c9`), and confirmed on
  GitHub-hosted Linux CI** (run
  [36350258312](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36350258312),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see the
  closing paragraph below for the exact evidence this run supports). A
  review of the previously-proposed composition-root slice
  found two real correctness gaps in already-existing, already-ADR-
  accepted code before any entry-point wiring was ever attempted; this
  slice fixes both and stops there — no `prepare_lifecycle()` call, no
  `RunController` construction change beyond its exception-catching
  surface, no worktree work, no CLI/model/UI.
  **Gap 1 (independent publisher cursors)**: `LifecycleContainerPublisher`
  and `LifecycleCheckpointRefPublisher` each independently tracked their
  own `_current` `LifecycleProjection`. Since `LifecycleProjection`
  equality is whole-object, any successful write through *any* path
  (the other adapter, or a direct `writer.advance_lifecycle_state()`
  call) invalidated every other independently-held `_current` for its
  very next write — not eventually, immediately, reproduced exactly and
  documented as a permanent regression test
  (`test_standalone_adapters_must_never_be_combined_against_one_writer`).
  Fixed with a new `lifecycle_store.LifecycleProjectionCursor` — the one
  authoritative in-memory `current` per lease, with `advance_state()`/
  `record_container()`/`record_checkpoint_ref()`/`refresh()` — and a new
  named bundle `SharedLifecyclePublishers` plus its factory
  `create_shared_lifecycle_publishers(writer, initial)`, the only
  sanctioned way to obtain a checkpoint-ref publisher and a container
  publisher safe to use together against one lease. Both publisher
  classes' existing public `__init__(writer, initial_projection)`
  signatures are completely unchanged and remain source-compatible for
  isolated use (each privately owns its own cursor, never shared) — a
  new private `_from_cursor` classmethod on each is the factory's own
  internal construction path, never called directly by anything else. A
  structural test (`test_shared_publishers_have_no_shadow_current`)
  proves a coordinated facade holds nothing but `{"_cursor"}` — no
  shadow state to silently drift.
  **Gap 2 (checkpoint publication was not controller-safe)**:
  `LifecycleCheckpointRefPublisher.publish()` let a raw
  `LifecycleStoreError` escape uncaught; `RunController.run()` has no
  enclosing try/except, so that error would have propagated straight out
  of `run()` itself, skipping `_fail()`, the domain transition,
  `_terminate()`, evidence capture, worktree disposal, and checkpoint-
  ref deletion entirely — confirmed by direct code reading, not assumed.
  Fixed by giving `checkpoint_session.py` (still dependency-light — it
  never imports `lifecycle_store`) a `CheckpointPublicationFailure`/
  `CheckpointPublicationError` pair beside `CheckpointTransitionPublisher`,
  mirroring `container_lifecycle.ContainerPublicationFailure`/
  `ContainerPublicationError`'s own Amendment 6 inversion pattern
  exactly. `LifecycleCheckpointRefPublisher.publish()` is now the one
  place a `LifecycleStoreError` is ever caught for this path, translated
  via an exhaustive, subscript-access map
  (`tests/unit/test_lifecycle_store.py` asserts its keys equal the
  complete `LifecycleStoreFailure` enum). `RunController`'s two
  checkpoint call sites (`establish()`/`advance()` in
  `_dispatch_apply_patch`) now also catch `CheckpointPublicationError`;
  `_map_checkpoint_error` gains one new branch mapping it to a new,
  deliberately unambiguously-named
  `ErrorCode.CHECKPOINT_LIFECYCLE_PUBLICATION_FAILED`
  (`ErrorDomain.LIFECYCLE`) — distinct from every `CHECKPOINT_REF_*`
  code, which are all about the Git mutation itself, never its
  lifecycle-projection record, and whether the Git mutation was ever
  reached depends on which publication phase failed (a pre-mutation
  transitional-intent publication failure means it was not; a post-
  mutation collapse or confirmed-UNCHANGED-recovery publication failure
  means it already concluded) — this code never asserts either way. The
  `OperationalError` also gets its own fixed, sanitized message ("the
  checkpoint lifecycle projection transition could not be confirmed"),
  distinct from the generic message every `CheckpointRefError`/
  `CheckpointSessionError` mapping still uses. This explicitly, narrowly
  revises
  Amendment 4's own previously-stated exception-identity contract at
  exactly these two named call sites. `session.delete()`'s own failures
  are unaffected — they still fall under `_terminate()`'s existing broad
  teardown catch and fold into the pre-existing
  `LIFECYCLE_CLEANUP_UNCONFIRMED` path, proven by a dedicated regression
  test rather than assumed.
  **Dual-cause preservation**: `establish()`/`advance()`'s existing
  confirmed-`UNCHANGED` recovery flow reuses the same exception object
  across two `raise ... from ...` statements when the recovery-collapse
  publish itself raises `CheckpointPublicationError`. Verified directly
  (a minimal Python reduction, not assumed) that this silently
  overwrites `__cause__`, demoting the original cause to `__context__`
  — which `raise ... from ...` also hides from every standard traceback
  via `__suppress_context__=True`. Fixed with a new, additive
  `CheckpointPublicationError.pre_recovery_cause` attribute, populated
  only in this one dual-failure scenario, preserving what `__cause__`
  held immediately before the existing, unchanged "projection-
  consistency failure dominance" rule (Slice 3B-3) reassigns it to the
  triggering `CheckpointRefError`. No `ExceptionGroup` — nothing in this
  repository's precedent or any `RunController` catch site justified
  one, and the existing linear chain already had a deliberate meaning
  this fix preserves rather than redesigns.
  Verified: the five directly affected files
  (`test_lifecycle_store.py`, `test_checkpoint_session.py`,
  `test_errors.py`, `test_events.py`, `test_controller.py`) collected
  and passed together, 1,099 passed, in both forward and reverse file
  order; the thirteen-file focused set established since Slice 3B-6,
  1,036 passed; the full local suite, with a real Docker daemon and
  `CODEAGENT_REQUIRE_DOCKER=1` (a skip treated as a failure): 2,648
  passed, 0 skipped, identical total with and without that env var;
  `git diff --check` clean. A real lease/real-writer interleaving test
  (`test_shared_bundle_realistic_interleaving_no_spurious_staleness`,
  no Docker, no real Git-ref mutation) drives the shared bundle through
  `PREPARING→ACTIVE`, both container roles through confirmed absence,
  and the full checkpoint-ref lifecycle
  (`ABSENT→CREATING→PRESENT→ADVANCING→PRESENT→REMOVING→ABSENT`) with
  zero spurious `STALE_EXPECTED_PROJECTION`, then fault-injects a
  durability-unconfirmed container publish and proves explicit
  `refresh()` re-syncs both facades in one call, and finally proves a
  genuine external stale write (a second, independent writer) is still
  correctly refused. **Not implemented** (later Milestone 3 work,
  unchanged scope): any `prepare_lifecycle()` call from production
  orchestration, any `RunController` composition/entry-point wiring, a
  `CLEANING`-publication hook, any lifecycle state change during a real
  controller run, and any worktree-tracking schema or allocation
  change. `docs/threat-model.md`'s T-E1 entry is unaffected — this
  slice adds zero entry-point behavior.

  **Confirmed on GitHub-hosted Linux CI** (commit
  `6a452b1d095584d754cbc51f291d1ebd5de9f3c9`, run
  [36350258312](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36350258312),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the
  mandatory Docker preflight passed; the pinned verification image was
  pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
  verification tests" step ran only `python -m pytest
  tests/integration/test_slice_c.py -v` — this file's own 3 legacy
  Milestone-1 tests, exactly, reporting `3 passed in 2.63s`; it does
  **not** specifically exercise Slice 3B-7 (this slice adds no new
  real-Docker test of its own — none of its work touches Docker). The
  separate "Run complete test suite" step (`python -m pytest -q`, with
  `CODEAGENT_REQUIRE_DOCKER=1` confirmed present in that step's own
  environment) reported `2645 passed, 3 skipped in 47.16s`; `2645 + 3`
  equals the local collected total of 2,648. That step used `pytest
  -q`, which prints no test identities, so these 3 skips are described
  only as three unidentified, platform/host-specific skips — never
  guessed at — and are not evidence of Docker unavailability, since
  Docker was required and confirmed available for that same step. The
  final leftover-container check used the anchored `grep -E
  '^codeagent-(verify-|baseline-|verification-)'` over a complete
  `docker ps -a` listing; its output was empty and the step succeeded.
  This is implementation/automated-test evidence only — it does not
  constitute or substitute for a security review, and is GitHub-hosted
  `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
  ARM64 portability claim. It adds no entry-point wiring, no lifecycle
  state change during a real controller run, no `CLEANING` integration,
  no worktree tracking, and no CLI behavior — `docs/threat-model.md`'s
  T-E1 entry remains exactly as stated above, unaffected.
- **Milestone 3 Slice 3C-1** (dependency-light owner-state lifecycle-
  publication boundary and a `RunController` terminal hook), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 8 (Accepted 2026-09-28)", **is implemented, locally
  validated on macOS (2026-09-28), committed
  (`3476b3d7a50e2f979059031ab9c670949a7c20fe`), and confirmed on
  GitHub-hosted Linux CI** (run
  [36483533854](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36483533854),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see the
  closing paragraph below for the exact evidence this run supports).
  Gives containers'/checkpoint-ref's existing
  durable write-ahead publishers (Slices 3B-6/3B-7) an owner-level
  counterpart: a new dependency-light leaf module,
  `src/codeagent/lifecycle_owner.py` (`OwnerStatePublicationFailure` —
  the same 10 members as `ContainerPublicationFailure`/
  `CheckpointPublicationFailure` — and `OwnerStatePublicationError`),
  a new local `controller.LifecycleOwnerPublisher` Protocol
  (`activate()`/`begin_cleanup()`/`complete()`), one new optional,
  keyword-only `RunController.__init__` parameter
  (`lifecycle_owner: LifecycleOwnerPublisher | None = None`, fully
  backward compatible), and `lifecycle_store.LifecycleOwnerStatePublisher`
  (the concrete adapter, sharing the existing `LifecycleProjectionCursor`
  via `SharedLifecyclePublishers`' new third field, `owner_publisher`).
  `RunController.run()` attempts `activate()` immediately after
  `RunStarted`, before any baseline/model/tool work; a confirmed failure
  maps to a new `ErrorCode.LIFECYCLE_STATE_PUBLICATION_FAILED`
  (`ErrorDomain.LIFECYCLE`) and enters the ordinary `_terminate()`
  early-abort path (no `events.py` change required — `RunFinished`'s
  allowed-code set is computed dynamically as `set(ErrorCode) -
  {POLICY_VIOLATION_SEVERE}`). `_terminate()` attempts `begin_cleanup()`
  first (only once `activate()` was confirmed — `PREPARING -> CLEANING`
  is not a legal edge), never skipping evidence capture or the existing
  resource-cleanup sequence on its failure, then attempts `complete()`
  once `begin_cleanup()` and all owned-resource cleanup are confirmed —
  **deliberately independent of evidence-capture success**: `lifecycle.
  json` is an operational resource-recovery projection, not the audit/
  evidence record, so a run can legitimately reach `RunFinished
  (UNRECOVERABLE_ERROR, error=EVIDENCE_*)` while its lifecycle projection
  durably reaches `COMPLETE`. A combined evidence-failure-and-`complete()`
  -failure is dominated by the existing (now wider-scoped)
  `LIFECYCLE_CLEANUP_UNCONFIRMED` precedence tier, proved by a dedicated
  load-bearing test. No automatic `refresh()`/retry anywhere — the
  `LifecycleOwnerPublisher` Protocol has no `refresh()` method at all,
  making this structurally true, not merely behavioral. **Explicitly
  deferred, not decided by this slice**: any `prepare_lifecycle()` call
  from production orchestration, any `RunController` composition/
  entry-point wiring, and — most consequentially — who owns and closes
  a `LifecycleLease` and what a lease-close failure does to the outer
  operation's result; this slice never constructs or closes a lease and
  has no standing to settle that question (Slice 3C-2). Each of the
  three installed-on-disk-versus-cursor-belief durability-unconfirmed
  splits (`activate()`/`begin_cleanup()`/`complete()`) is proven
  independently by its own real-writer fault-injection test, not
  generalized from a single `activate()`-only test; the `complete()`
  case additionally has a dedicated `tests/unit/test_reconciliation.py`
  test proving a real reconciliation pass classifies a seeded
  `COMPLETE`-plus-absent-shape entry as `SKIPPED_TERMINAL`. A correction
  pass (2026-09-28, before this slice was committed) also fixed two
  overclaims: a comment in `controller.py`/this ADR's own text wrongly
  said nothing had touched "Docker, Git, the worktree, or the checkpoint
  ref" by the time `activate()` runs — false, since `RunController` is
  always handed an already-entered `Workspace`, never one it enters
  itself; and `errors.py`'s own comment on the new code wrongly said "the
  run never got started," when `RunStarted` and the `RUN_STARTED`
  transition had already occurred — both corrected to state precisely
  what didn't happen (no controller-driven baseline/Docker/patch/
  checkpoint-ref mutation; normal execution never began). Verified
  (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`, post-
  correction-pass totals): the four directly affected files
  (`test_errors.py`, `test_lifecycle_store.py`, `test_reconciliation.py`,
  `test_controller.py`), 500 passed, forward and reverse file order;
  the established sixteen-file focused Milestone-3 set, 1,843 passed,
  forward and reverse file order; the complete suite, 2,690 passed, 0
  skipped; `git diff --check` clean; no leftover `codeagent-*`
  containers, extra worktrees, `refs/codeagent` refs, or temp state
  roots afterward — confirmed directly against the default macOS state-
  root location too, since the slice's own real-writer tests redirect
  `prepare_lifecycle()` to an isolated per-test `tmp_path` state root via
  `CODEAGENT_STATE_DIR`, never the default one, and no production path
  this slice introduces calls `prepare_lifecycle()` at all.
  **Confirmed on GitHub-hosted Linux CI** (commit
  `3476b3d7a50e2f979059031ab9c670949a7c20fe`, run
  [36483533854](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36483533854),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success`): the
  mandatory Docker preflight passed; the pinned verification image
  (`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
  was pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
  verification tests" step ran only `python -m pytest
  tests/integration/test_slice_c.py -v` (`3 passed in 2.33s`) — this
  file's own legacy Milestone-1 tests, exactly; it does **not**
  specifically exercise Slice 3C-1, which adds no real-Docker test of
  its own. The separate "Run complete test suite" step (`python -m
  pytest -q`, with `CODEAGENT_REQUIRE_DOCKER=1` confirmed present in
  that step's own logged environment) reported `2687 passed, 3 skipped
  in 47.83s`; `2687 + 3` equals the local collected total of 2,690. The
  skipped-test identities are not printed by the `pytest -q` log and are
  not guessed here — they are not evidence of Docker unavailability,
  since the daemon, the pulled image, and `CODEAGENT_REQUIRE_DOCKER=1`
  were all already confirmed for that same step (source inspection, not
  CI-log evidence, continues to identify the three Linux-unconditional
  skips named in earlier slices' entries as the likely candidates — see
  `ENGINEERING_LOG.md`). The final leftover-container check used the
  anchored `grep -E '^codeagent-(verify-|baseline-|verification-)'`
  pattern over a complete, unfiltered `docker ps -a` listing; its
  captured output was empty and the step succeeded. This is
  implementation/automated-test evidence only — it does not constitute
  or substitute for a security review, and is GitHub-hosted
  `ubuntu-24.04` x86_64 evidence specifically, not a general Linux or
  ARM64 portability claim. It does not claim `prepare_lifecycle()`,
  `LifecycleLease` ownership/closure, CLI wiring, or any production
  entry-point composition exists. `docs/threat-model.md`'s T-E1 entry
  is unchanged and remains unmitigated by this slice — nothing here
  calls `prepare_lifecycle()` or constructs a real entry point.
- **Milestone 3 Slice 3C-2** (deterministic lifecycle-scoped worktree
  placement), the smallest correct prerequisite identified during joint
  Codex/Claude planning review for what would otherwise be an unsafe
  "3C-2" production-composition slice, **is implemented and locally
  validated on macOS (2026-09-29)**. `reconciliation.py` already
  hard-codes the expected deterministic worktree path
  (`state_root.path/worktrees/<repo_key>/<lifecycle_id>`) in its own
  crash-recovery check, but nothing in production ever placed a real
  worktree there — `GitWorktree` only ever used an unpredictable
  `tempfile.TemporaryDirectory()`. Composing a real run on top of that
  gap would have made reconciliation's own worktree-absence check
  silently meaningless (always "confirmed absent," since it was checking
  a path nothing ever wrote to), a false-clean result exactly of the
  kind ADR 0004 exists to prevent. This slice closes that gap alone,
  with no `RunController`, Docker, checkpoint-session, or lifecycle-
  projection-schema change of any kind.
  `src/codeagent/state_root.py` gains `StateRoot.reserve_worktree_leaf()`
  (opens/creates the idempotently-reopenable `worktrees/<repo_key>/`
  parent chain via the existing managed-directory-chain primitive, then
  exclusively creates the `<lifecycle_id>` leaf via the existing
  `create_exclusive_directory_at` — never reopened or adopted) and a new
  private `_WorktreeLeafReservation` class, mintable only through that
  method (matching `lifecycle_store._LifecycleProjectionWriter`'s own
  "the only sanctioned way to obtain one" idiom): a context manager
  owning both the parent and leaf descriptors for its lifetime, with an
  explicit `RESERVED -> CLAIMED -> CONSUMED` single-consumer state
  machine (added in a same-day correction pass — see below) whose
  `__exit__` only ever removes the leaf when the reservation is not
  `CONSUMED`, and only after re-verifying by fd (`os.fstat`/`os.stat(...,
  dir_fd=..., follow_symlinks=False)`/`os.path.samestat`) that the
  pathname still names the exact inode it created — never a bare
  "it's empty, so remove it" pathname-only check, which a same-user
  process could defeat by swapping in a different, also-empty directory
  at the identical name. `src/codeagent/workspace.py`'s `GitWorktree`
  gains an optional, keyword-only `reservation` parameter (default
  `None`, fully backward compatible — every existing tempdir-based
  caller and test is unaffected, and any non-`None`, non-genuine value
  is rejected by an `isinstance` check before any reservation access or
  Git call); when given, `__enter__` first atomically `claim()`s the
  reservation (refusing before any Git mutation if already claimed by
  another instance), then performs the same fd-based identity check both
  immediately before `git worktree add` is ever invoked (a mismatch
  means zero Git mutation is attempted) and again immediately after the
  full worktree materialization succeeds (a mismatch here means the
  pathname may now name a foreign, same-user-swapped directory, so the
  run refuses to proceed) — only a full, doubly-reconfirmed success calls
  `reservation.consume()`.

  A same-day, pre-finalization review round (Codex) raised, and this
  slice incorporates, two safety constraints beyond the originally
  planned design: (1) a pathname-only `rmdir` for an unused reservation's
  cleanup is unsafe against a same-user directory swap — fixed by holding
  the parent directory's own descriptor for the reservation's whole
  lifetime and removing the leaf only via `os.rmdir(name,
  dir_fd=parent_fd)` after a fresh fd-relative identity re-check, never a
  bare path string; (2) an enter-time failure must never run the existing
  unconditional `git worktree remove --force <path>` cleanup against a
  pathname whose inode identity can no longer be reconfirmed after a full
  materialization — fixed by gating that removal on a fresh identity
  check first, and refusing to touch the pathname at all (leaving it as
  evidence, raising a categorical cleanup-unconfirmed error) when
  identity is lost or uninspectable. Both were verified against real code
  and real git/POSIX behavior (a disposable `/tmp` experiment) before
  being incorporated, not accepted on assertion; verification is pinned
  in the automated test suite
  (`test_git_accepts_the_real_pre_created_empty_directory`), not merely a
  comment.

  A second, same-day correction pass (also Codex) raised four further
  findings, all confirmed against real code and fixed before this slice
  was considered final: (a) the `reservation` parameter's bare type
  annotation was never enforced at runtime — a duck-typed impostor
  exposing the same surface could have been accepted; fixed with an
  explicit `isinstance` check in `GitWorktree.__init__`, rejected with a
  fixed, sanitized message that never echoes the rejected object's own
  repr; (b) `consume()` alone, set only at the very end of a successful
  entry, could not stop two `GitWorktree` instances from both being
  handed the same reservation and both reaching `git worktree add` — the
  second instance's own enter-time failure cleanup would then
  identity-verify successfully (the path is still the same inode) and
  run `git worktree remove --force` against the *first* instance's real,
  live worktree; fixed with the explicit `RESERVED -> CLAIMED -> CONSUMED`
  state machine above, `claim()` protected by an internal lock (proven
  atomic against genuinely concurrent threads by a real two-thread
  barrier test, not merely reasoned about) so only one caller can ever
  succeed, called before any Git mutation is attempted; (c)
  `_remove_unconsumed_leaf_if_safe()` trusted `os.rmdir`'s own reported
  success without the independent post-mutation observation this
  project's cleanup discipline otherwise always requires — fixed with a
  fresh, fd-relative, no-follow re-observation after `rmdir`, where only
  a confirmed `FileNotFoundError` counts as success and any entry still
  present (even a new, unrelated inode) is reported cleanup-unconfirmed
  and left untouched; (d) one of this slice's own new tests leaked a
  reservation's descriptors past its own scope by never calling
  `reservation.__exit__()`, and one used an overly broad
  `pytest.raises((RuntimeError, Exception))` assertion and a private
  `_consumed = True` mutation to paper over the leak — both corrected
  (the test now uses the reservation context manager while preserving
  the exact simulated-crash condition; the broad assertion now checks
  the exact `LifecycleFsError`/`CLEANUP_UNCONFIRMED` reason; the private
  mutation is removed entirely, relying instead on the real "confirmed
  absent" cleanup path exercised by finding (c)'s own fix).

  A third, same-day correction pass (also Codex) raised and this slice
  incorporates two further findings at `GitWorktree`'s own public
  boundary, plus one wording correction: (e) `claim()`'s refusal let a
  raw `LifecycleFsError` escape `GitWorktree.__enter__()` directly — the
  second-claimant tests had been written to expect exactly that
  storage-layer type, which is itself the defect, since this module
  never exposes `state_root`/`_lifecycle_fs` exception types to its own
  callers anywhere else; fixed by translating a refused claim into a
  fixed, sanitized `GitWorktreeError` chained `from` the
  `LifecycleFsError`, with no worktree-level cleanup attempted for the
  refused claimant (that reservation was never claimed by *this*
  instance, so its Git registration and directory belong entirely to
  whichever instance actually holds the claim); the sequential- and
  preserved-first-worktree second-claimant tests now assert the public
  `GitWorktreeError`, its `__cause__`, and the exact categorical
  reason, while the private reservation's own direct `claim()`/
  `consume()` tests continue to assert the raw `LifecycleFsError`,
  since that boundary is `GitWorktree`'s alone; (f) the ownership
  transfer (`reservation.consume()`) was called *after* `self.path`/
  `self._initial_commit` were already published and *outside* the
  guarded try/except that handles cleanup — a `consume()` failure there
  would have both leaked a raw `LifecycleFsError` and left the instance
  claiming a completed entry it never actually finished; fixed by
  moving `consume()` inside the guarded path, immediately after
  post-Git verification and before either field is published, with its
  own failure translated and routed through the same reservation-aware
  exact-cleanup path as any other post-materialization failure — proven
  by a new load-bearing fault-injection test forcing `consume()` to
  fail after a real worktree was fully created and verified,
  confirming the public exception type and cause, exact registration/
  directory cleanup, and that `self.path`/`initial_commit` stay
  unpublished; (g) one stale comment claimed `claim()` refuses "before
  any Git call," when `_rev_parse("HEAD")` against the source
  repository already runs first — corrected throughout to "before any
  Git mutation," which is the property that actually holds and the one
  this mechanism actually protects.

  A related, out-of-scope-for-this-slice finding surfaced during the
  first review round — `RunController.run()` has no top-level exception
  handler, so `_terminate()` (and therefore checkpoint-session/
  owner-state cleanup) never runs on a fully unexpected exception — is
  recorded as an explicit, unsettled review item for Milestone 3 Slice
  3C-3's own planning, not decided or touched here; it intersects ADR
  0005's still-unimplemented cancellation semantics.

  Verified (macOS, real Docker daemon, `CODEAGENT_REQUIRE_DOCKER=1`,
  post-third-correction-pass totals): the fourteen-file focused set
  (`test_lifecycle_fs`, `test_repo_identity`, `test_state_locks`,
  `test_state_root`, `test_lifecycle_store`, `test_checkpoint_session`,
  `test_checkpoint_ref`, `test_reconciliation`, `test_bounded_subprocess`,
  `test_container_lifecycle`, `test_docker_ownership`, `test_executor`,
  `test_ci_container_leftover_check`, `test_workspace`) collected and
  passed together, 1,160 passed (up from 1,159), in both forward and
  reverse file order; the complete suite, 2,733 passed (up from 2,732), 0
  skipped; `git diff --check` clean; no leftover `codeagent-*`
  containers, extra worktrees, `refs/codeagent` refs, or temp state roots
  afterward; the real default macOS state-root location (`~/Library/
  Application Support/CodeAgent`) confirmed never created by this
  slice's tests. Docker was already running before all three review
  passes; it was never started or restarted, and no administrator-access
  dialog appeared. A real (non-Docker) regression test in `test_reconciliation.py`
  proves `reconcile_repository()` genuinely refuses/blocks when a real,
  deterministically-placed worktree is found registered and present
  (simulating a crash before disposal) — a scenario that could not
  previously be constructed with real Git at all, only simulated via a
  mock. **Explicitly not claimed by this slice**: worktree write-ahead
  publication or removal during reconciliation (both remain
  unimplemented, unchanged from every prior slice's own stated scope),
  any `RunController`/CLI/composition wiring, and concurrent-live-run
  mitigation — both remain unchanged and unimplemented.

  **Confirmed on GitHub-hosted Linux CI** (commit
  `7ec1fd2ce892c123491492c95ef6a990f841cbd2`, run
  [36642099820](https://github.com/G-ChandraSekhar/codeagent/actions/runs/36642099820),
  `ubuntu-24.04` x86_64 — confirmed directly in the log: "Operating
  System: Ubuntu 24.04.5 LTS," "Runner Image: ubuntu-24.04" — Python
  3.12, conclusion `success`, every step succeeded): the mandatory
  Docker preflight succeeded (Docker Engine - Community, version
  `28.0.4`, directly present in the log); the pinned verification image
  (`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
  was pulled and confirmed `linux/amd64`. The dedicated "Run real Docker
  verification tests" step ran only `python -m pytest
  tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER=1`
  confirmed present in that step's own logged environment): `3 passed
  in 1.69s`, 0 skipped — this is this file's own legacy Milestone-1
  suite, exactly, and is **not** specific evidence for Slice 3C-2, which
  adds no real-Docker test of its own. The separate "Run complete test
  suite" step (`python -m pytest -q`, `CODEAGENT_REQUIRE_DOCKER=1` also
  confirmed present in that step's own logged environment) reported
  `2730 passed, 3 skipped in 39.17s`; `2730 + 3` equals the local
  collected total of 2,733. The run used `pytest -q`, which prints no
  test identities, so these 3 skips are not identified from the log and
  are not guessed here — they are not evidence of Docker unavailability,
  since the daemon, the pulled image, and `CODEAGENT_REQUIRE_DOCKER=1`
  were all already confirmed for that same step. Source inspection —
  not CI-log evidence — continues to identify the same three
  Linux-unconditional, platform/host-specific skips named in every
  earlier slice's own entry (`test_evidence.py`'s Darwin-only
  ambient-`/tmp`-symlink test, `test_lifecycle_fs.py`'s Darwin-only
  case-canonicalization test, and `test_repo_identity.py`'s
  case-insensitive-filesystem-dependent alias test) as the likely
  candidates; this is stated separately as source-based inference, not
  something this run's own log proves. The final leftover-container
  check ran the anchored `grep -E
  '^codeagent-(verify-|baseline-|verification-)'` pattern over a
  complete, unfiltered `docker ps -a --format '{{.Names}}'` listing,
  covering all three CodeAgent container families; its captured output
  was empty and the step succeeded. This is implementation/automated-test
  evidence only — it does not constitute or substitute for a security
  review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
  specifically, not a general Linux or ARM64 portability claim. It does
  not claim `RunController`/CLI/composition-root wiring, lifecycle-
  projection worktree attribution, or any new T-E1 mitigation exists —
  Slice 3C-2 remains an unwired prerequisite; none of that is part of
  this slice. `docs/threat-model.md`'s T-E1 entry is **unchanged**:
  nothing here is wired into a real entry point yet.
- **Milestone 3 Slice 3C-3** (`RunController` ordinary-exception
  terminalization boundary), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 9 (Accepted 2026-10-02)", is implemented, locally verified
  on macOS (2026-10-02), committed (`bd90a992a001a2f9e9aed42ea65138802065f5cb`),
  and confirmed on GitHub-hosted Linux CI (run
  [37052793645](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37052793645),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see the
  closing paragraph below for the exact evidence this run supports).
  Closes an independently-discovered, present gap
  flagged during Slice 3C-2's own review: `RunController.run()` had no
  top-level exception boundary, so an unexpected ordinary `Exception`
  from any collaborator propagated straight out of `run()`, skipping
  evidence capture, worktree disposal, checkpoint-ref deletion, and
  owner-state cleanup entirely. `run()`'s post-start body is extracted
  into `_run_after_start()` and wrapped in a `try/except Exception:`
  whose fallback — placed strictly *after* the `except` block exits,
  never inside it, with the caught exception never bound to a name —
  calls `_terminate()` exactly once, producing
  `RunFinished(terminal_reason=UNRECOVERABLE_ERROR,
  error.code=ErrorCode.UNCLASSIFIED_FAILURE)` (no new `ErrorCode`; its
  existing contract already permits exactly this use) with one fixed,
  sanitized message. `_terminate()` itself gains a genuine one-shot
  guard (a check-and-raise, not a bare flag write) as its first two
  lines, so an exception escaping an already-started `_terminate()`
  propagates unchanged and is never re-terminalized. Deliberately
  narrow: `BaseException` subclasses that are not `Exception`
  (`KeyboardInterrupt`/`SystemExit`/`GeneratorExit`) pass through
  untouched by construction; signal/cancellation semantics remain ADR
  0005's domain (accepted, unimplemented); the initial `RunStarted`
  emission and `RUN_STARTED` transition stay outside the guarded
  region; every existing recognized-failure path, and every existing
  `_terminate()` cleanup/precedence rule, is unchanged. New tests in
  `tests/integration/test_controller.py` cover unexpected exceptions
  from lifecycle-owner activation, baseline verification, the model/
  read-plan phase, approval, patch application (after a real checkpoint
  ref is established), and non-baseline verification; `KeyboardInterrupt`/
  `SystemExit` propagation; a direct second-`_terminate()`-call
  regression; and a real end-to-end regression proving a fallback
  teardown failure's `__context__`/`__cause__` are both `None` — the
  discarded collaborator exception is never implicitly chained. One
  pre-existing test, `test_fake_model_gate_actually_fails_without_the_
  marker`, required a correction (not a weakening): it had asserted an
  `AssertionError` (itself an ordinary `Exception`) escaped `run()` raw,
  true only because no boundary previously existed; it now asserts the
  new, correct, intended behavior. Verified: `test_controller.py` alone,
  60 passed; the directly affected set (`test_controller.py` +
  `test_errors.py` + `test_events.py`), 806 passed, forward and reverse
  file order; the established fifteen-file focused Milestone-3 set (the
  prior fourteen-file set plus `test_controller.py`), 1,220 passed,
  forward and reverse file order; the complete suite with
  `CODEAGENT_REQUIRE_DOCKER=1`, 2,744 passed, 0 skipped; `git diff
  --check` clean; no leftover `codeagent-*` containers, extra
  worktrees, `refs/codeagent` refs, lingering processes, or temp/
  default state roots afterward. **Not implemented** (unchanged scope):
  `prepare_lifecycle()` production wiring, any composition root, the
  worktree transition table, `LifecycleLease` ownership/close-timing,
  CLI/UI, signal handling. `docs/threat-model.md`'s T-E1 entry is
  **unchanged** — this is a controller-internal correctness fix, not a
  new concurrent-run or lifecycle-attribution mitigation.
  **A same-day correction pass (2026-10-02)** found and closed four
  gaps between the reviewed acceptance criteria and what the first pass
  actually tested/documented, with no change to the production control
  flow above: added a test proving the fallback path drives the
  identical `activate()`→`begin_cleanup()`→`complete()` owner-state
  sequence to completion when activation itself had already succeeded
  (distinct from the activation-failure test, which correctly keeps
  `begin_cleanup()`/`complete()` never attempted); added two precedence
  tests specific to the new fallback path (an evidence-capture failure
  and a workspace-disposal failure, each overriding
  `UNCLASSIFIED_FAILURE` exactly per the existing, unchanged precedence
  rules) and removed the previously-unused permissive
  `allow_override_codes` helper parameter in favor of fully explicit
  assertions in those two tests; strengthened the fallback
  teardown-chaining regression with a direct `_terminate()` call-count
  spy, escaping-instance identity assertion, and a scan of every
  already-emitted event (not only the escaped exception's own `str()`)
  for the discarded collaborator exception's text/type; and corrected
  an unqualified claim in Amendment 9 that no "collaborator input" is
  ever persisted — the accurate, narrower claim is that no detail
  *derived from the caught exception itself* is ever persisted,
  emitted, logged, interpolated, or chained, which does not and cannot
  claim anything about ordinary events emitted before the exception
  occurred. Verified: `test_controller.py` alone, 63 passed (up from
  60); the directly affected set, 809 passed (up from 806), forward and
  reverse file order; the fifteen-file focused Milestone-3 set, 1,223
  passed (up from 1,220), forward and reverse file order; the complete
  suite with `CODEAGENT_REQUIRE_DOCKER=1`, 2,747 passed (up from
  2,744), 0 skipped; `git diff --check` clean; no leftover resources.
  Scope and `docs/threat-model.md`'s unchanged status are unaffected.
  **Confirmed on GitHub-hosted Linux CI** (commit
  `bd90a992a001a2f9e9aed42ea65138802065f5cb`, run
  [37052793645](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37052793645),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success`, 1 job, 13
  steps, every step `success`): the mandatory Docker preflight step
  succeeded (Docker Engine - Community, version `28.0.4`, directly
  present in the log); the pinned verification image
  (`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
  was pulled and confirmed `linux/amd64`. The dedicated "Run real
  Docker verification tests" step ran only `python -m pytest
  tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER=1`
  confirmed present in that step's own logged environment): `3 passed
  in 2.37s`, 0 skipped — this is this file's own legacy Milestone-1
  suite, exactly, and is **not** specific evidence for Slice 3C-3, which
  adds no real-Docker test of its own. The separate "Run complete test
  suite" step (`python -m pytest -q`, `CODEAGENT_REQUIRE_DOCKER=1` also
  confirmed present in that step's own logged environment) reported
  `2744 passed, 3 skipped in 49.20s`; `2744 + 3` equals the local
  collected total of 2,747. The run used `pytest -q`, which prints no
  test identities, so these 3 skips are **not identified from this
  run's own log** and are not guessed here — they are not evidence of
  Docker unavailability, since the daemon, the pulled image, and
  `CODEAGENT_REQUIRE_DOCKER=1` were all already confirmed for that same
  step. Source inspection — not CI-log evidence — continues to identify
  the same three Linux-unconditional, platform/host-specific skips
  named in every earlier slice's own entry (`test_evidence.py`'s
  Darwin-only ambient-`/tmp`-symlink test, `test_lifecycle_fs.py`'s
  Darwin-only case-canonicalization test, and `test_repo_identity.py`'s
  case-insensitive-filesystem-dependent alias test) as the likely
  candidates; this is stated separately as source-based inference, not
  something this run's own log proves. The final leftover-container
  check ran the anchored `grep -E
  '^codeagent-(verify-|baseline-|verification-)'` pattern, covering all
  three CodeAgent container families, over a complete, unfiltered
  `docker ps -a --format '{{.Names}}'` listing; its captured output was
  empty and the step succeeded. This is implementation/automated-test
  evidence only — it does not constitute or substitute for a security
  review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
  specifically, not a general Linux or ARM64 portability claim. It does
  not claim any composition root, worktree lifecycle-projection table,
  `LifecycleLease` ownership decision, CLI/UI, or signal/cancellation
  implementation exists, and changes no cleanup precedence or exception
  semantics beyond what this slice's own implementation section above
  already states. `docs/threat-model.md`'s T-E1 entry remains
  unaffected — this is a controller-internal correctness change, not
  production lifecycle composition or concurrent-run protection.
- **Milestone 3 worktree-attribution substrate slice** (`worktree_lifecycle.py`
  and `lifecycle_store.py` extensions), per
  `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 10 (Accepted 2026-10-02)", is implemented, locally verified
  on macOS (2026-10-02), committed (`f41da250bb7e67ea2b42113e17e25f3ec8143436`),
  and confirmed on GitHub-hosted Linux CI (run
  [37060981509](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37060981509),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success` — see the
  closing paragraph below for the exact evidence this run supports).
  Resolves Amendment 9's own named remaining open
  question — the worktree transition/combination table — after a joint
  review found both originally-proposed `expected_head` designs
  incomplete: an independently-republished field would require new
  `RunController`/`CheckpointSession` ordering (not substrate-only), and
  a cross-field rule requiring live `HEAD == checkpoint_ref.accepted_sha`
  is provably wrong, confirmed by direct code trace —
  `GitPatchApplier.apply()` commits directly into the worktree (moving
  real `HEAD` to `B`) *before* `CheckpointSession.advance()` is ever
  called, and `advance()` durably publishes `ADVANCING(A, A, B)`
  *before* the Git CAS runs, so `checkpoint_ref.accepted_sha == A` while
  real worktree `HEAD` is already `B` is the **normal window on every
  successful patch**, not a crash edge case. **Resolved interpretation**:
  `worktree.expected_head` is the immutable materialization/origin
  commit for one worktree incarnation — fixed at `creating`, retained
  unchanged through `present`/`disposing`, cleared at `disposing->absent`
  — never republished on checkpoint advances, never compared against
  live `HEAD` for ownership (ADR 0004 section 8's own already-accepted
  text never required `HEAD`-matching). `src/codeagent/
  worktree_lifecycle.py` (new, dependency-light, stdlib-only, mirroring
  `container_lifecycle.py`/`lifecycle_owner.py` exactly): `WorktreeIntent`
  (moved here from `lifecycle_store.py`, re-imported there for source
  compatibility), `WorktreeTransition` (a validated value object now
  used as `LifecycleProjection.worktree`'s own field type — the
  identical consolidation `checkpoint_session.CheckpointTransition`
  already made for `checkpoint_ref`), `WorktreeTransitionPublisher`
  Protocol, `WorktreePublicationFailure`/`WorktreePublicationError`
  (identical 10-member taxonomy to the container/owner-state precedent).
  **Correction pass**: the old `lifecycle_store.WorktreeAttribution`
  class — public, non-underscored — is kept as an exact identity alias
  (`WorktreeAttribution is WorktreeTransition`), not retired, since a
  repository-wide grep finding only one internal reference cannot prove
  no external caller imports it; construction through the alias is now
  deliberately stricter than the old unvalidated dataclass was (see
  `ENGINEERING_LOG.md`'s dated correction-pass entry for the full list
  of fixes this pass made, including a corrected "real Git-derived
  SHAs" claim below, now "object-format-valid synthetic SHA-1/SHA-256
  OIDs").
  `lifecycle_store.py`: `_validate_worktree_shape` extended to the full
  four-shape table with object-format-aware OID validation (reusing the
  existing `_is_valid_oid_for_format` helper, identical rejection
  behavior to `checkpoint_ref`'s own fields); `_validate_worktree_edge`
  implementing the legal owner edges plus immutable-OID continuity;
  `_LifecycleProjectionWriter.record_worktree_transition()` with the
  identical lock/stale/state-gate/shape/edge/publish ordering every other
  resource-transition method already uses; `LifecycleWorktreePublisher`
  (identical `__init__`/`_from_cursor`/`publish`/`refresh` shape to
  `LifecycleCheckpointRefPublisher`, with `lifecycle_id`/`state_root_id`
  identity properties mirroring `LifecycleContainerPublisher`'s own, for
  a future integration's benefit) with its own exhaustive
  `_WORKTREE_PUBLICATION_FAILURE_MAP`; `SharedLifecyclePublishers`/
  `create_shared_lifecycle_publishers` extended to a 4th
  `worktree_publisher` field sharing the existing `LifecycleProjectionCursor`
  — avoiding the Slice 3B-7 independent-stale-cursor bug by construction,
  for this 4th facade exactly as for the first three. **Deliberately
  out of scope, all later, separately-scoped Milestone 3 work**: any
  `workspace.py`/`GitWorktree` publication call (no production
  integration — this slice is a substrate only, proven via direct
  writer/publisher calls using object-format-valid synthetic SHA-1/
  SHA-256 OIDs, not real `git rev-parse`-derived commits and not a real
  worktree), any `RunController`/composition-root wiring, any
  `checkpoint_session.py` change, any `reconciliation.py` change or
  worktree removal (the eligibility predicate `is_projection_
  reconciliation_eligible_shape` still requires worktree `ABSENT`,
  unchanged), the `StateRoot.reserve_worktree_leaf()`/lifecycle-
  projection crash-gap interaction (named, not resolved), CLI, signal
  handling, abandonment, or model/UI work. An earlier, non-accepted
  exploratory sketch — a reconciler-table row refusing a `present`/
  `disposing` worktree on live-`HEAD` disagreement — was raised only
  during review, never published, and does not survive the ADVANCING-
  window analysis; it is explicitly not part of this slice or ADR
  Amendment 10. Four pre-existing tests required correction (not
  weakening), since stopping the categorical refusal of non-absent
  worktree shapes is this slice's entire point — see
  `ENGINEERING_LOG.md`'s dated entry for the exact four and their
  rationale. Verified (post-correction-pass totals): `test_worktree_
  lifecycle.py` (new), 27 passed; `test_lifecycle_store.py`, 261 passed
  (up from 210 before this slice; 256 after the first implementation
  pass; 261 after the same-day correction pass's own five new tests —
  see `ENGINEERING_LOG.md`'s dated correction-pass entry for the exact
  list); both files together, 288 passed; the sixteen-file focused
  Milestone-3 set (the fifteen-file set established since Slice 3A-1,
  plus the new file), 1,304 passed, forward and reverse file order; the
  complete suite with `CODEAGENT_REQUIRE_DOCKER=1`, 2,828 passed, 0
  skipped; `git diff --check` clean; no leftover resources.
  `docs/threat-model.md` is **unchanged** — this slice adds no
  production wiring, so T-E1/T-F2 remain exactly as stated ("not
  implemented"/"none yet" for worktree removal), unaffected.
  **Confirmed on GitHub-hosted Linux CI** (commit
  `f41da250bb7e67ea2b42113e17e25f3ec8143436`, run
  [37060981509](https://github.com/G-ChandraSekhar/codeagent/actions/runs/37060981509),
  `ubuntu-24.04` x86_64, Python 3.12, conclusion `success`, 1 job, 13
  steps, every step `success`): the mandatory Docker preflight step
  succeeded (Docker Engine - Community, version `28.0.4`); the pinned
  verification image
  (`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`)
  was pulled and confirmed `linux/amd64`. The dedicated "Run real
  Docker verification tests" step ran only `python -m pytest
  tests/integration/test_slice_c.py -v` (`CODEAGENT_REQUIRE_DOCKER=1`
  confirmed present in that step's own logged environment): `3 passed
  in 2.31s`, 0 skipped — this is this file's own legacy Milestone-1
  suite, exactly, and is **not** specific evidence for the worktree-
  attribution substrate slice, which adds no real-Docker test of its
  own. The separate "Run complete test suite" step (`python -m pytest
  -q`, `CODEAGENT_REQUIRE_DOCKER=1` also confirmed present in that
  step's own logged environment) reported `2825 passed, 3 skipped in
  48.81s`; `2825 + 3` equals the local collected total of 2,828. This
  is the step through which Amendment 10's new worktree tests
  (`test_worktree_lifecycle.py` and the additions to
  `test_lifecycle_store.py`) actually executed. The run used `pytest
  -q`, which prints no test identities, so these 3 skips are **not
  identified from this run's own log** and are not guessed here — they
  are not evidence of Docker unavailability, since the daemon, the
  pulled image, and `CODEAGENT_REQUIRE_DOCKER=1` were all already
  confirmed for that same step. Source inspection — not CI-log
  evidence — continues to identify the same three Linux-unconditional,
  platform/host-specific skips named in every earlier slice's own entry
  as the likely candidates; this is stated separately as source-based
  inference, not something this run's own log proves. The final
  leftover-container check ran the anchored `grep -E
  '^codeagent-(verify-|baseline-|verification-)'` pattern, covering all
  three CodeAgent container families, over a complete, unfiltered
  `docker ps -a --format '{{.Names}}'` listing; its captured output was
  empty and the step succeeded. This is implementation/automated-test
  evidence only — it does not constitute or substitute for a security
  review, and is GitHub-hosted `ubuntu-24.04` x86_64 evidence
  specifically, not a general Linux or ARM64 portability claim. It does
  not claim any change to the accepted design, schema, transition
  table, or scope stated above, and `docs/threat-model.md`'s T-E1/T-F2
  entries remain unaffected.
- **Milestone 3 optional `GitWorktree` worktree-transition
  publication**, per `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md`'s
  "Amendment 11 (Accepted 2026-10-02)", **is implemented and locally
  validated on macOS (2026-10-02); not committed, Linux CI pending.**
  `GitWorktree` gains an optional `worktree_publisher` (default `None`;
  no-publisher behavior unchanged) bound to the reservation's
  `repo_key`/`state_root_id`/`lifecycle_id` before Git preflight;
  publishes `creating` before any Git mutation, `present` after
  materialization+`consume()` outside the cleanup-owning block,
  `disposing` before removal, and `absent` only after confirmed
  registration absence and `observe_leaf() is ABSENT` (new
  `state_root.LeafObservation`); `preserve()` publishes nothing. A
  failed `present` always retains the worktree; failures latch on a
  separate `lifecycle_error` (`GitWorktreeLifecycleError`), re-raised as
  the same instance, never masking a body exception; no refresh/retry,
  no `creating→absent` recovery. `WorktreeTransitionPublisher` gained
  `repo_key` (contract change). **This is an unwired producer seam**: no
  production composition path supplies the publisher, and
  lifecycle-aware production composition must not be enabled until
  worktree reconciliation and removal exist. Crashes currently block
  admission (`RECONCILIATION_BLOCKED`, proven by real SIGKILL tests)
  rather than recover; the reservation/projection gap remains open;
  T-E1/T-F2 are not newly mitigated. Verified: full suite 2,904 passed,
  0 skipped (`CODEAGENT_REQUIRE_DOCKER=1`); 17-file focused set 1,380,
  forward and reverse. Implementation/test evidence only, not a
  security review. See `ENGINEERING_LOG.md`'s dated entry.
- One Stage-2 spike is unstarted: Responses API strict function tools
  and multiple tool calls. (A sixth spike, JSONL replay into the first
  frontend view, is also listed in the handoff and unstarted.)

The flagship fixture repository used by slice B/C
(`tests/fixtures/retry_worker/`) is a narrow, hand-built stand-in for
this and has not yet been replaced by the empirically validated
flagship fixture repository named in `docs/PROJECT_BRIEF.md`.
