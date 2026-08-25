---
name: execute-milestone
description: Implement exactly one decision-ready typed capsule, verify its observable gates, and return one durable typed result. Not for planning or broad refactors.
---

# Execute Milestone

Own exactly one decision-ready capsule from discovery through verification. The
capsule is the authority for intent, decomposition, acceptance modes, mutable
surfaces, protected surfaces, and scope. Do not invent a second milestone,
change the public contract, or turn implementation into planning.

## Work inside the capsule

Read the repository instructions, canonical plan, capsule, relevant code/tests,
and exact worktree status. State the active scope, non-goals, protected
surfaces, acceptance modes, and promotion gates before editing. Preserve
unrelated user changes. Give shared schemas, runners, persisted contracts, and
production entrypoints one owner.

Implement the smallest coherent change. Keep every edit inside the mutable
surfaces and prove protected surfaces remain unchanged. Run focused checks,
repair ordinary failures, and self-review the complete diff. Acceptance is
observable: rendered evidence is required for `visual`, boundary and safety
evidence for `architecture`, and deterministic code/contract evidence for
`objective`. A mixed capsule must pass all of its modes.

Honor the capsule's distinct code authority for `objective`, render-aware
visual authority for `visual`, and boundary/security authority for
`architecture`; do not substitute one authority for another.

## Controller boundary

Use `$workflow-control` for the runtime path. It invokes the packaged
`codex-flow` controller and reports the durable status; it is the only runtime
control surface for this milestone. Do not create another orchestration path or
persist ad-hoc workflow state. The controller result is supplementary to the
actual repository outcome, not a substitute for it.

Recovery is completion-biased within the capsule: finish a bounded repair or
change implementation approach while intent, contract, safety boundary, cost,
destructive behavior, and scope remain unchanged. Ask for a decision only when
accepted intent or required authority is genuinely missing; report an external
block only for a missing prerequisite; report failure only when evidence shows
the accepted outcome is infeasible. Never weaken a gate to make a result look
complete.

## Return one result

Write exactly one `codex_flow.contracts.ModelFacingResult` containing the
terminal status, concise summary, changed surfaces, validation facts, durable
controller status, and (when needed) one next action. JSON/JSONL is the
controller serialization format, not the model-facing authoring language.

The result must identify the observable repository outcome, not merely test
counts or bookkeeping. Include residual risks and the recommended next
milestone in the handoff to the planner. Stop after this capsule is complete;
later pilots, compatibility decisions, legacy retirement, and unrelated audits
belong to later milestones.
