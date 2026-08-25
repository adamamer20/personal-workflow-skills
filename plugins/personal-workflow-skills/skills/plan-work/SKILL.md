---
name: plan-work
description: Turn substantial or architecturally uncertain work into one decision-ready typed capsule with explicit acceptance and ownership. Not for small localized changes.
---

# Plan Work

Use this skill when the request needs decomposition, an explicit acceptance
contract, or a meaningful ownership decision. The planner owns intent and the
canonical plan; the executor owns implementation inside one capsule.

## Produce one decision-ready plan

Inspect the repository instructions, current plan, relevant code and tests, and
the worktree before deciding. Ask only questions that could change user intent,
public or persisted contracts, safety, ownership, scope, or acceptance. Resolve
ordinary implementation details from repository evidence.

Keep exactly one active plan at the repository-defined path. It must state:

- the outcome and non-goals;
- the decomposition into independently closable milestones;
- one mutable owner and the protected surfaces for each milestone;
- explicit acceptance modes: `objective`, `visual`, and/or `architecture`;
- acceptance criteria, validation, promotion gates, and residual risks; and
- the next executable milestone as one capsule.

Acceptance authorities are distinct by mode. `objective` uses a code authority,
`visual` uses a render-aware visual authority, and `architecture` uses a
boundary/security authority. A mixed milestone must satisfy every declared
mode; passing one authority never implies another.

## Typed capsule boundary

Write the next capsule with the typed Python model
`codex_flow.contracts.ModelFacingCapsule`. It contains the objective,
decomposition, acceptance modes and criteria, mutable and protected surfaces,
distinct authority roles, a bounded execution prompt, and the explicit
completion-biased recovery policy. JSON is only the controller's serialization
projection. Do not author a second free-form capsule shape.

The capsule is sufficient for an executor to act without inventing intent,
architecture, ownership, or acceptance. Keep prompts concise and within the
repository's measured prompt budget. The controller selects runtime identity,
workspace details, persistence, and durable status from the capsule and
repository configuration.

## Handoff to execution

When implementation is requested, hand the first executable capsule to the
implementation owner. Do not implement the milestone while planning. The
executor returns one typed result and the planner decides whether the next
milestone is now executable.

If a requirement is genuinely underdetermined or needs new authority, record it
as a decision request in the plan. Do not broaden scope, weaken an acceptance
gate, or turn an implementation difficulty into a user question.

## Closure check

Before ending, verify that the active plan has one owner per mutable surface,
no mutable/protected overlap, explicit acceptance modes and authorities, a
bounded capsule, observable gates, and a concrete successor. Keep historical
evidence beside the plan rather than creating another roadmap.
