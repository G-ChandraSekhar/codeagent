# CodeAgent — Build Plan

Companion to DESIGN_SPEC.md — read that first for the "why," this is the
"in what order." Locked decisions this plan assumes (see DESIGN_SPEC.md
Section 9 for what's still open):

- LLM backend: OpenAI API, function/tool-calling
- Execution sandbox: Docker
- Scope: Python repositories only

Each phase below has a concrete deliverable and a concrete way to verify
it actually works — not "should work," demonstrably works, live, before
moving to the next phase. This mirrors how the LLM gateway project was
built: nothing was marked done without a real run proving it.

---

## Phase 1 — Agent loop skeleton, no real tools yet

**Build:**
- The orchestration loop's state machine (DESIGN_SPEC.md Section 8) as
  real code, with EXPLORE/PLAN/EXECUTE/VERIFY as actual states.
- OpenAI tool-calling wired up with the Section 7 tool *schemas* defined,
  but every tool's implementation is a stub that returns canned data.
- Budget caps (max iterations, max tool calls, timeouts) enforced as
  real limits, even against stub tools.

**Verify:**
- Run the loop against a trivial fake task and confirm it correctly
  calls stub tools, hits PLAN, and stops at the approval gate.
- Deliberately misconfigure a budget cap low and confirm the loop
  actually stops instead of running forever — prove the safety net
  works before there's anything real for it to protect against.

## Phase 2 — Real tools: file read/write/grep against a real repo

**Build:**
- `list_dir`, `read_file`, `grep`, `edit_file` implemented for real,
  operating directly on the filesystem (no Docker yet — sandboxing is
  Phase 3, kept separate on purpose so file-tool bugs aren't confused
  with sandbox bugs).
- `edit_file`'s find/replace semantics, including the failure case where
  `old_text` doesn't match anything or matches more than once.

**Verify:**
- Point the tools at a real small repo (could be an old portfolio repo)
  and manually drive a few tool calls — confirm `grep` finds real
  matches, `edit_file` produces a correct, reviewable diff, and the
  ambiguous-match failure case actually raises instead of silently
  picking one.

## Phase 3 — Docker sandbox + policy/safety layer

**Build:**
- Container lifecycle: spin up per task, mount the repo, tear down
  after. Network disabled by default. Resource + timeout limits set and
  actually enforced (not just configured — verified they trigger).
- `run_command` wired through the policy/allowlist layer before it ever
  reaches the container.
- Every allowed AND every blocked command call logged for the eventual
  transcript.

**Verify:**
- Run a command that should be blocked and confirm it never reaches the
  container at all (check via the container's own logs that nothing
  executed).
- Run a command that should time out and confirm the timeout actually
  fires and the container is cleaned up afterward — check for orphaned
  containers left running.
- Confirm network really is disabled (attempt a real network call inside
  the sandbox and watch it fail).

## Phase 4 — Test verification + the iterate-on-failure loop

**Build:**
- The VERIFY state actually runs the repo's real test command inside the
  sandbox and parses real pass/fail + failure output.
- Failure output gets fed back into the LLM's context for the next
  EXPLORE iteration — this is the actual "learns from its mistake" loop.

**Verify:**
- Against a repo with one deliberately broken, known-fixable test: run
  the full agent end-to-end and confirm it (a) finds the failure, (b)
  proposes a plan, (c) after approval, edits the right file, (d) the
  sandboxed test run genuinely goes from failing to passing.
- This is the first point where a truly end-to-end demo exists — treat
  it as the milestone worth recording a transcript of.

## Phase 5 — CLI polish, transcript/diff output, plan-approval UX

**Build:**
- Real CLI entry point (task description + repo path as arguments).
- Renders the FinalReport (DESIGN_SPEC.md Section 10) as readable
  output: the plan, the diff, pass/fail, full tool-call transcript.
- Implements whichever plan-approval UX gets decided in DESIGN_SPEC.md
  Section 9's open question 6.

**Verify:**
- A cold-start run by someone who hasn't seen the code: can they follow
  what happened purely from the CLI output and the final report, with
  no need to read source to understand what the agent did?

## Phase 6 — Benchmark harness + seeded mini-repos

**Build:**
- 5-10 small, purpose-built repos, each with one deliberately introduced,
  verified-fixable bug (design choice pending: DESIGN_SPEC.md Section 9,
  open question 5).
- A harness script that runs the full agent against every seeded repo
  and produces a real pass-rate report.

**Verify:**
- Run the harness for real, twice, and confirm the pass rate is
  reproducible (not flaky in a way that undermines the number).
- This report's output is what the README leads with — it needs to be
  something the author can defend being asked about live in an
  interview, not just a number that sounds good.

## Phase 7 — README, transcript writeup, final polish

**Build:**
- README following the standard set by the LLM gateway's `tasks/todo.md`:
  real design decisions with rationale, at least one real bug found
  during development, honest documentation of what's NOT handled
  (matching DESIGN_SPEC.md Section 3's non-goals).
- Include one full, real transcript of the agent solving a task,
  end-to-end, as a concrete example rather than only abstract
  description.

**Verify:**
- Have the README reviewed cold by someone (or another LLM) who hasn't
  seen the build process — can they explain back what the project does
  and why each major design choice was made, using only the README?

---

## What's deliberately NOT in this plan

Consistent with DESIGN_SPEC.md Section 3's non-goals: no multi-language
support, no IDE integration, no fully-unsupervised default mode, no
attempt to chase a SWE-bench-style leaderboard number. Anything from
Section 9's open questions that gets resolved as "build it" during
review should get its own phase inserted here — this plan assumes those
are still open, not decided.
