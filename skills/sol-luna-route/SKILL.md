---
name: sol-luna-route
description: Run substantial repository implementation, audit, debugging, or review work with the main Sol agent as the decision and integration plane while Luna subagents own bounded exploration, implementation, testing, and routine repair. Use when explicitly invoked with `$sol-luna-route` or when repository instructions require it for non-trivial work. Do not use for questions, planning-only requests, small localized edits, or work whose coordination overhead exceeds the operational context it would isolate.
---

# Sol-Luna Route

Keep requirements, architecture, decisions, acceptance, and final synthesis in
the main thread. Move noisy operational context into isolated Luna threads.
Optimize expensive-main-agent usage without creating redundant worker work.

## Select the route

- Use the direct main-agent path for questions, planning-only requests, and
  small localized operations.
- Use this route for substantial work that benefits from isolating repository
  discovery, implementation iterations, test output, logs, or independent
  review.
- Use the smallest number of workers that gives each mutable surface one clear
  owner. Concurrency follows genuine independence, not available capacity.
- Once Explorer has defined safe package boundaries, start all currently
  independent packages concurrently up to the lower of the configured Codex
  cap and the session's available child slots. Keep dependent work and
  overlapping mutable ownership serial; do not leave usable slots idle merely
  because the default package count is smaller.
- Keep every worker on standard velocity unless the user explicitly requests a
  faster service tier.

## Establish the execution contract

Before spawning implementation workers, state concisely in the main thread:

- outcome and active scope;
- non-goals and protected surfaces;
- acceptance checks and required repository gates;
- ownership of shared files, schemas, registries, runners, and public contracts;
- stop conditions and decisions that must return to the main agent.

Use the repository's existing execution plan or follow-up register when one
exists. Do not create a parallel roadmap or worker-owned status system.

## Keep Sol as the knowledge plane

The main agent owns task direction, architecture, package boundaries, contract
changes, integration order, acceptance decisions, official status, and user
communication.

The main agent should normally use tools only for initial state checks, compact
coordination, decision-critical evidence, integration inspection, and final
verification. Delegate broad repository reads, routine implementation,
diagnostics, test logs, and repair iterations. Inspect underlying worker
evidence only when it is contradictory, incomplete, stale, high risk, or needed
for a material decision.

## Create isolated Luna workers

For each initial operational worker:

- spawn `gpt-5.6-luna` at `high` reasoning and standard velocity;
- use `fork_turns="none"` so the worker does not inherit the main conversation;
- assign one coherent, independently completable package;
- prohibit delegation unless nested work is explicitly part of the package;
- identify its implementation or verification counterpart when applicable.

For every substantive invocation, initialize or reuse one persistent read-only
Explorer named for the active deployment. Spawn it as `gpt-5.6-luna` at `high`
reasoning and standard velocity with `fork_turns="none"`. Give it the outcome,
known constraints, investigation questions, and evidence boundaries. Ask for a
compact planning brief covering the architecture map, relevant files and
interfaces, constraints, risks, recommendation, unresolved decisions, and
exact references. Do not allocate execution packages before receiving this
brief. Form the package boundaries and integration order from it; do not repeat
its raw discovery in the main thread.

After each coherent group of worker completions, ask the same Explorer for a
knowledge-delta brief when contracts, assumptions, risks, or cross-package
understanding may have changed. Send compact worker outcomes and artifact
references, not logs. Reuse unchanged evidence and reopen source only when a
decision depends on freshness or contradictory facts.

## Send execution-ready capsules

Every initial worker capsule must include:

1. task ID, iteration, and required outcome;
2. owned files or symbols and protected areas;
3. relevant decisions, dependencies, interfaces, and authorized contract
   changes;
4. recommended approach, rationale, primary invariant, and likely pitfall;
5. ordered starting steps with focused checks;
6. acceptance criteria, regression boundary, and required verification;
7. escalation conditions and expected return format.

Adapt emphasis by role: Explorer receives questions, boundaries, authoritative
sources, and evidence format; executor receives the complete approach and
execution guide; verifier receives the acceptance matrix, risks, public
contracts, regression boundary, and independence requirements.

Resolve known choices in the capsule. Do not make Luna rediscover decisions the
main agent or Explorer already established. Follow-ups contain only changed
state, new evidence, the affected criterion, and the next action.

For every Luna executor, include an execution guide with:

1. verified starting state and prerequisites;
2. an ordered implementation sequence naming each exact file or symbol, the
   required change, rationale, affected interface or invariant, and focused
   check after that step;
3. edge cases, failure paths, compatibility requirements, protected behavior,
   and explicit non-goals;
4. a validation ladder from focused checks through package tests to the
   required integration or repository gate;
5. a completion checklist plus stop and escalation conditions for invalid
   prerequisites, contradictory evidence, expanded ownership, or contract
   changes.

Keep the guide execution-complete but compact. Validate it against repository
evidence before editing; adapt local details when evidence requires it and
report material deviations.

## Execute and verify

- Let an executor own local discovery, implementation, focused checks, diff
  review, and routine production repair for its package.
- Keep shared runners, schemas, registries, models, and public contracts under
  one implementation owner and integrate overlapping changes serially.
- Start independent verification only after the executor's local acceptance
  checks pass, unless test research is genuinely independent.
- Give the verifier the acceptance matrix, public contracts, risk areas,
  regression boundary, executor task name, and exact evidence locations.
- Keep production fixes with the executor. The verifier may own tests,
  fixtures, mocks, and test configuration only when assigned.
- Give every executor and verifier a stable canonical task name and include the
  counterpart's canonical name in both capsules.

For routine defects, have the verifier send the executor a compact packet with
the failed criterion, minimal reproduction, observed versus expected result,
affected contract, and focused evidence. The executor repairs in its original
scope; the verifier reruns the failed and affected checks. Allow at most two
focused repair attempts before escalating to the main agent.

Escalate immediately for scope expansion, ownership conflict, public-contract
change, invalidated architecture, security or migration risk, missing authority,
or an environment limitation that changes the accepted outcome.

Wait for worker lifecycle events instead of polling status or inspecting the
filesystem merely for activity. Send progress updates only for meaningful
assignment, handoff, knowledge-changing defect, blocker, or completion events.
After one evidence-free worker response, send one focused retry that names the
missing proof. Replace the worker after a second evidence-free response; do not
let repeated empty handoffs consume the main thread.

## Review and close

After non-trivial implementation and local acceptance are green, spawn a fresh
independent Luna reviewer at high reasoning and standard velocity with
`fork_turns="none"`. Keep it read-only. Require it to check:

- P0/P1 correctness, safety, and integration defects;
- adherence to active scope, non-goals, accepted decisions, and closure gates;
- reachability of new abstractions from a real production entrypoint;
- whether claimed verification is sufficient and newer than relevant changes.

The implementation owner reproduces and fixes valid findings, reruns the
original acceptance checks and required gates, and receives one second
independent review after P0/P1 fixes. Stop after two review waves on unchanged
scope. Fix inexpensive in-scope P2/P3 findings; otherwise record them in the
existing follow-up register with a concrete closure gate.

Close only when open P0/P1 findings are zero, the implementation matches the
active execution contract, and required checks pass. Do not stage, commit,
push, or create project documentation unless the user request independently
authorizes it.

## Keep evidence compact

Workers retain full logs, large diffs, source inventories, and diagnostics in
their thread or referenced artifacts. Return at most 250 words using:

```text
Status | Outcome | Contract changes | New facts
Verification | Residual risks | Decision required | Exact references
```

Include exact commands and artifact paths for material claims. Use
`Decision required: none` when no decision is needed. The main agent synthesizes
worker knowledge deltas without replaying operational transcripts or rerunning
fresh, coherent evidence.
