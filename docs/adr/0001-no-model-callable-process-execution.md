# ADR 0001: No model-callable process-execution tool in v1

Status: Accepted

## Context

`DESIGN_SPEC.md` and `CODEAGENT_IMPLEMENTATION_GUIDE.md` §7 both describe a
`run_process(argv, cwd, timeout_seconds, environment)` tool as one of the
model's callable tools ("process tool"), alongside the read tools and the
patch tool. Milestone 0's first domain-types pass (`src/codeagent/domain.py`)
initially included `ToolName.RUN_PROCESS` as legal during `EXPLORE`, for
model-invoked investigative commands (e.g. `grep`, `git status`), distinct
from the controller-initiated `VERIFY`-state check.

On review, that inclusion was removed before any tool dispatcher, process
policy, or Docker executor exists to bound it.

## Decision

`ToolName` has no process-execution member. The model cannot execute
arbitrary commands in any state. Its only callable tools are
`list_directory`, `read_file`, `search_text`, and `propose_plan` during
`EXPLORE`, and `apply_patch` during `EXECUTE`.

Baseline recording and verification command execution are performed by a
controller-owned `ProcessExecutor` (guide's suggested `execution/policy.py`,
`execution/docker.py`), invoked directly by the controller in the `BASELINE`
and `VERIFY` states — never as a tool call the model can request.

## Consequences

- Smaller model attack surface for v1: there is no model-facing process
  allowlist to design for v1; the controller-owned executor still requires
  structured arguments, sandboxing, resource limits, and validation.
  Removing model process execution reduces attack surface — it does NOT
  make the controller-owned verification executor trusted.
- Exploration becomes read-only and structured: the model can inspect
  repository content and propose a plan, but cannot run arbitrary shell
  commands (e.g. `git log`, linters, ad hoc greps beyond `search_text`) to
  gather evidence during `EXPLORE`.
- If a later milestone determines the model needs to run bounded,
  policy-checked commands during exploration (e.g. to reproduce a failure
  before proposing a fix), that requires a new ADR reintroducing a
  process-execution tool together with its policy and sandbox design — it
  should not be added back into `domain.py` alone.
- This is a narrowing, not a widening, of `DESIGN_SPEC.md`'s and the
  implementation guide's originally described tool set. It does not require
  a v1-scope exception under `PROJECT_BRIEF.md`'s "Explicitly out of scope"
  list, since it removes model capability rather than adding it.
