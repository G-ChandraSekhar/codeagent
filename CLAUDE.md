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

Do not expand v1 scope (`docs/CODEAGENT_IMPLEMENTATION_GUIDE.md` §2 —
"Out of scope") without the user's explicit approval, even when a
library or the model makes a broader feature trivially easy to add.

## Reference documents, in order of authority

1. `docs/CODEAGENT_IMPLEMENTATION_GUIDE.md` — authoritative. Where it
   conflicts with an open question in DESIGN_SPEC.md, this guide's
   decision wins (see its own §9, "Resolved open questions").
2. `docs/DESIGN_SPEC.md` — original product spec and architecture
   rationale. Still the source for *why*, even where *what* has since
   been refined by the implementation guide.
3. `docs/BUILD_PLAN.md` — original 7-phase plan. Superseded by the
   Milestone 0-7 sequence in the implementation guide (§12); kept for
   the phase-level rationale, not as the current source of truth for
   sequencing.

## Current status

Nothing has been implemented yet. The project has not started
Milestone 0 ("Contracts and threats" — domain types, state-transition
table, event/error schemas, threat model, ADRs for consequential
decisions). No code, no repository structure beyond this file and
`docs/` exists as of the last update to this file.
