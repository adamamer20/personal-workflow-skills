---
name: plan-work
description: Turn substantial or architecturally uncertain work into one decision-ready typed capsule with explicit acceptance and ownership. Not for small localized changes.
---

# Plan Work

Use for decomposition, acceptance, or ownership decisions. Planner owns the
plan; executor owns one capsule.

## Produce one decision-ready plan

Inspect instructions, plan, code/tests, and worktree. Ask only about unresolved
intent, contracts, safety, ownership, scope, or acceptance.

Use existing decisions for routine choices. Detail the next executable milestone;
outline later readiness and dependencies. Cutover prerequisites block cutover,
not independently verifiable implementation.

Keep exactly one active plan at the repository path. It must state:

- outcome and non-goals;
- the decomposition into independently closable milestones;
- one owner and protected surfaces for each milestone;
- explicit acceptance modes: `objective`, `visual`, and/or `architecture`;
- acceptance, non-negotiable validation gates, hard-cap conflict policies,
  promotion gates, and residual risks; and
- the next executable milestone as one capsule.

## Freeze the implementation architecture map

Map each production path as `create`, `modify`, `preserve`, or `remove`; assign
one responsibility/owner and dependency direction; name owned types,
entrypoints, boundaries, and a reasoned new-artifact budget.

Prefer an existing coherent module. Private helpers may remain inside it; a new
production module, public class, registry, runner, schema, entrypoint,
dependency edge, or durable artifact requires a bounded plan update.

## Budget semantic vocabulary

Record a semantic delta: new domain concepts, boundaries and new compatibility paths;
repeated semantic patterns compressed; concepts collapsed/deleted. New vocabulary
must carry an invariant, policy, identity, boundary validation, substitution or
algorithm; forwarding, tests or style alone do not justify it.

Use functions and direct composition by default. A class needs state, identity,
lifecycle, or policy; a one-implementation protocol needs a real independently
owned and replaceable boundary. Validate once at each untrusted edge into one
trusted representation. Keep behavior beside its invariant, split by reason to
change, and reject pass-through layers or synonym config/state/result families.
Plan a pure reducer/policy, declarative spec, relationship type, or typed codec
when it makes repeated decisions, relationships, transitions, or validation
disappear from call sites; do not abstract incidental mechanical repetition.

## Factor an independently closable milestone DAG

Record a dependency DAG and current readiness for independently closable
vertical milestones with disjoint mutable surfaces. Freeze shared schemas,
public/persisted contracts, state authority, and production entrypoints before
fan-out. Each serial edge names its schema, authority, entrypoint, migration
order, or acceptance dependency. Minimize the critical path; require single-owner
milestones, reject fake boundaries and nested swarm/controller orchestration,
and parallelize only ready milestones with disjoint work. Multiple STARTs are allowed only for
such peers; serial dependencies remain serial.

Keep one program worktree as the sole local integration trunk. Before mutable
fan-out, require a verified authorized trunk commit and record its SHA as
`fan_out_base`; unrelated dirty baseline bytes block fan-out until separated.

For each parallel mutable milestone, record its physical sibling worktree path
`<repo-parent>/<repo-name>.worktrees/<program-slug>-<lane-slug>`, branch
`agent/<program-slug>-<lane-slug>`, exact base SHA, owned surfaces, review
commit/range and integration edge. Lane workers never mutate or integrate the
trunk. Mutable `COMPLETION` requires a coherent owned-surface local commit after
staged-diff inspection and `git diff --cached --check`; only read-only work or
an explicit no-commit contract is exempt.

Promotion binds to exact commits; repairs add a successor commit. Only the
integration owner integrates lanes after blocking P0/P1=0. Prefer a merge commit
for fan-out; otherwise record why, perform ancestry/extraneous-commit
verification, treat conflicts as new changes, and run integration gates. Advance
from the new integrated tip and retain lanes until evidence is durable. Planned
local commits/integration are authorized; push, rebase, history rewrite,
discard, remote mutation, and implicit cleanup are not.

## Outcome and evidence priority

Outcome and evidence are ordered and non-negotiable:

1. Accepted observable outcome and user intent.
2. Required safety, integrity, lineage, isolation, recovery, and production/independent-proof guarantees.
3. Explicitly designated hard external constraints (legal/protocol/compatibility/deployment ceilings), with a recorded conflict policy.
4. Secondary proxy and optimization metrics (lines, files, duration, tokens, coverage, complexity, scores, inventories).

A lower-priority item never authorizes weakening a higher-priority item.
Numeric targets are secondary unless the accepted plan marks them hard external constraints. Even a hard cap never silently authorizes removing a required check: use proof-preserving replacement or return a bounded replan or genuine decision when it conflicts. Never report success merely because a proxy is exact.

Name every new durable path and code/contract identifier from its capability,
domain, responsibility, or observable behavior. Never use temporary
milestone/task numbers in filenames, modules, classes, functions, methods,
variables, constants, tests, fixtures, CLI commands, exports, evidence paths,
or generated artifacts. A numbered published path/protocol needs a recorded
compatibility or provenance exception and safe reference-preserving migration.

Acceptance authorities are distinct by mode. `objective` uses a code authority,
`visual` uses a render-aware visual authority, and `architecture` uses a
boundary/security authority. A mixed milestone must satisfy every declared
mode; passing one authority never implies another.

Substantial code milestones need independent objective review plus self-review.
Add architecture acceptance for ownership, public/persisted contracts, trust
boundaries or a named integration risk, not merely a prior architecture phase.

## Proportional validation

Use discriminating checks during execution and every affected partition and
named closure gate. Tests/bookkeeping support observable proof; shared
contracts, collection, or packaging require the full repository gate. Track
severity separately from `promotion_blocking`; deferred findings name owner and
`defer_to`, and closure has none open. Skills never dispatch, callback, route,
retry, schedule successors, create worktrees, or mutate the ledger.

## Typed capsule boundary

Write the next typed `codex_flow.contracts.ModelFacingCapsule` with objective,
decomposition, acceptance, surfaces, authorities, bounded prompt, and
`completion-biased` recovery. JSON is controller projection only; reference the
architecture map/budgets without a sidecar. The controller owns runtime facts.

## Handoff to execution

For implementation, hand the first executable capsule through the canonical
plan path and exact milestone id. The executor must not copy sidecar JSON or
author routing, identity, permission, delivery, or successor facts; planning
does not implement the milestone. The executor returns one typed result and the
planner decides readiness of the next milestone.

If a requirement is genuinely underdetermined or needs new authority, record it
as a decision request in the plan. Do not broaden scope, weaken an acceptance
gate, or turn an implementation difficulty into a user question.

## Close

Before ending, check ownership, surfaces, acceptance, architecture map,
semantic-vocabulary budgets and next readiness. Keep historical evidence
beside the plan.
