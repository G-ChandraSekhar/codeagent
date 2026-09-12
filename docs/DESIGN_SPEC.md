# CodeAgent — Product Spec & Architecture Design

Status: PRE-BUILD (design review phase). Nothing described here has been
implemented yet. This document is meant to be readable standalone — it
does not assume you've seen any prior conversation about it.

---

## 1. Core idea, in one paragraph

CodeAgent is an autonomous coding agent: give it a task description and a
path to a Python repository, and it explores the codebase, proposes a
plan, makes the actual file edits, runs the test suite inside an isolated
Docker sandbox, and iterates on failures — up to a bounded number of
attempts — until the task is done or it gives up and reports why. The
differentiator isn't raw capability (it isn't trying to compete with
OpenHands or Claude Code on benchmark score); it's that every part of the
loop is small enough to fully own, explain, and defend in an interview:
a real safety/policy layer gating command execution, a plan-before-act
step the operator can inspect, and a self-built benchmark with real
ground truth instead of a vague capability claim.

## 2. Why this project, and why now

- Autonomous coding agents are a genuine, currently-growing product
  category (OpenHands raised an $18.8M Series A in June 2026 for exactly
  this), not a novelty demo.
- It's a level above the author's existing RAG-style AI projects
  (BrookMinds, Chat2SQL): those are applications *that use* an LLM; this
  is tooling that *directs an LLM to modify software*, a materially
  different and harder problem — planning under uncertainty, verifying
  its own work, recovering from failure.
- It maps directly onto real job requisitions the author is currently
  applying to (roles asking for "agentic AI harness and quality," "AI-
  native software engineer," Claude-Code/Cursor fluency).

## 3. Explicit non-goals (v1)

Naming what this is *not* trying to be is as important as the spec
itself — scope creep is the most likely way this project turns into
something unfinished and unexplainable.

- **Not multi-language.** Python repositories only. No JS/TS/Go/etc.
  support in v1.
- **Not an IDE plugin or chat UI.** CLI only.
- **Not a general dev assistant.** It solves one shape of task: "make
  this repo's tests pass" / "implement this described change and make
  sure tests cover it." It is not meant to answer open-ended questions
  about a codebase.
- **Not fully unsupervised in the default flow.** The plan-before-execute
  step exists specifically so a human can see what the agent intends to
  do before it touches files, by default. A "yolo mode" that skips this
  may exist as an explicit flag, but it is not the default.
- **Not optimizing for SWE-bench-style leaderboard performance.** The
  self-built benchmark (Section 8) exists to produce an honest,
  explainable number — not to chase a competitive score against
  billion-dollar-funded projects.

## 4. Users and primary use case

Primary "user" is the project's own author, using it against:
1. Their own repos (the LLM gateway, prior distributed-systems projects)
   with a deliberately seeded bug, as a demo.
2. A small set of purpose-built benchmark repos (Section 8) with known,
   verified-fixable issues.

Secondary, aspirational use case (not required for v1, worth designing
for so it's not precluded): pointed at an arbitrary small-to-medium
Python repo with a real GitHub issue.

## 5. Design principles (and which real project each borrows from)

Every principle below is a deliberate choice, not a default — each has a
one-line rationale so it can be argued with, not just accepted.

| Principle | Borrowed from | Why |
|---|---|---|
| A small, fixed set of well-designed tools, not a kitchen sink | SWE-agent's "Agent-Computer Interface" concept | Fewer tools means a smaller, more predictable action space, easier to reason about and to secure |
| Plan before execute, plan is inspectable | Open SWE | Converts "black box autonomy" into "reviewable autonomy" — a real architectural distinction, not cosmetic |
| Every real edit is a diff against git, trivially revertable | Aider | Cheap safety net; also gives a natural, demoable transcript of what changed |
| All command execution passes through an explicit policy/allowlist layer | The author's own prior work (a take-home project building a command-policy/redaction engine) | Direct, defensible connection to real prior experience — not a borrowed idea, an extended one |
| Execution happens in an isolated, network-disabled, resource-capped container | OpenHands' sandboxed CodeAct pattern | The agent runs LLM-generated shell commands; assume they could be wrong or hostile, not just imperfect |
| Success is measured against a self-built benchmark with known ground truth | (Reaction against vague "it works well" claims) | A specific, defensible number beats an impressive-sounding but unverifiable claim |

## 6. Layered architecture

Eight layers, ordered from "what the operator sees" down to "what
actually executes." A request flows top-to-bottom on the way in, and
results flow bottom-to-top on the way out.

```
┌─────────────────────────────────────────────────────────┐
│ 1. CLI / Output layer                                   │
│    Accepts a task description + repo path. Renders the  │
│    final report: diff, pass/fail, full transcript.       │
├─────────────────────────────────────────────────────────┤
│ 2. Orchestration loop (the agent loop itself)            │
│    State machine: EXPLORE → PLAN → (approval gate) →     │
│    EXECUTE → VERIFY → iterate-or-stop. Owns the loop's    │
│    budget (max iterations, max tool calls, max tokens).  │
├─────────────────────────────────────────────────────────┤
│ 3. LLM interface layer                                   │
│    Wraps the OpenAI API: builds the tool/function         │
│    schemas, sends the running conversation, parses tool   │
│    call requests out of the model's response.             │
├─────────────────────────────────────────────────────────┤
│ 4. Tool layer                                             │
│    The fixed, small set of callable tools (Section 7).    │
│    Each tool call here is a data-in/data-out function;    │
│    this layer has NO knowledge of Docker or the LLM.      │
├─────────────────────────────────────────────────────────┤
│ 5. Policy / safety layer                                  │
│    Every run_command call is checked against an allow-    │
│    list before it reaches the sandbox. Logs every attempt │
│    (allowed or blocked) for the final transcript.          │
├─────────────────────────────────────────────────────────┤
│ 6. Execution sandbox layer                                │
│    Owns Docker container lifecycle: spin up a fresh        │
│    container per task, mount the repo, disable network,    │
│    enforce a timeout and resource limits, tear down.       │
├─────────────────────────────────────────────────────────┤
│ 7. Verification layer                                     │
│    Runs the repo's test command inside the sandbox,        │
│    parses pass/fail + failure output back into a form      │
│    the orchestration loop can feed back to the LLM.        │
├─────────────────────────────────────────────────────────┤
│ 8. Benchmark harness (offline, not part of a live run)     │
│    A separate tool: seeds N known-broken mini-repos,        │
│    runs the full agent against each, records pass/fail,     │
│    produces the "here is our real measured success rate"    │
│    report the README leads with.                            │
└─────────────────────────────────────────────────────────┘
```

Layers 4-7 are deliberately kept separate from each other so each can be
unit-tested in isolation — e.g. the policy layer can be tested with fake
commands and no real Docker, the sandbox layer can be tested with a
trivial "echo hello" task and no real LLM involved.

## 7. The tool set (Layer 4)

Kept deliberately small, per the "Agent-Computer Interface" principle.

| Tool | Purpose | Notes |
|---|---|---|
| `list_dir(path)` | See what's in a directory | Read-only |
| `read_file(path, [start_line, end_line])` | Read a file, optionally a range | Read-only; range support avoids blowing the context window on large files |
| `grep(pattern, [path])` | Search for a pattern across the repo | Read-only; this is how the agent finds relevant code without being handed the whole repo upfront |
| `edit_file(path, old_text, new_text)` | Targeted find/replace, not a full rewrite | Mirrors Claude Code's own edit tool design — smaller diffs, fewer accidental regressions than "rewrite the whole file" |
| `run_command(command)` | Execute a shell command in the sandbox | The highest-risk tool; every call passes through the policy layer (Layer 5) first |
| `propose_plan(steps)` | Not a sandbox action — emits the plan for the approval gate | Doesn't touch the repo; exists purely to make the loop's intent inspectable before Layer 6 does anything |

Explicitly NOT a tool in v1: `write_file` (full-file overwrite). Forcing
all edits through `edit_file`'s find/replace shape is a deliberate
constraint, not an oversight — open question in Section 9 on whether
this is too restrictive for genuinely new files.

## 8. The orchestration loop (Layer 2) — state machine

```
INIT
  │  (load task description, repo path, resolve git HEAD)
  ▼
EXPLORE  ◄───────────────────────────────┐
  │  (agent calls list_dir/read_file/grep│
  │   some bounded number of times)      │
  ▼                                      │
PLAN                                     │
  │  (agent calls propose_plan)          │
  ▼                                      │
[APPROVAL GATE]  — default: pause, show  │
  │  plan, wait for operator confirm.    │
  │  --yolo flag: skip, log a warning.   │
  ▼                                      │
EXECUTE                                  │
  │  (agent calls edit_file / run_command│
  │   to carry out the plan)             │
  ▼                                      │
VERIFY                                   │
  │  (run the test command in the        │
  │   sandbox; parse pass/fail)          │
  ├─── PASS ──► DONE (success report)    │
  └─── FAIL ──► iteration_count++ ───────┘
                   │
                   ├─ under max_iterations: loop back to EXPLORE
                   │  with failure output added to context
                   └─ at max_iterations: DONE (failure report,
                      includes every attempt's diff + test output)
```

Budget caps the loop must enforce (exact numbers are an open question,
Section 9, but the caps themselves are not optional):
- Max iterations (full EXPLORE→VERIFY cycles)
- Max tool calls per iteration (prevents an agent that just keeps
  `read_file`-ing forever without ever proposing a plan)
- Wall-clock timeout for the whole run
- Wall-clock timeout per `run_command` call inside the sandbox

## 9. Open questions — genuinely undecided, worth another model's opinion on

These are real trade-offs, not padding. Feedback on any of these is
exactly what's wanted before implementation starts.

1. **Should the agent be allowed to edit test files?** This is a known
   failure mode in this space ("reward hacking" — the agent makes the
   test pass by weakening or deleting the assertion instead of fixing
   the actual bug). Options: (a) hard-block edits to any file matching
   `test_*`/`*_test.py`, (b) allow it but flag/diff-highlight test-file
   changes specially in the report for human review, (c) allow it freely
   and treat this as a benchmark-measurable failure mode to report
   honestly. Current lean: (a) for v1 simplicity, (b) as a natural v2.

2. **What are the actual numbers for the budget caps in Section 8?**
   Too tight and legitimate tasks fail from starvation; too loose and a
   demo run could rack up real OpenAI API cost or hang. No number has
   been chosen yet.

3. **Should `write_file` (full-file create/overwrite) exist for
   genuinely new files**, since `edit_file`'s find/replace has nothing
   to anchor against on an empty file? Current lean: yes, but only for
   files that don't yet exist (never to overwrite an existing file) —
   worth a second opinion on whether that's the right boundary.

4. **How should multi-file changes be applied — atomically (all edits
   in an iteration succeed or all roll back) or incrementally (each
   `edit_file` call lands immediately)?** Atomic is safer but harder to
   implement correctly against a live git working tree; incremental is
   simpler but can leave a repo in a broken intermediate state if the
   loop is interrupted.

5. **Benchmark repo design: synthetic seeded bugs, or real closed
   GitHub issues from small OSS repos?** Synthetic is fully controlled
   and the ground truth is unambiguous; real issues are more externally
   credible but harder to guarantee are actually solvable within the
   tool set and budget caps above.

6. **Plan approval UX**: is a synchronous CLI prompt (the run literally
   blocks on stdin) the right mechanism, or should the plan be written
   to a file the operator edits/confirms out-of-band? Affects whether
   this can ever run non-interactively (e.g. in CI) at all.

7. **What exactly counts as "success" beyond "tests pass"?** E.g., should
   the loop also sanity-check diff size (a 500-line diff to fix a 1-line
   bug is a smell even if tests pass), or check that no *other*
   previously-passing test broke? Current lean: track both as reported
   metrics even if they don't gate pass/fail in v1.

## 10. Key data structures (draft)

```python
# A single task handed to the agent
Task = {
    "description": str,          # natural-language task
    "repo_path": str,            # absolute path to the repo on disk
    "test_command": str,         # e.g. "pytest -q"
}

# One tool call the LLM requested
ToolCall = {
    "tool": str,                 # one of the Section 7 tool names
    "arguments": dict,
    "iteration": int,
}

# Result of executing one tool call
ToolResult = {
    "tool": str,
    "success": bool,
    "output": str,               # stdout/file contents/grep matches/etc.
    "error": str | None,
}

# One full EXPLORE→VERIFY cycle
IterationRecord = {
    "iteration": int,
    "tool_calls": list[ToolCall],
    "tool_results": list[ToolResult],
    "plan": list[str] | None,
    "diff": str | None,          # git diff produced this iteration
    "test_output": str | None,
    "test_passed": bool | None,
}

# What the CLI ultimately renders
FinalReport = {
    "task": Task,
    "outcome": str,               # "success" | "failed" | "budget_exceeded"
    "iterations": list[IterationRecord],
    "final_diff": str,            # cumulative diff across all iterations
    "total_tool_calls": int,
    "total_wall_clock_seconds": float,
}
```

## 11. Success criteria for v1

- Runs end-to-end against at least one real, deliberately-broken
  repository and produces a correct fix, verified by the sandboxed test
  run — not just "looks plausible," actually verified.
- The self-built benchmark (Section 8's Layer 8) runs against at least
  5 seeded mini-repos and reports a real, reproducible pass rate.
- Every command the agent executes is visible in the final transcript,
  alongside whether the policy layer allowed or blocked it.
- The plan-approval gate genuinely blocks execution until confirmed, in
  a live demo run (not just unit-tested).
- README documents at least one real bug or design mistake found and
  fixed during development — matching the standard set by the LLM
  gateway project's own `tasks/todo.md`.
