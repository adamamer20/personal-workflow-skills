---
name: sol-luna-route
description: Route substantial repository implementation, audit, debugging, or review work through the smallest safe Sol/Luna execution mode. Keep Sol as the decision plane when present, delegate bounded operations to Luna, and use conditional checkpoints and reviews. Do not use for questions, planning-only work, or small localized edits.
---

# Sol-Luna Route

Keep requirements, architecture, decisions, acceptance, and final synthesis in
the current main thread (Sol when present). Move noisy operational work to
isolated Luna threads. Choose the smallest mode that gives every mutable
surface one owner; never create parallel scaffolding or duplicate production
routes.

## Select one execution mode

### Single-owner / Direct Luna

Choose this when the plan is fresh and decision-ready, work is sequential, and
there is no open architecture decision. Keep one mutable owner end-to-end. If
the current main is Luna, execute directly. If the current main is Sol, use one
Luna implementation owner end-to-end and keep Sol episodic for decisions and
synthesis.
Do not add an Explorer, verifier, committer, or reviewer unless a named risk or
requested local commit requires it; a final independent review is conditional
on non-trivial risk.

### Sol-Luna Lite

Choose this for a shared boundary or non-trivial risk with one mutable owner
when Full's cross-workstream coordination is unnecessary.
Sol defines the execution contract once; one Luna owner executes it; use
milestone checkpoints and one independent reviewer. An Explorer is optional
when the plan and evidence are fresh. Use a dedicated Luna Git Committer when
commits/checkpoints are requested, the worktree is mixed or dirty, multiple
agents are active, or a stable handoff boundary is required; otherwise make it
conditional and checkpoint only at milestones or final closure.

### Sol-Luna Full

Choose this only for at least two genuinely independent workstreams or mutable
owners, dynamic replanning, or cross-workstream privacy, security, migration,
concurrency, or data-integrity risk. Start all currently independent packages
concurrently up to the configured/session cap; keep dependent or overlapping
ownership serial. Use Explorer and verifier roles only when the named risk
warrants them. Retain the Git safety contract below.

## Model and pace

Start controlled runs with Sol at Medium reasoning; use High only for
architecture, security, migration, or critical review. Luna implementation and
review workers use High; Explorer and Git mechanics use Medium unless critical
complexity justifies High. Use standard velocity. This skill cannot change the
current main-thread model or effort; never claim that it did or restart a task
only to change it unless the user requests that reset.

## Establish the contract

Before substantial execution, state the outcome, active scope, non-goals,
protected surfaces, acceptance checks and repository gates, ownership of shared
files/schemas/registries/runners/public contracts, and stop or escalation
conditions. Reuse the repository's one active plan or follow-up register; do
not create a parallel roadmap. Keep Sol responsible for boundaries, decisions,
integration order, acceptance, status, and user communication.

## Coordinate economically

- Keep every worker on standard velocity and give each one coherent ownership.
- Do not routinely call `list_agents`; for long waits, use lifecycle events and
  event-driven `wait_agent` (target root waits <12/hour).
- After one evidence-free handoff, send one focused evidence request; replace or
  escalate after the second empty handoff. Do not send status messages merely
  because a timeout occurred.
- When telemetry exists, target <=100–150 main model calls per
  milestone. Do not burn turns trying to measure unavailable telemetry.
- Rollover a worker at a green milestone after 2–3 substantial packages, or
  (when telemetry exists) 50–75M tokens, 300–500 calls, or two major compactions.
  Compact the handoff and preserve the same mutable ownership one worker at a
  time.
- By every two checkpoints or milestones, ask whether a demonstrable vertical
  user outcome exists. Once closure gates pass, move non-blocking cleanup to
  the existing follow-up register.

## Assign and run workers

Use `gpt-5.6-luna` with `fork_turns="none"`; prohibit delegation unless the
package explicitly permits it. Give each capsule the task ID, exact owned and
protected paths, decisions and interfaces, approach and invariant, ordered
steps with focused checks, acceptance/regression boundary, and escalation
conditions. Resolve choices in the capsule; do not make Luna rediscover them.

The executor owns local discovery, implementation, focused checks, and routine
repairs. Shared runners, schemas, registries, models, and public contracts have
one implementation owner; overlapping work is serial. An executor self-reviews
always. Start a verifier only for a named privacy, security, migration,
concurrency, data-integrity, or public-contract risk. A verifier reports a
minimal corrective packet; the executor fixes it. Escalate scope expansion,
ownership conflict, invalidated architecture, missing authority, or an
environment limit that changes the accepted outcome.

## Git safety and checkpoints

The current branch is the task branch. Never create, switch, rebase, merge,
stash, discard, or absorb pre-existing user changes. Record the exact dirty-path
baseline before mutation and preserve unrelated paths. When a local commit or
checkpoint is requested, create or reuse one persistent Luna Git Committer at
the first green milestone in every mode, including Direct Luna. Do not create a
committer for read-only work or before a valid checkpoint exists merely because
this route is active. The committer is the only agent allowed to mutate the Git
index or create commits; it cannot edit, fix, stage broad paths, or repair
production code. Main implementation agents, executors, Explorers, verifiers,
and reviewers never stage or commit.

Send the Git Committer a commit capsule with expected branch/HEAD, exact include
paths and exclusions, fresh accepted checks, coherent imperative subject, and
expected residual paths. The committer verifies branch/HEAD and baseline,
stages only the exact owned paths, inspects status, cached name-status, cached
check, and the staged patch, and refuses the commit on unexpected paths, stale
evidence, ownership ambiguity, or a mismatched capsule. A commit capsule must
include implementation, tests, contracts, migrations, and necessary docs for
one outcome. Once a commit SHA has been handed to a reviewer, keep it stable;
repair with a new atomic commit. Local checkpoint commits do not authorize a
push, pull request, merge, tag, release, or remote rewrite without separate
user authorization.

## Review, tests, and close

When the selected mode or named risk requires independent review, review the
definitive commit range at final closure. Do not add a duplicate final reviewer
when a verifier already reviewed that exact range; add a second review only
after P0/P1 findings. The final reviewer checks P0/P1 correctness, safety,
scope/non-goals, real production reachability, and fresh evidence. The
implementation owner reproduces valid findings and reruns checks.

Run focused tests per package, integration tests for each coherent 3–5 package
group, and the full suite at each milestone/final closure or immediately after
a high-risk or shared-contract change. Close only with required outcome and
safety gates green, open P0/P1 findings at zero, the contract satisfied, and
coherent local commits or explicitly named intentional residuals. Return
compact evidence with exact commands and artifact references. Keep full logs
and large diffs in worker threads or artifacts; return at most 250 words in the
form `Status | Outcome | Contract changes | New facts` and
`Verification | Residual risks | Decision required | Exact references`.
