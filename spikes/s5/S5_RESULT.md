# S5_RESULT.md — interruption, cancellation, orphan-resource reconciliation

**Status: DRAFT / author review pending.** This document reports what
was directly observed on both macOS/arm64 and Linux/x86_64. The macOS
record contains one authoritative run plus seven preserved historical
runs (one of which failed on a real bug and is retained deliberately);
the Linux record contains one authoritative GitHub Actions run and its
independent workflow diagnostics. It does not itself decide anything —
no label schema, registry location, lock mechanism,
startup-reconciliation policy, or signal-handling policy is accepted
on the author's behalf by this document. See "Candidate decisions"
below for what this evidence could inform, not what it settles.

## Scope

Linux and macOS remain separate evidence domains under
`docs/threat-model.md` A9. The original macOS/Docker Desktop evidence
and the later Linux/x86_64 reproduction are reported separately below;
neither substitutes for the other. Every mechanism exercised here
— the three-tier Docker label schema, the durable JSON manifest, the
`fcntl.flock` advisory lock, cooperative signal handling, and
reconciliation — is **spike-only scaffolding**. Current production
`codeagent.executor.DockerVerifier` and `codeagent.workspace.
GitWorktree` have **none** of this: no labels, no cancellation wiring,
no durable manifest, no signal handling, no startup reconciler.
`src/codeagent` was not modified; `codeagent.executor.DEFAULT_IMAGE`
is imported read-only for the pinned verification image.

`GitWorktree` cannot itself implement this experiment's child-owned,
precomputed-path worktree (its `__enter__` always allocates its own
`TemporaryDirectory` with no destination parameter) — `spike_s5.py`
reimplements a minimal, spike-local worktree lifecycle reusing
`GitWorktree`'s *principles* (structured argv, exact
`git worktree list --porcelain` registration parsing) but not its code.

## What was run

**Authoritative run**: `spikes/s5/evidence/macos-docker-desktop-arm64/run-ffd711753021/`,
produced by `spikes/s5/spike_s5.py` with no arguments after a fifth,
surgical correction pass on top of the fourth (below): the `except
BaseException` block now performs exactly one operation,
`experiment_exception = exc`, with no `elog` call — the
original-exception log message is instead constructed inside the
already-guarded phase-9 run-log block, so a failure in `elog` itself
can no longer run inside the unguarded `except`, before
`experiment_exception` is even assigned.
`RUN_INFO.json` records the exact harness that produced this evidence
by SHA-256 (`harness_sha256`, independently re-verified against the
on-disk `spike_s5.py` after this run) — the repository `HEAD`
(`1de3c019f3afeb50c3bea41536f83f7cc68803dd`) does **not** represent
this code: `repository_dirty` is `true`, since `spikes/s5/` is entirely
uncommitted. Host: Darwin 25.6.0 (arm64), Docker Desktop, Docker Engine
29.7.2, Python 3.12.14.

Prior correction (fourth pass) fixed a real defect the report before
it had *claimed* was already fixed but was not: `run_full_experiment`
used a `try`/`except` followed by ordinary sequential cleanup
statements, not a real `try`/`except`/`finally` — an unprotected
`write_evidence` or `elog` call during finalization could itself
raise, which would propagate out of the function and **replace** the
original experiment exception with that secondary failure, and would
skip every finalization phase that came after it. That pass made it a
genuine `try`/`except`/`finally`: the `finally` runs all nine
finalization phases — emergency cleanup, its evidence write, the
fixture-worktree inspection, its evidence write, scratch-root removal
and absence check, the final Docker/host-checkout inspection, its
evidence write, summary construction/write, and the run-log write —
each independently wrapped in its own `try`/`except` so a failure
anywhere among them is appended to `secondary_cleanup_failures` and
never prevents a later phase from running. Only *after* the `finally`
block has fully completed does the function decide what to raise: the
original exception if one occurred (re-raised via `raise
experiment_exception`, which carries its own original traceback),
never converted into success and never replaced by a secondary
failure; otherwise, if any secondary failure occurred, a new
`RuntimeError` summarizing them (so `overall_verdict` can never read
`PASS`); otherwise the real summary.

**Historical, preserved unchanged, not authoritative**:
- `run-14ddb5f761dd/` — the original pre-correction run. Its
  scenario/reconciliation/canary behavior was already correct; it
  predates every correction pass below.
- `run-036ef88e95a7/` — an intermediate run made during the first
  correction pass. Its actual tested behavior was correct
  (`overall_verdict: PASS`), but a wiring bug meant three of the
  child's `write_manifest_state` calls bypassed the newly added
  snapshot-directory parameter, so `manifest_snapshots/` is missing
  those states for each run token.
- `run-f578bd84cabc/` — the authoritative run from the first correction
  pass; superseded only because later passes followed it.
- `run-fa300398863b/` — the authoritative run from the second
  correction pass (unconditional cleanup as *claimed* — not yet
  accurate at that point); superseded by later passes.
- `run-57f62574cdce/` — a **FAILED** run from the third correction
  pass, retained deliberately rather than discarded.
  `overall_verdict: FAIL`, `scenario_classifications.reconciliation:
  "FAIL"`, specifically `canaries_unchanged: false`, even though the
  underlying reconciliation itself was correct. Root cause: the
  just-added scenario/sigkill children were appended to the *same*
  `inventory` list the canary-snapshot comparison read from, so the
  "after" snapshot had more entries than the "before" one — a real
  regression, not a flaky Docker/timing issue; the environment was
  independently confirmed fully clean immediately after this run,
  because emergency cleanup itself was unaffected by the bug. Fixed by
  snapshotting a separate, frozen `canary_inventory` list.
- `run-76f65b1cb95f/` — the authoritative run from the third correction
  pass (inventory completeness and per-phase `try`/`except` guards, but
  not yet a genuine `try`/`finally`).
- `run-de1003958f0d/` — the authoritative run from the fourth
  correction pass (the genuine `try`/`except`/`finally` structure
  described above); superseded only because the fifth, surgical pass
  (removing the fallible `elog` call from the bare `except` block)
  followed it.

All seven prior runs are retained rather than deleted, per this
project's rule against silently overwriting or discarding a prior
run's evidence — including the one that failed.

## Linux/x86_64 reproduction

**Authoritative run**: GitHub Actions workflow run
[`34783737248`](https://github.com/G-ChandraSekhar/codeagent/actions/runs/34783737248),
attempt 1, from source commit
`3d25fa6a6ec2575c97c716aeccc3bbbea18fc9b7`. Retained evidence:
`spikes/s5/evidence/linux-x86_64/run-34783737248-attempt-1/`;
independent workflow-level baseline/final diagnostics are retained
under its `workflow_diagnostics/` subdirectory. Two raw Git-worktree
listings have only their terminal blank record separator removed to
satisfy the repository's whitespace check; their substantive lines
remain identical to the downloaded artifact.

The run completed successfully on Ubuntu 24.04 (`Linux
6.17.0-1022-azure`, x86_64), Python 3.12.14, Docker client/server
28.0.4, cgroup v2. The harness SHA-256 in `RUN_INFO.json` matches the
tracked `spike_s5.py` at the source commit, the S5 baseline commit
`d2a6f63` is recorded as an ancestor, and the workflow URL/run ID/run
attempt all match the producing run. The scenario-5 lock and worktree
were observed on ext4 at the `/` mount only after both exact paths
existed and the child reached `READY`.

All seven recorded classifications passed: normal completion,
cooperative cancellation, SIGINT, SIGTERM, expected SIGKILL orphaning,
fresh-process reconciliation, and the separate idempotency check.
SIGKILL was confirmed by return code `-9`; the lock was busy before
the signal and acquirable afterward; the labeled container and
registered worktree remained until reconciliation; reconciliation
removed exactly those resources while preserving all canaries.
Harness cleanup reported `emergency_cleanup_all_clean: true`,
`scratch_root_removed_confirmed: true`, a fixture repository with only
its main worktree, and exact baseline/final equality. The independent
workflow diagnostics likewise reported no capture failures, comparison
failures, or leftovers. The workflow ran 175 focused S5 tests before
the experiment. No production code was exercised as an S5 lifecycle
implementation, and no candidate decision was accepted by this run.

| Evidence domain | Lifecycle/reconciliation result | Independent cleanup result |
| --- | --- | --- |
| macOS/arm64, Docker Desktop | All seven classifications PASS | Harness cleanup and exact baseline/final checks PASS |
| Linux/x86_64, GitHub Actions | All seven classifications PASS | Harness checks and workflow diagnostics PASS |

## Observations (directly inspected, not inferred)

**Scenarios 1–4 (normal completion, cooperative cancellation, SIGINT,
SIGTERM) — all PASS.** Each dedicated child reached `READY` with
`ready_barrier_ack_at` recorded, was independently re-verified (real
`docker inspect` + real `git worktree list`) by the harness before any
action, received its action only after that confirmation, recorded the
scenario-specific acknowledgement field (`continue_received_at`,
`cancellation_observed_at`, `sigint_received_at`, `sigterm_received_at`)
via normal control flow (SIGINT/SIGTERM handlers only set a flag —
they never call Docker/Git themselves), reached manifest state
`COMPLETE`, and left both its container and worktree independently
confirmed absent.

**Scenario 5 (SIGKILL) — PASS, meaning "orphan observed and
attributed," not "cleanup happened."** Before the signal: `READY` +
`ready_barrier_ack_at` confirmed; the harness's own read-only lock
probe on the recomputed `lock_path` reported busy (immediately
released); worktree registration and container presence independently
re-verified. `os.kill(exact_pid, SIGKILL)` was sent to the one PID this
harness's own `subprocess.Popen` returned. After: `child.returncode ==
-9` (confirmed killed by SIGKILL); the harness's lock probe now
succeeded (immediately released again so scenario 6 acquired it
fresh); **no** post-action acknowledgement field
(`cleaning_started_at`, `complete_at`, `cancellation_observed_at`,
`sigint_received_at`, `sigterm_received_at`) was present — only the
pre-existing `ready_barrier_ack_at`, which is expected and required
before any signal; both the container (present, correct labels) and
the worktree (registered, directory present) were independently
confirmed still there. Full raw fields retained in
`check_scenario_5_sigkill.json`.

**Scenario 6 (fresh reconciliation) — PASS.** A separate process
(`python spike_s5.py --reconcile ...`, no shared in-memory state)
recomputed the scenario-5 entry's identity from trusted inputs,
independently inspected both resources live (not from the manifest's
own flags), validated container ID/labels and worktree
registration/containment, removed the container first and confirmed
its absence, only then removed the worktree via structured
`git worktree remove --force` and confirmed both directory and
registration absence, and wrote `RECONCILED` with
`reconciliation_attempts: 1` (an ordinary first-attempt success now
counts as exactly one attempt — the pre-correction accounting left
this at `0`). The standalone `--reconcile` command itself exited `0`
for this run (no `technical_failure`/`reconciliation_failed` entries),
and still emitted the same parseable structured JSON regardless of
exit status. All four canaries — unlabeled (A), similarly-named (B),
foreign-session-labeled (C), and the live concurrent run (D, real
dedicated child, own manifest, own held lock) — were independently
re-inspected and found byte-for-byte/name-for-name unchanged; D's
entry was explicitly reported `skipped_active` (its lock was still
held).

**Idempotency (second fresh reconciliation pass) — PASS.** Every
entry — the four `COMPLETE` scenario-1–4 manifests, the now-`RECONCILED`
scenario-5 entry, and D's still-`active` entry — was reported
`skipped_terminal`/`skipped_active`, and all canaries remained
unchanged. The live run itself does not instrument internal call
counts, so it is evidence only for the observable outcome (the correct
action label, and resources/canaries genuinely unchanged), not for the
mechanism's internal call counts. The stronger, exact claims —
`skipped_terminal` entries (`COMPLETE`/`RECONCILED`) take **zero**
lock, inspection, or mutation calls at all, while a `skipped_active`
entry (like D's) necessarily makes **one** lock attempt and then
**zero** inspection/mutation calls — are proven separately by the
focused test suite's call-recording/fail-if-called instrumentation
(`test_reconcile_skips_complete_with_zero_lock_or_inspection`,
`test_reconcile_skips_reconciled_with_zero_lock_or_inspection`,
`test_process_entry_active_lock_is_skipped`), not by this live run.

**Emergency cleanup (independent from the reconciler-under-test) —
all clean.** Canaries A, B, and C were removed by the harness's own
in-memory-recorded exact ID + expected label state (never a manifest
or a generic label sweep). Canary D received a cooperative shutdown
request (the same control-file `cancel` protocol as scenario 2) and
completed within the bounded wait — `mode: "cooperative"`, no SIGKILL
fallback was needed this run.

**Unconditional outer-harness cleanup (correction, proven by focused
tests, not exercised by this live run's happy path).** The live run
above never raises an unexpected exception, so it does not itself
demonstrate the real `try`/`except`/`finally` structure now wrapping
everything from immediately after scratch-root creation through the
idempotency check. That behavior is proven instead by Docker-free
fault-injection tests against a small in-memory fake Docker registry:
`test_run_full_experiment_unconditional_cleanup_on_exception` forces
one canary's creation to fail after two others already exist, and
confirms emergency cleanup still ran exactly once, targeted exactly
those two already-recorded resources (never a name/label sweep), the
scratch root and evidence were still written, the original exception
still propagated, and `overall_verdict` could not be `PASS`;
`test_run_full_experiment_exception_after_scenario_child_launch_cleans_that_exact_child`
launches a real *scenario* child (not just a canary), forces an
exception immediately after it is recorded in `inventory`, and
confirms that exact child's identity is what emergency cleanup
processes — this is the test that specifically closes the gap an
earlier version of this correction pass left open, where scenario/
SIGKILL children were never added to `inventory` at all;
`test_emergency_cleanup_continues_across_individual_failures_with_multiple_entries`
confirms one entry's unconfirmed removal does not stop the others from
being attempted and reported; `test_emergency_cleanup_isolates_an_unexpected_exception_to_one_entry`
confirms a genuine exception (not merely a nonzero Docker/Git result)
cleaning up one entry is caught and recorded as a sanitized
`technical_failure` for that entry alone, without aborting the rest.

**A genuine `try`/`except`/`finally` (correction, proven by focused
tests, not exercised by this live run's happy path).** Three further
Docker-free fault-injection tests specifically target the finalization
structure itself, each combining a real original experiment exception
(a canary creation failure) with a SECOND, independently injected
failure writing one particular evidence file (via a monkeypatched
`Path.write_text`) during finalization:
`test_run_full_experiment_survives_broken_emergency_cleanup_evidence_write`,
`test_run_full_experiment_survives_broken_fixture_worktree_evidence_write`,
and `test_run_full_experiment_survives_broken_summary_evidence_write_and_still_writes_run_log`.
Each confirms: the broken file itself is absent; every finalization
phase scheduled *after* the broken one still ran and wrote its own
evidence (including scratch-root removal and the final Docker/host
inspection); `summary.json`'s `experiment_exception` field names the
*original* canary failure, never the secondary write failure; and the
secondary failure is recorded, separately, in
`secondary_cleanup_failures`. The summary-write variant additionally
confirms `run.log` (the very next phase) is still written even though
the immediately preceding phase failed.

**Fixture-repository worktree cleanliness vs. CodeAgent-checkout
integrity — two distinct checks, both confirmed, and both required for
`overall_verdict: PASS`.** `fixture_worktree_check.json` captures the
temporary fixture repository's own `git worktree list --porcelain`
*before* the scratch root (which contains that repo) is deleted, and
confirms it registers **only its own main worktree**
(`has_only_main_worktree: true`) — this is the check that actually
demonstrates every scenario/canary worktree was removed.
`baseline_final_comparison.json`'s CodeAgent-checkout comparison is a
**separate, weaker fact**: it proves this run never registered a
worktree in, or otherwise disturbed, the real CodeAgent checkout
itself — a host-integrity guarantee that would hold even if the
fixture repository's own per-scenario worktrees were never cleaned up,
since nothing here ever targets the real checkout in the first place.
Neither check is presented as proof of the other's claim.

**Scratch-root removal and exact Docker baseline/final equality — both
confirmed, and both required for `overall_verdict: PASS`.**
`shutil.rmtree(..., ignore_errors=True)` was followed by an
independent `Path.exists()` check on the exact scratch root the
harness itself created (`scratch_root_removed_confirmed: true` —
`ignore_errors=True` alone is never treated as confirmation). The
exact, unfiltered `docker ps -a --no-trunc` output was captured
*before* this run created anything and again after emergency cleanup
and scratch-root removal; both matched byte-for-byte
(`baseline_final_comparison.json`, `exactly_equal: true`) — not merely
"looks empty," but identical raw text.

## Fail-closed child lifecycle (correction, proven by focused tests)

The real run above only exercises the happy path of container
creation/start/cleanup — it does not itself induce a `docker start` or
cleanup failure. That failure-path behavior is proven by the focused
test suite instead: `docker create`'s container ID is now persisted to
the manifest *immediately* on success, before `docker start` is even
attempted (`test_child_docker_start_failure_leaves_container_created_never_ready_or_complete`
confirms the container ID is present even though `start` then fails).
A failed `docker start` leaves the manifest at the nonterminal
`CONTAINER_CREATED` state with a fixed `failure_phase`/`failure_detail`
— never `READY`, never `COMPLETE` — so a later reconciliation pass can
still discover and act on it (it is not in `TERMINAL_STATES`). The
child's own cleanup (`perform_child_cleanup`) now removes and confirms
the container *before* ever touching the worktree, and if that
confirmation fails, the worktree is never touched and the manifest
stays at `CLEANING` with a fixed failure detail rather than `COMPLETE`
(`test_perform_child_cleanup_container_failure_never_produces_complete_or_touches_worktree`,
`test_perform_child_cleanup_worktree_failure_never_produces_complete`,
`test_perform_child_cleanup_failure_state_is_not_permanently_skipped_by_reconciliation`).

## Expected SIGKILL orphaning, stated plainly

Scenario 5's result is the **correct, expected** outcome for an
uncatchable signal, not a defect: no in-process Python code can run
after SIGKILL, so the child's normal teardown code — which exists and
works, per scenarios 1–4 — simply never executes. This is why external
reconciliation (scenario 6) is architecturally necessary, not merely
convenient, for any process that can be killed with SIGKILL — which is
every real process.

## Limitations

- Scratch-local manifests only; no `~/.codeagent/...`-style real
  location was prototyped.
- The advisory-lock mechanism's cross-process exclusion and automatic
  release after the exact holder process dies were empirically
  confirmed on macOS/arm64/APFS and Linux/x86_64/ext4. PID identity is
  not the authority, but PID reuse itself was not induced or observed;
  host-reboot behavior and network filesystems remain untested.
- Host-reboot recovery remains explicitly out of scope (consistent
  with `docs/threat-model.md`'s T-F4); only an application-level
  restart (a fresh reconciler process) was exercised.
- ADR 0003's checkpoint-cleanliness/resume-validation gate (a
  patch-application-safety concern) is untouched and out of scope for
  this spike, which only asks "does the resource still exist."
- The reconciliation-retry path (`RECONCILIATION_FAILED` → later
  success), the `docker start`/cleanup fail-closed paths, and the
  genuine `try`/`except`/`finally` finalization structure (including
  its now-minimal `except` block) were all exercised in the focused
  test suite with mocked/faked Docker and Git calls, not against a real
  induced live-Docker failure in any of the eight retained runs (none
  of which raised from the harness itself except the fifth, which
  failed on a real assertion inside the harness's own classification
  logic — a genuine bug, not an injected fault — and is retained as
  evidence of that, not of the finalization structure).
- The cooperative-cancellation control-file protocol is explicitly a
  spike-only mechanism, not a proposal for a production cancellation
  API.
- This correction pass itself is evidence that four prior "done"
  claims (unconditional cleanup; complete inventory coverage; a real
  `try`/`finally` structure; a minimal, side-effect-free `except`
  block) were each wrong when first reported — readers should treat
  any single pass's self-report with appropriate skepticism until
  independently re-verified, which is exactly how this defect, its own
  regression, and two further structural gaps were all found in turn.

## Candidate decisions this evidence could inform (none accepted here)

- Docker ownership-label schema (the three-tier `spike`/`session`/`run`
  labels demonstrated here are a candidate, not a decision).
- Durable orphan-registry design and location.
- Manifest-path/identity tamper protection (containment + recompute
  discipline demonstrated; no production-grade integrity mechanism
  proposed).
- Stale-vs-active determination policy (advisory-lock approach
  demonstrated as PID-reuse-safe locally; cross-reboot robustness not
  addressed).
- Startup-reconciliation trigger/scope in production.
- SIGINT/SIGTERM ownership and whether production should install
  handlers at all.
- Cooperative-cancellation API shape.
- Behavior when reconciliation itself fails (this spike's
  `RECONCILIATION_FAILED`/retry discipline is evidence for, not a
  decision about, production retry/alert/refuse-to-start policy).

None of the above is accepted by this document. All require an
explicit author decision, to be recorded separately (and, if it meets
the bar in `CLAUDE.md`'s documentation ladder, as an ADR) if and when
made.
