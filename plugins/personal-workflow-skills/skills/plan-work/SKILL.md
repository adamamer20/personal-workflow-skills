---
name: plan-work
description: Turn substantial or architecturally uncertain work into one decision-ready typed capsule with explicit acceptance and ownership. Not for small localized changes.
---

# Plan Work

Use when work needs decomposition, explicit acceptance, or ownership decisions.
The planner owns intent/canonical plan; the executor owns a capsule.

## Produce one decision-ready plan

Inspect instructions, plan, code/tests, and worktree first. Ask only questions
that may change intent, contracts, safety, ownership, scope, or acceptance;
resolve ordinary details from evidence.

Keep exactly one active plan at the repository path. It must state:

- outcome and non-goals;
- the decomposition into independently closable milestones;
- one owner and protected surfaces for each milestone;
- explicit acceptance modes: `objective`, `visual`, and/or `architecture`;
- acceptance, non-negotiable validation gates, hard-cap conflict policies,
  promotion gates, and residual risks; and
- the next executable milestone as one capsule.

## Freeze the implementation architecture map

Before execution, map each production path as `create`, `modify`, `preserve`, or
`remove`, assign one responsibility/owner and dependency direction, name owned
types and entrypoints, state error/serialization/side-effect boundaries, and
set a bounded new-artifact budget with reasons.

Prefer an existing coherent module. Private helpers may remain inside it; a new
production module, public class, registry, runner, schema, entrypoint,
dependency edge, or durable artifact requires a bounded plan update.

## Factor an independently closable milestone DAG

After freezing the map, record a dependency DAG and current readiness for
independently closable vertical milestones with disjoint mutable surfaces.
Freeze shared schemas, public/persisted contracts, state authority, and
production entrypoints before fan-out. Each serial edge names shared schema,
state authority, entrypoint, migration order, or acceptance dependency.
Minimize the safe critical path; keep milestones single-owner and independently
closable, reject fake boundaries and nested swarm/controller orchestration, and
parallelize only ready milestones with disjoint surfaces. One planning turn may
issue multiple START operations under those conditions; the one-start rule is
per peer and milestone, and serial dependencies remain serial.

Record one program worktree as the sole local integration trunk for the life of
the plan. Before any mutable parallel group becomes ready, require a coherent
verified local trunk commit containing only authorized surfaces and record its
exact SHA as `fan_out_base`. If unrelated dirty baseline bytes are not safely
separable, keep fan-out blocked and plan the separation instead of blessing an
ambiguous base.

For every parallel mutable milestone, record a semantic lane and its physical
sibling worktree path
`<repo-parent>/<repo-name>.worktrees/<program-slug>-<lane-slug>`, branch
`agent/<program-slug>-<lane-slug>`, exact base SHA, owned surfaces, review
commit/range and integration edge. Lane workers never mutate or integrate the
trunk. A mutable milestone requires a coherent owned-surface local commit before
`COMPLETION`; read-only planning/review/evidence and an explicit no-commit
contract are the only exceptions.
Require staged-diff inspection and `git diff --cached --check` before that
commit.

Promotion binds to exact commits. A repair adds a successor commit, and only the
controller/integration owner integrates promoted lane commits after blocking
P0/P1 findings reach zero. Prefer a merge commit for true fan-out; require a
recorded reason for cherry-pick or fast-forward, ancestry/extraneous-commit
verification, conflict work as a new integration change, and proportional
integration gates. Advance readiness only after integration succeeds; derive
successors from the new integrated tip and retain lane worktrees/branches until
commit, review, integration and recovery evidence are durable. Local planned
commits/integration are authorized; push, rebase, history rewrite, discard,
remote mutation and implicit cleanup are not.

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

## Proportional validation

Run the smallest discriminating checks during execution, then each affected
partition and named package/integration/promotion gate at closure. Tests and
bookkeeping support observable proof; shared contracts, collection, or
packaging require the full repository gate. Track severity separately from
`promotion_blocking`; P0 is presumptively blocking and P1 blocks accepted
guarantees or integrity. Deferred findings name an owner and `defer_to`; close
with no open promotion-blocking finding. Skills own cognition only: never
dispatch, callback, route, retry, schedule successors, create worktrees, or
mutate the ledger.

## Typed capsule boundary

Write the next capsule with typed `codex_flow.contracts.ModelFacingCapsule`
(objective, decomposition, acceptance modes/criteria, mutable/protected
surfaces, authorities, bounded prompt, `completion-biased` recovery). JSON is
controller projection only; reference the architecture map and artifact budget
without a sidecar. The controller owns identity, workspace, persistence, and
durable status.

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

Before ending, verify one owner per mutable surface, no mutable/protected
overlap, explicit modes/authorities, bounded capsule, observable gates,
architecture map, artifact budget, semantic names/exceptions, and a successor.
Keep historical evidence beside the plan.
