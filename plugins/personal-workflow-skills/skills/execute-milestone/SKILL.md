---
name: execute-milestone
description: Implement exactly one decision-ready typed capsule, verify its observable gates, and return one durable typed result. Not for planning or broad refactors.
---

# Execute Milestone

Own exactly one decision-ready capsule through verification. It governs intent,
acceptance, mutable/protected surfaces, and scope. Do not invent another
milestone, change public contracts, or turn implementation into planning.

## Work inside the capsule

Read instructions, plan, capsule, relevant code/tests, and worktree status.
State scope, non-goals, protected surfaces, acceptance modes, and promotion
gates. Preserve unrelated changes and singular ownership of shared contracts
and entrypoints.

Implement the smallest coherent change within mutable surfaces and prove
protected surfaces unchanged. Repair ordinary failures and self-review the
diff. Acceptance is observable: rendered evidence for `visual`, boundary/safety
evidence for `architecture`, deterministic evidence for `objective`; mixed
capsules pass every mode.

Use stable capability/domain names for every durable path or identifier; reject
temporary milestone/task labels. Preserve numbered historical/protocol names
only for documented compatibility/provenance, and migrate all owned references
together.

## Preserve semantic density

Implement the planned semantic delta. New vocabulary must carry an invariant,
domain distinction, policy, lifecycle/identity, boundary validation, genuine
substitution, or reusable algorithm; explicitness, forwarding, tests, or style
alone do not justify it. Use functions/direct composition by default. A class
needs state, identity, lifecycle, or policy; a one-implementation protocol needs
a real independently owned and replaceable boundary.

Keep strict edges and boring interiors: validate once at the earliest honest
boundary into one trusted representation. Keep behavior beside its invariant;
reject pass-through layers and synonym lifecycle nouns. Tests describe
observable guarantees and transitions, not helper decomposition. Self-review
the actual semantic delta; any unplanned module, public type, protocol,
registry, runner, schema, entrypoint, compatibility path, or layer returns to
planning.

Do not equate simplicity with fewer abstractions. Implement planned semantic
compression when a pure policy/reducer, declarative spec, relationship type, or
typed codec makes repeated decisions, state handling, or validation disappear
from call sites. Do not unify incidental mechanical repetition.

Honor the capsule's distinct code authority for `objective`, render-aware
visual authority for `visual`, and boundary/security authority for
`architecture`; do not substitute one authority for another.

## Outcome and evidence priority

Outcome and evidence are ordered and non-negotiable:

1. Accepted observable outcome and user intent.
2. Required safety, integrity, lineage, isolation, recovery, and production/independent-proof guarantees.
3. Explicitly designated hard external constraints (legal/protocol/compatibility/deployment ceilings), with a recorded conflict policy.
4. Secondary proxy and optimization metrics (lines, files, duration, tokens, coverage, complexity, scores, inventories).

A lower-priority item never authorizes weakening a higher-priority item.
Numeric targets are secondary unless the accepted plan marks them hard external constraints. Even a hard cap never silently authorizes removing a required check: use proof-preserving replacement or return a bounded replan or genuine decision when it conflicts. Never report success merely because a proxy is exact.

## Proportional validation and findings

Use discriminating checks during implementation. At closure run every affected
semantic partition and named packaging/integration/promotion gate. Shared
contracts, collection, or packaging require the full repository gate. Tests and
metadata support, but do not replace, observable outcome evidence.

Separate severity from promotion impact. P0 is presumptively blocking; P1
blocks accepted guarantees, contracts, boundaries, production reachability, or
integrity. Deferrals name owner and `defer_to`; close with none blocking.

For a mutable milestone, `COMPLETION` also requires at least one coherent local
commit on the assigned lane branch. Stage only owned surfaces, inspect the exact
staged diff, run `git diff --cached --check`, verify the named outcome gates,
and confirm no unrelated artifact entered the commit. Read-only work and an
explicit no-commit capsule are the only exceptions. Report the exact commit tip
or range; do not merge into the program integration trunk, amend a reviewed
commit, push, rebase, rewrite history, discard changes or mutate a remote.

Skills own cognition only. They do not own dispatch, callbacks, routing,
retries, successor scheduling, worktree creation, or ledger mutation.

## Controller boundary

Use `$workflow-control`, backed by packaged `codex-flow`, as the sole runtime
control surface. Do not add orchestration or ad-hoc state; durable controller
status supplements rather than replaces repository outcome.

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
