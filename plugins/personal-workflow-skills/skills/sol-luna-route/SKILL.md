---
name: sol-luna-route
description: Run substantial repository implementation, audit, debugging, or review work with the main Sol agent as the decision and integration plane, bounded Luna executors and reviewers, and one dedicated Luna Git Committer for stable local checkpoints. Use when explicitly invoked with `$sol-luna-route` or when repository instructions require it for non-trivial work. Do not use for questions, planning-only requests, small localized edits, or work whose coordination overhead exceeds the operational context it would isolate.
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

## Anchor work in Git checkpoints

For implementation and repair work, treat local commits as part of this route.
Audit-only, review-only, and planning-only invocations remain read-only.

Before the first mutation, create or reuse one persistent Luna Git Committer for
the active worktree. Spawn it as `gpt-5.6-luna` at high reasoning and standard
velocity with `fork_turns="none"`. It must not delegate or edit production code,
tests, plans, or documentation. It is the only worker allowed to mutate the Git
index or create commits in the shared worktree.

Have the Git Committer inspect and report the current worktree branch, `HEAD`,
upstream or merge base when available, and the exact pre-existing dirty paths.
Sol accepts that report as the execution baseline and owns every decision about
commit boundaries and content. The current branch is the task branch: do not
create, switch, rebase, or merge a branch unless the user or repository workflow
explicitly requests it. Never stash, discard, stage, or absorb pre-existing user
changes merely to obtain a clean baseline. Escalate when they overlap the task's
owned files and prevent a safe commit.

Executors, explorers, and reviewers must not stage, commit, amend, rebase,
cherry-pick, or push. Sol normally delegates the Git mechanics instead of
running them directly, but remains the semantic Git integration owner.

When a coherent behavioral package or integrated package group has passed its
focused acceptance checks and leaves a valid repository state, Sol sends the
Git Committer a commit capsule containing:

1. expected branch and `HEAD`;
2. exact paths to include and explicit pre-existing or unrelated exclusions;
3. accepted check results with their freshness and evidence references;
4. the coherent outcome and imperative commit subject;
5. expected residual working-tree paths after the commit.

The Git Committer then:

- verifies the branch, `HEAD`, dirty-path baseline, and that Sol has synchronized
  every worker that could still mutate an included path;
- stages only the exact owned paths and inspects `git status --short`,
  `git diff --cached --name-status`, `git diff --cached --check`, and the staged
  patch; it never uses broad staging in a mixed worktree;
- confirms the commit includes the implementation, tests, contract updates,
  migration, and necessary documentation for one coherent outcome;
- refuses the commit on unexpected paths, changed `HEAD`, stale acceptance
  evidence, ownership ambiguity, or a staged diff that does not match the
  capsule, and returns the mismatch to Sol without repairing it;
- creates the commit and reports its SHA, included paths, validation evidence,
  and residual status to Sol.

Sol records the accepted commit SHA, validation performed, remaining scope, and
next package in the existing execution plan or status surface. Avoid red,
speculative, log-only, or arbitrary time-based checkpoint commits; retain a
patch or evidence artifact instead when no valid boundary exists.

Once a commit SHA has been handed to a reviewer, keep it stable. Review the
committed range from the recorded base or prior accepted checkpoint through the
new SHA, plus any explicitly named residual working-tree changes. Add verified
repairs as a new atomic commit; amend only an unpushed commit that has not yet
become a review or handoff boundary.

Before final closure, require a clean task-owned diff or name every intentional
residual path, and report the base SHA, final commit sequence, and current
branch. Local checkpoint commits do not authorize a push, pull request, merge,
tag, release, or remote history rewrite.

## Keep Sol as the knowledge plane

The main agent owns task direction, architecture, package boundaries, contract
changes, integration order, acceptance decisions, official status, and user
communication.

The main agent should normally use tools only for initial state checks, compact
coordination, decision-critical evidence, integration inspection, and final
verification. Delegate broad repository reads, routine implementation,
diagnostics, test logs, repair iterations, and Git checkpoint mechanics. Inspect
underlying worker evidence only when it is contradictory, incomplete, stale,
high risk, or needed for a material decision.

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
active execution contract, required checks pass, and the task-owned work is
captured in coherent local commits. Do not push, open or merge a pull request,
tag, release, rewrite remote history, or create unrelated project documentation
unless the user request independently authorizes it.

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
