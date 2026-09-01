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

Use stable descriptive semantic names for every new durable path and
code/contract identifier, including files, modules, classes, functions,
methods, variables, constants, tests, fixtures, CLI commands, public exports,
evidence records, and generated artifacts. Reject temporary milestone/task
labels in new names. Preserve an explicitly documented historical or protocol
name only for compatibility/provenance, and update symbols, imports, packaging,
commands, links, fixtures, evidence references, and tests together when the
capsule owns a safe rename.

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

Do not delete or weaken a production, persisted-artifact, lineage, isolation,
recovery, or independent-proof check to meet a proxy. Removing a check requires
proof-preserving replacement; if a designated hard cap still conflicts, stop
the approach and return a bounded replan or genuine decision.

## Proportional validation and findings

Run the smallest discriminating checks for each changed surface during
implementation. At closure, run every affected semantic partition and each
explicit packaging, integration, or promotion gate named by the capsule. A
shared contract, test-collection, or packaging change requires the full
repository gate. Tests, manifests, counts, and status metadata are supporting
evidence rather than a substitute for the observable outcome.

Treat severity and promotion impact separately. A P0 is presumptively blocking;
a P1 blocks when it violates an accepted guarantee, public or persisted
contract, security boundary, architecture ownership, production reachability,
or data integrity. Non-blocking findings carry an owner and `defer_to` target.
Close only when no open finding is promotion-blocking.

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
