# CodeAgent — Stage 2 Evidence Plan

Status: Stage 2 is underway. S1, S3, and S4 have executed and produced
retained evidence; S3's resulting decision is accepted as
`docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`. S4
(container/executor isolation) executed on both macOS/Docker Desktop
and Linux/x86_64, found that `--memory` alone did not cap combined
memory+swap and that a Docker-confirmed OOM kill could not be
distinguished from an ordinary test failure, and both were resolved in
Milestone 3 commit `00063d4` (`security: enforce Docker memory ceiling
and classify OOM`) — a post-hardening follow-up then directly validated
that resolution on both platforms (`spikes/s4/S4_RESULT.md`). S5
(interrupt and lifecycle scenarios) has completed spike evidence on
macOS/arm64 and Linux/x86_64 (`spikes/s5/S5_RESULT.md`, evidence
complete), including independent clean workflow diagnostics on Linux;
its production design is accepted in
`docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md` and
`docs/adr/0005-cancellation-and-signal-ownership.md` but not yet
implemented. The remaining spikes
below (S2a, S2b, S7, S8, plus the non-spike S6) are still pending.
Production code exists for Milestone 0, Milestone 1,
and the Milestone 3 memory/OOM hardening (see `CLAUDE.md`'s current
status), but nothing from S3's accepted decision has been implemented
yet. This document does not authorize implementation on its own — each
spike still individually authorizes deciding, in writing, what evidence
an open uncertainty needs before it becomes a design decision; the
experiment plans below are retained as originally written for that
purpose, including for the spikes that have already run.

Read alongside the frozen `PROJECT_BRIEF.md` (accepted Stage 1
baseline), `CODEAGENT_IMPLEMENTATION_GUIDE.md`, `DESIGN_SPEC.md`, and
`BUILD_PLAN.md`.

## How to read this document

Each spike below follows one template:

- **Question** — the specific thing that's actually unknown.
- **Hypothesis** — the design's current working assumption, stated so it
  can be falsified, not just confirmed.
- **Smallest experiment** — the minimum standalone code/action that can
  produce real evidence, deliberately not "build the feature and see."
- **Pass/fail criteria** — checkable in advance, not judged after the
  fact.
- **Time box** — a proposed ceiling, not a target to fill.
- **Retained evidence** — exactly what survives the spike once it's
  done and the throwaway code is (usually) discarded.
- **Design decision enabled** — which specific open question this
  closes.

Two items are explicitly **not** spikes (S6, and the path-policy note
under S4) — flagged as such rather than forced into a template that
doesn't fit.

---

## Dependency graph (corrected)

The previous version of this plan overstated S1's blocking power,
understated how much of S3/S4/S6/S8 can run independently, and never
separated domain/controller work from the infrastructure spikes at
all. Corrected:

```
Domain/controller/budgets/events/reporting/approval — NOT blocked by
any spike below. Buildable and testable now, against fake/stub
workspace and executor interfaces (same pattern as llm/fake.py).

Tier 1 — independent, no real dependency on each other, any order:
  S1  (worktree+Docker+pytest+cleanup)
  S2a (Responses API live contract check)
  S2b (offline parser fixtures)               — can begin independently with
                                                 hand-built, documented-schema
                                                 fixtures; does not strictly
                                                 depend on S2a
  S3  (patch atomicity, 3 sub-experiments)    — needs only `git worktree`, not Docker
  S4  (container/executor isolation)          — needs only "a container can run with a mount"
  S6  (flagship Dockerfile — not a spike)      — standalone build, no dependency to start
  S8  (real-issue candidate discovery)         — sourcing/licensing only, starts anytime

Tier 2 — soft dependency on Tier 1's Docker lifecycle familiarity:
  S5  (interrupt scenarios)                   — benefits from S1/S4's container patterns

Tier 3 — genuine dependency:
  S7  (dependency preparation)                — needs S1's pattern AND S4's network-isolation findings

Tier 4 — genuine dependency:
  S8 full feasibility validation              — needs S7 resolved (discovery itself is Tier 1)
```

| Item | Blocks (production subsystem) | Blocks (portfolio/criteria) |
|---|---|---|
| S1 | `workspace/git_worktree.py`, `execution/docker.py`, `verification/pytest.py` | The *real* implementations of these modules only — not domain/controller work |
| S2a/S2b | `llm/openai.py` | Milestone 4 (real model loop); v1-useful criterion 5 |
| S3 | `tools/patch.py` | Within-worktree atomicity guarantees only — see S3 note on what it does NOT establish |
| S4 | `execution/docker.py` (security config) | The threat model's container-isolation claims specifically |
| S4-note | `execution/policy.py`, `tools/*.py` | Path-policy correctness — ordinary testing, not a spike |
| S5 | `execution/docker.py` (cleanup/reconciliation) | Any run that gets interrupted or crashes |
| S6 (not a spike) | Flagship fixture / benchmarks infra | v1-useful criterion 1 |
| S7 | A new dependency-preparation module | v1-complete criterion 10; all of v1.1 |
| S8 | `benchmarks/cases/` | v1-complete criterion 10 |

---

## S1 — Worktree + Docker + pytest + cleanup

**Question**: Can a disposable git worktree be created, mounted into a
Docker container, have its tests run inside that container, and be
fully cleaned up afterward — reliably, repeatedly, without leaking
CodeAgent-owned state?

**Hypothesis**: `git worktree add` + a bind-mount into a minimal
Python+pytest Docker image + `pytest` run + `git worktree remove` is
sufficient. No exotic tooling is needed for v1.

**Smallest experiment**: A standalone script that first records a
*baseline* inventory of existing git worktrees (`git worktree list`)
and Docker resources (`docker ps -a`) — not to require them empty, only
as a reference point, since other unrelated worktrees/containers on the
machine are expected and irrelevant. It then tags every resource it
creates with a unique per-run identifier (e.g. a
`codeagent-spike-s1-<uuid>` container name/label and matching worktree
path), creates a worktree from a small scratch repo, mounts it
read-write into a container, runs pytest inside, captures output, tears
the container down, removes the worktree — then repeats the full
sequence 3 times, each with its own identifier.

**Pass/fail criteria**: Pass if, after each of the 3 runs, no git
worktree or Docker resource bearing *that run's* unique identifier
remains — checked by identifier, never by requiring the machine's
entire inventory to be empty. Pass also requires the 3 runs' pytest
outcomes to be equivalent under a *normalized* comparison (same tests,
same pass/fail per test, same counts) — not byte-identical stdout,
since timestamps, durations, and warning ordering can legitimately
differ between otherwise-correct runs. Fail if any run leaves an
identifier-tagged resource behind, or normalized outcomes differ.

**Time box**: ~3-4 hours.

**Retained evidence**: the script (kept, not deleted); a short note of
the exact commands/flags that worked and any platform-specific
surprises.

**Design decision enabled**: confirms or refutes the core
Workspace + Docker Executor architecture — whether "worktree mounted
into Docker" is viable as designed, or needs a fallback.

**Code disposition**: throwaway driver script. The *validated pattern*
informs the real `workspace/git_worktree.py` and `execution/docker.py`,
not the literal code.

---

## S2a — Responses API live contract check

**Question**: What is the real shape of a Responses API response when
multiple tool calls are requested in one turn — how are multiple
tool-call output items structured, how does `call_id` correlate a
result back to its originating call, what does a continuation turn
(feeding a prior tool result back in) look like, and how is an
incomplete/truncated response status actually signaled?

**Hypothesis**: Documented behavior for these cases matches real
behavior closely enough that the parser can be built against the
documentation plus a small number of live confirmations — not
extensive live experimentation.

**Smallest experiment**: Against the real API: define 2+ real tool
schemas, send a prompt that plausibly triggers multiple tool calls in
one response and inspect every output item's structure and `call_id`;
construct a follow-up turn feeding a tool result back in via that
`call_id` and confirm correct continuation handling; if practical,
trigger an incomplete/truncated response (e.g. a very low
max-output-token setting) and inspect exactly how that status is
signaled.

**Pass/fail criteria**: Pass if the real shapes for multi-tool-call,
`call_id` correlation, continuation, and incomplete-status are captured
and understood well enough to write a parser against them with
confidence. Fail if real behavior here is ambiguous or inconsistent
across repeated attempts.

**Time box**: ~1-2 hours — a small number of live calls, not extensive
exploration.

**Retained evidence**: example raw request/response JSON for each case
(API key redacted) — added later as golden regression fixtures
alongside S2b's independently hand-built set, not a prerequisite to it.

**Design decision enabled**: confirms the real wire-level contract
`llm/openai.py` needs to parse.

**Code disposition**: throwaway script; its captured JSON is explicitly
added as golden regression fixtures alongside S2b's independent set,
not discarded.

---

## S2b — Offline deterministic parser fixture tests

**Question**: Does the tool-call parser correctly and deterministically
handle every edge case that matters — every response output item type,
`call_id` correlation, tool-result continuation, incomplete status,
unknown/unrecognized tool names, structurally malformed arguments,
duplicate `call_id`s, and local enforcement of the custom-tool budget —
without depending on the model actually producing any of these on
demand?

**Hypothesis**: A parser built and tested against hand-constructed
fixture JSON — written directly against the Responses API's *published,
documented* schema, not waiting on a live capture — can be validated
fully offline, with no API calls, and covers edge cases the model won't
reliably produce on request even when prompted for them. S2a's real
captures are added *later*, once available, as golden regression
fixtures alongside the hand-built ones — a valuable addition, not a
prerequisite to starting S2b.

**Smallest experiment**: A no-network test suite constructing fixture
JSON, hand-built against the documented schema, for: every response
output item type the parser must handle; correct `call_id` correlation
across a multi-call response; a tool-result continuation turn; an
incomplete-status response; a tool call referencing an unknown tool
name; structurally malformed arguments (invalid JSON, wrong types,
missing required fields) built by hand rather than hoping the model
produces them; a response with duplicate `call_id`s; and a sequence of
calls that should trip the locally enforced custom-tool budget (max
tool calls per iteration) — run the actual parser against every
fixture. Once S2a completes, fold its real captured responses in as
additional golden fixtures alongside the hand-built set.

**Pass/fail criteria**: Pass if every fixture produces the correct,
specific outcome: valid cases parse correctly; unknown-tool and
malformed-argument cases produce a clear, typed error rather than a
silent bad parse or unhandled exception; duplicate `call_id`s are
detected and rejected, not silently overwritten; the budget-enforcement
fixture halts before exceeding its configured limit. Fail on any silent
incorrect parse, unhandled exception, or an exceedable budget check.

**Time box**: ~2-3 hours — mostly fixture construction.

**Retained evidence**: the full fixture set — **this graduates directly
into `tests/unit/` as real regression tests**, an explicit exception to
the general throwaway pattern.

**Design decision enabled**: confirms parser edge-case handling and
local budget enforcement before either is exercised against the real,
non-deterministic model.

**Code disposition**: production-bound. Not thrown away.

---

## S3 — Patch atomicity: three distinct properties, not one

**Status: executed, decision accepted.** Evidence:
`spikes/s3/S3_RESULT.md` and its retained JSON. Decision:
`docs/adr/0003-recover-partial-patches-by-replacing-worktree.md`
(Accepted) — v1 requires abandon-and-recreate recovery (not per-file
rollback) plus a checked interruption-detection invariant before
trusting a worktree; true crash-consistent filesystem mutation is
explicitly not guaranteed. The planning text below is retained as
written before the spike ran, for the rationale it captures — it is no
longer the current status.

**Question**: Does CodeAgent's patch mechanism provide three genuinely
different guarantees — (a) **prevalidation atomicity**: rejecting an
invalid proposal before any file is touched; (b) **rollback after a
handled mid-application failure**: restoring prior state if a caught
exception interrupts a validated proposal partway through; and (c)
**crash consistency**: leaving the worktree in a safe, detectable state
if the process is killed outright mid-application? These are not the
same property, and passing one says nothing about the others.

**Hypothesis**: Prevalidation catches most real invalid-proposal cases
cheaply. It does **not**, by itself, provide true filesystem atomicity
for multi-file writes — a handled mid-application failure needs
explicit rollback logic, and a hard crash needs a fundamentally
different mechanism (e.g. write-to-scratch-then-atomic-rename) if true
crash consistency is required. This spike exists to find out how much
of that v1 actually needs versus can explicitly defer.

**Smallest experiment**: Three separate experiments against a scratch
worktree:
1. *Prevalidation*: a 3-operation proposal where the 3rd operation is
   invalid in a way detectable during validation (mismatched hash,
   ambiguous match) — confirm validation rejects it before any write.
2. *Handled-failure rollback*: a proposal where the 2nd operation's
   write step raises a caught exception after the 1st operation's write
   has already landed — check whether the system restores the 1st
   file's pre-apply state, or leaves it applied.
3. *Crash consistency*: repeat experiment 2's setup, but SIGKILL the
   process at the same point instead of raising a catchable exception —
   inspect the worktree's resulting state: fully original, fully
   applied, or genuinely ambiguous/corrupted?

**Pass/fail criteria**: Experiment 1 passes if the worktree is
byte-for-byte unchanged after rejection. Experiment 2 passes if the
worktree is restored to its exact pre-apply state after a handled
failure, **or** if the system clearly documents that it does not roll
back handled failures — a stated limitation is an acceptable pass; a
silent, undetected partial state is not. Experiment 3 passes if a hard
kill leaves the worktree unambiguously original or fully applied (via
an atomic rename/swap), or, failing that, the partial state is at least
*detectable* (a marker/incomplete-transaction flag) rather than
indistinguishable from valid state. An undetectable corrupted partial
state fails and requires redesign.

**Time box**: ~4-6 hours (three sub-experiments).

**Retained evidence**: the scripts; a short written note of the
validate-then-apply logic and exactly which of the three properties are
actually implemented vs. explicitly deferred (ADR candidate).

**Design decision enabled**: which of the three atomicity guarantees
`tools/patch.py` needs for v1, and which can be documented as a known
limitation instead of engineered around immediately.

**What this spike does NOT establish**: original-checkout isolation.
That guarantee comes from using a disposable worktree at all (S1's
domain), not from anything S3 tests. S3 is entirely about atomicity
*within* the worktree.

**Code disposition**: throwaway scripts; the validated algorithm is
likely substantially reused (refactored, not copied) in `tools/patch.py`.

---

## S4 — Executor/container isolation

**Status: executed, retained, followed by a successful cross-platform
post-hardening validation.** Evidence: `spikes/s4/S4_RESULT.md` and its
retained JSON under `spikes/s4/evidence/{macos-docker-desktop-arm64,
linux-x86_64}/`. The original run found two open questions (combined
memory+swap not capped by `--memory` alone; OOM-kill vs. ordinary
test-failure disposition), both resolved in Milestone 3 commit
`00063d4` and then directly validated by a separate post-hardening
follow-up on both platforms — see `S4_RESULT.md`'s own post-hardening
section for the full record. The experiment plan below is retained as
originally written, for the reason stated in this document's own
status paragraph above.

**Question**: Do Docker's container-level isolation primitives —
network disabled, non-root execution, dropped Linux capabilities,
no-new-privileges, no Docker-socket exposure, a read-only root
filesystem with narrowly bounded writable locations, and an explicit
environment allowlist — actually hold when a process inside the
container attempts to violate each, alongside enforced timeouts, output
caps, and bounded process-count limits?

This spike validates **executor/container isolation only** — not the
agent's own path-policy semantics (see the separate note below, which
is ordinary application testing, not a spike).

**Hypothesis**: Standard Docker flags (`--network none`,
`--user <non-root-uid>`, `--cap-drop=ALL` plus only strictly necessary
re-additions, `--security-opt=no-new-privileges`, no Docker-socket
mount, `--read-only` plus explicit mounts for the one or two locations
that must be writable, `--pids-limit`, `--memory`, `--cpus`, and an
explicit `--env` allowlist rather than inheriting the host environment)
are sufficient for v1's threat model — no custom seccomp profile or
heavier sandbox technology (gVisor, Firecracker) is needed.

**Smallest experiment**: Run a container configured with the full
intended flag set, then from inside attempt, one at a time:
- a real network call to a public IP (expect: fails);
- reading a path that is genuinely outside the container, via any real
  mount point — reading the container's **own** internal files (e.g.
  its own `/etc/passwd`) is explicitly excluded from this test; that
  file exists legitimately inside the container's own filesystem and
  reading it is not evidence of anything;
- confirming the process's UID is non-zero;
- confirming a capability that should be dropped (e.g. `CAP_SYS_ADMIN`)
  is genuinely unavailable, via an operation that requires it;
- confirming `no-new-privileges` blocks a setuid-style escalation;
- confirming `/var/run/docker.sock` is absent/inaccessible;
- attempting a write outside the bounded writable location(s) and
  confirming it fails (read-only root filesystem enforcement);
- confirming no unallowlisted host environment variable is visible
  inside the container;
- a process that runs past the configured timeout;
- a process that generates far more output than the configured cap;
- a **bounded** process-exhaustion test: attempt exactly
  (configured pids-limit + 1) processes and confirm the limit blocks
  the last one — never an uncontrolled fork bomb, which risks
  destabilizing the host machine running the spike itself.

**Pass/fail criteria**: Pass if every check behaves as intended
(blocked, capped, denied, or correctly absent), and the container is
left clean and killable after each. Fail if any check succeeds when it
shouldn't, or the bounded process-exhaustion test doesn't stop at the
configured limit.

**Time box**: ~4-5 hours (many individually-fast checks).

**Retained evidence**: the script and output per check, plus the exact
flag set that worked — this becomes the actual production
`execution/docker.py` configuration.

**Design decision enabled**: confirms (or disproves) that this specific
Docker flag set is sufficient for v1's threat model without a heavier
sandbox technology.

**Code disposition**: throwaway driver script; the discovered
configuration is retained directly as production config.

### S4-note — Repository path-policy checks (not a Stage 2 spike)

Confirming that `list_directory`/`read_file`/`grep` reject
`..`-traversal, absolute paths outside the repository root, and
symlinks resolving outside the mounted worktree is ordinary
deterministic application logic — standard unit/integration testing of
`execution/policy.py` / `tools/*.py`, not a genuine uncertainty needing
an evidence-gathering spike. Requires no Docker, no dependency on S4,
and can be written and tested at any point.

---

## S5 — Interrupt and lifecycle scenarios

**Status (factual, added after the fact — the experiment plan below is
retained as originally written and is not itself updated to describe
current behavior)**: macOS/arm64 and Linux/x86_64 spike evidence for all six scenarios
(five lifecycle outcomes plus fresh-process reconciliation), followed
by a separate idempotency check, is complete
(`spikes/s5/S5_RESULT.md`, evidence complete). Linux workflow run
`34783737248` also retained independent clean baseline/final
diagnostics. The production design this evidence informed is accepted
in `docs/adr/0004-owned-resource-lifecycle-and-reconciliation.md` and
`docs/adr/0005-cancellation-and-signal-ownership.md`, but no S5
mechanism is implemented in
`src/codeagent/executor.py`, `src/codeagent/workspace.py`, or anywhere
else in production code. The original **Code disposition** line below
("the reconciliation mechanism is directly reused in
`execution/docker.py`") no longer describes the plan: the S5 spike
code is spike-only and is not reused in production; any production
implementation reimplements the demonstrated principles with its own
tests.

**Question**: Under each of six distinct lifecycle scenarios — normal
completion, explicit cancellation, SIGINT, SIGTERM, an uncatchable
SIGKILL to the parent process, and a subsequent application restart —
what actually happens to the underlying Docker container and worktree,
and can CodeAgent detect and clean up anything left behind?

**Hypothesis**: Normal completion, cancellation, SIGINT, and SIGTERM
are all catchable and can be handled via in-process signal handlers
plus `docker run --rm`-style cleanup. SIGKILL to the parent process
**cannot** be handled this way — it is uncatchable and unblockable by
definition, so no in-process code runs when it arrives. Orphan cleanup
for that case can only come from an external mechanism: labeled-
resource reconciliation performed at CodeAgent's next startup, scanning
*only* for resources bearing CodeAgent's own identifying label, never
touching unrelated Docker resources on the machine.

**Smallest experiment**: Six short experiments against a real
long-running container:
1. Let the run complete normally; confirm clean teardown.
2. Trigger CodeAgent's own cancellation path; confirm clean teardown.
3. Send SIGINT to the parent; confirm the handler tears the container
   down.
4. Send SIGTERM to the parent; confirm the same.
5. Send SIGKILL to the parent; confirm — honestly — that cleanup does
   **not** happen (no handler can run), and a labeled container/
   worktree is left behind.
6. Immediately after scenario 5, simulate an application restart: run
   startup reconciliation and confirm it finds and removes exactly the
   labeled orphan from scenario 5, touching nothing else on the
   machine.

**Pass/fail criteria**: Scenarios 1-4 pass if the container and
worktree are fully torn down with nothing CodeAgent-labeled remaining.
Scenario 5 is *expected* to leave a labeled orphan behind (that's the
correct, honest outcome for an uncatchable signal, not a spike
failure) — it passes if that orphan is clearly labeled/identifiable.
Scenario 6 passes if reconciliation removes exactly that orphan and
touches nothing lacking CodeAgent's label. Fail if reconciliation
misses the real orphan, or — a much worse failure — if it
touches/removes anything not bearing CodeAgent's own label.

**Time box**: ~3-4 hours (six scenarios).

**Retained evidence**: the scripts covering all six scenarios; the
exact reconciliation logic that worked; an explicit written note that
SIGKILL fundamentally cannot be handled in-process, so this is
documented as an architectural fact, not silently assumed away.

**Design decision enabled**: confirms in-process signal handling covers
scenarios 1-4, and that labeled-resource reconciliation at startup is a
*required* architectural component for v1, not an optional nicety —
because scenario 5 is a real, expected failure mode of any process that
can be SIGKILLed, which is every real process.

**Code disposition**: throwaway driver scripts; the reconciliation
mechanism is directly reused in `execution/docker.py`.

---

## S6 — Flagship fixture environment (NOT a spike)

No real uncertainty here. A minimal pinned Docker image containing
Python, pytest, and the fixture's known, fixed dependencies is
sufficient to build and demonstrate the first vertical slice. Simple,
known build work — not an experiment.

**Dependency**: standalone to build — does not need S1 to start.
Depends on S1 only for *integration* into a full worktree+Docker run;
the Dockerfile itself can be written independently, at any time.

**Code disposition**: production code from the start. It ships and is
used for every real flagship demo run — not throwaway, unlike
everything else in this plan.

**What it blocks**: v1-useful criterion 1 — the flagship demo cannot
run without this existing.

---

## S7 — General dependency preparation for external repositories

**Question**: Which strategy for preparing an external repository's
dependencies — a task-specific image with a network-enabled build
step, a pre-warmed cache, or a narrow time-boxed network-enabled setup
phase — actually works, for both `requirements.txt` and a
pip-installable PEP 621 `pyproject.toml` project (Poetry-specific
projects remain deferred per the support matrix below — a `pyproject.toml`
present does not by itself mean Poetry), while satisfying the
untrusted-setup-code safety requirements in `PROJECT_BRIEF.md`?

**Hypothesis**: The task-specific image approach is likely simplest to
implement correctly and satisfies the safety requirements most
directly — genuinely uncertain, which is the point of running this.

**Smallest experiment**: Rather than attempting all six
strategy×format combinations up front, evaluate the two leading
strategies — (1) task-specific image with a network-enabled build step,
and (2) a narrow time-boxed network-enabled setup phase — against both
a `requirements.txt` repo and a pip-installable PEP 621
`pyproject.toml` repo (4 combinations; Poetry-specific projects
excluded, remaining deferred per the support matrix).
Evaluate the third strategy (pre-warmed cache) only if both leading
strategies fail or are inconclusive on at least one format within the
time box.

**Pass/fail criteria**: For each combination, record one of three
outcomes — not a binary pass/fail: **PASS** (works correctly, every
safety requirement satisfied for that format), **TECHNICAL FAILURE** (a
specific, understood reason it doesn't work), or **INCONCLUSIVE** (the
time box expired before a definitive result — this is not evidence of
failure and must not be reported as one). The spike overall passes if
at least one strategy achieves a clean PASS across both formats within
the time box. If every attempted strategy is only INCONCLUSIVE, that's
a signal to extend the time box or escalate, not evidence that nothing
works.

**Time box**: ~3-5 hours for the initial 4-combination evaluation; +1-2
hours if the third strategy needs to be brought in.

**Retained evidence**: comparison results for every combination
attempted, tagged PASS/TECHNICAL FAILURE/INCONCLUSIVE — including
non-winning strategies, which is real evidence, not noise — the support
matrix from `PROJECT_BRIEF.md` filled in with actual findings, and a
short ADR recording which strategy was chosen and why.

**Design decision enabled**: the real dependency-preparation
subsystem's design, and the evidence-backed version of the
packaging-format support matrix.

**Code disposition**: throwaway comparison scripts for every strategy
evaluated, even the winner — the chosen approach is rebuilt properly as
production code afterward, not promoted directly from spike quality.

---

## S8 — Real-issue candidate: discovery, then feasibility validation

**Question**: Does a real, small, appropriately-licensed Python
repository exist with a **closed** issue and a **merged** reference fix
— checked out at the parent commit immediately before that fix — that
CodeAgent can plausibly solve within its tool set and budgets, and can
such a candidate be found, independently reproduced, and assessed for
public-benchmark-contamination risk in reasonable time?

Preferring a closed/merged issue (not an open one) matters for two
reasons: it gives a verified reference fix to compare against, and it
confirms the issue really is solvable, since someone already solved it.

**Hypothesis**: At least one suitable candidate exists among small,
permissively-licensed Python utility libraries with a closed issue and
a reasonably-scoped merged fix. Most such candidates will not appear in
well-known public LLM coding benchmarks (SWE-bench and similar), but
this needs to be checked per-candidate, not assumed.

**Smallest experiment** (discovery — Tier 1, no dependency, can start
immediately): search for and manually inspect 3-5 candidate repos/
issues, restricted to closed issues with a merged reference fix.

**Smallest experiment** (feasibility validation — Tier 4, needs S7): for
the most promising candidate: check out the repository at the parent
commit immediately preceding the fix's merge; independently reproduce
the bug by writing or confirming a failing test at that commit — not
just trusting the issue's description; confirm applying the actual
merged fix commit makes that test pass; confirm the fix is plausibly
within scope for the tool set and budgets; check whether the repo/
issue/fix appears in or closely resembles entries in well-known public
coding benchmarks as a contamination risk.

**Pass/fail criteria**: Pass if at least one candidate clears: license
compatibility, closed-issue-with-merged-fix status, independently-
reproduced bug at the parent commit, confirmed-passing reference fix,
plausible scope, and a documented (not necessarily zero-risk, but
assessed and disclosed) contamination check. Fail if no candidate
clears all of these — useful evidence the search needs to broaden or
more lead time is needed.

**Time box**: discovery ~2-3 hours; full feasibility validation
~4-6 hours (separate, since discovery can happen much earlier than
validation).

**Retained evidence**: a written vetting record per candidate
considered, not just the winner — license, closed/merged status,
independent reproduction result, reference-fix confirmation, scope
assessment, contamination-risk notes — plus the final candidate's repo
URL, parent commit, and merged-fix commit reference.

**Design decision enabled**: confirms feasibility of v1-complete
criterion 10 before committing engineering time to the pipeline
integration; informs whether the full v1.1 real-issue suite (3-5 tasks)
is realistic on the stated timeline.

**Code disposition**: not code — manual research/vetting work. No
throwaway/production distinction applies in the usual sense.

---

## First experiment to execute

**The previous version's justification for S1 was wrong** — it claimed
S1 uniquely unblocked the most downstream work. The corrected
dependency graph shows S3, S4, S6, and S8's discovery step are all
equally free to start in Tier 1; S1 doesn't strictly block any of them.

Re-examined honestly, **S1 is still the recommended first experiment**,
but for a different, more modest reason: it de-risks the one
*foundational assumption* — "a disposable worktree bind-mounted into a
Docker container" — that S4, S5, S6, and S7 all build on in practice,
even though none of them strictly requires S1's validation to begin
their own narrower setup. If that base assumption turns out to be
flawed (permission issues, mount performance, platform quirks), finding
that out before three more spikes are built on top of it is worth more
than the small amount of parallelism given up by not starting elsewhere
first.

**A legitimate alternative worth naming, not just S1 by default**:
**S2b** has zero infrastructure dependency (no Docker, no git worktree),
the shortest time box, and produces a retained artifact immediately —
real regression tests, not throwaway code. If the priority is fastest
concrete progress with the least setup, S2b is defensible as the actual
first move, run in parallel with or just before S1.

This document identifies S1 (with S2b as a reasonable alternative or
parallel first step). Neither has been run. Execution starts only when
you say so.
