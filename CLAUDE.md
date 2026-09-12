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
- `tests/unit/{test_domain,test_events,test_errors}.py` — 677 tests,
  all passing as of the last full run.

Further ADRs are not inherently required — per the documentation ladder
above, another ADR is created only when a future consequential decision
actually meets the ADR threshold, not as a standing checklist item.

Stage 2 (of the four-stage planning process in
`docs/CODEAGENT_LLM_HANDOFF.md`) spikes remain separately unstarted
except S1:

- **S1 — temporary worktree + Docker + pytest + cleanup: PASSED**
  (Linux; 3 trials, exit 0, consistent per-test results, no orphaned
  containers or worktrees after cleanup). See `spikes/s1/S1_RESULT.md`.
- Remaining four Stage-2 spikes are unstarted:
  1. Responses API strict function tools and multiple tool calls.
  2. Atomic multi-file patch failure behavior.
  3. Network/path/timeout/output/resource isolation.
  4. Interruption without orphaned containers.
  (A sixth spike, JSONL replay into the first frontend view, is also
  listed in the handoff and unstarted.)

The flagship fixture repository does not exist yet and has not been
empirically validated (see `docs/PROJECT_BRIEF.md`).
