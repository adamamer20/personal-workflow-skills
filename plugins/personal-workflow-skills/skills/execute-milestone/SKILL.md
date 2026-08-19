---
name: execute-milestone
description: Execute exactly one decision-ready milestone from a canonical plan in a fresh implementation context. Own implementation, testing, ordinary repair, self-review, safe local commits, and one terminal peer-thread callback; escalate only material decisions. Not for program planning or routine progress coordination.
---

# Execute Milestone

Own exactly one decision-ready milestone end-to-end. The planning thread owns
the program plan, architecture decisions, milestone order, scope changes, and
program completion. This execution thread owns focused discovery,
implementation, tests, ordinary repair, its code and documentation, milestone
evidence, self-review, and safe local commits.

The capsule must also contain the planning callback `threadId` and, when the
native tools returned one, its exact `hostId`. Never derive a host from a
project, environment, path, or model runtime. If the callback route is missing
or ambiguous, resolve it read-only before mutation; if that fails, report the
invalid capsule in this task and stop before mutation. Do not defer route
discovery until the terminal step.

Do not act as a program planner, continuously coordinate with the planning
thread, or create additional mutable owners unless the milestone explicitly
contains independent parallel work with disjoint ownership.

## Establish the milestone contract

Before mutation, read repository instructions, the canonical plan, relevant
code/tests, branch, and worktree status. State the milestone outcome, active
scope and non-goals, owned and protected surfaces, dependencies, acceptance and
promotion gates, review requirement, and escalation target. Shared runners,
registries, schemas, models, persisted or public contracts, and production
entrypoints have one implementation owner.

Reject or escalate a capsule that is not decision-ready rather than inventing a
material product or architecture decision. Reuse the canonical production path;
do not build parallel scaffolding or a duplicate execution route.

## Model routing

The planning and handoff creator resolve the execution role to a concrete
native pair under the governing user/`AGENTS.md` policy. The default authorized
pairs are `gpt-5.6-luna` with `thinking=high` for bounded or mechanical work,
`gpt-5.6-luna` with `thinking=xhigh` for substantial milestones and independent
normal reviews, and `gpt-5.6-sol` with `thinking=high` for critical
architecture/security review or planning. The creator applies the resolved
pair to a fresh peer's native `create_thread` call; this skill cannot change
the model of the task that is already running.

Skill installation alone does not authorize model overrides. Without an
applicable user authorization, the creator omits `model` and `thinking` and
reports that routing was not enforced. If an authorized pair is not advertised
by the native schema or is rejected by the native call, dispatch fails closed:
there is no default-model, alternate-model, or retry fallback.

The role guidance is therefore a task-creation contract, not capsule-only
recommendation text. A returned peer id confirms dispatch; model enforcement
is a separate fact that requires native confirmation of the exact pair.

## Execute and validate

Implement, run focused checks, debug routine failures, self-review the complete
owned diff, repair valid findings, then run the milestone's required repository
gates. Tests, manifests, counts, and ledgers are diagnostic evidence unless the
plan names them as an outcome or safety gate. Close only when required outcome
and safety gates are green, the contract is satisfied, and open P0/P1 findings
are zero.

Independent review is a peer-thread promotion gate only when the plan names a
high-risk or subjective gate. Use one fresh reviewer. A second review is
justified only after a P0/P1 repair materially changes the reviewed surface.

## Escalate material decisions only

Message the exact planning thread id only when:

- required scope must materially expand;
- an accepted architecture or ownership boundary must change;
- a public or persisted contract must change unexpectedly;
- repository evidence contradicts an accepted plan decision;
- an uncovered security, privacy, data-integrity, permission, or destructive
  operation decision is required; or
- a dependency or environment limitation makes the accepted outcome impossible.

Do not escalate ordinary implementation choices, helper structure, naming,
routine test or lint failures, debugging, ordinary refactoring, progress,
checkpoint completion, or lower-priority cleanup already covered by the plan.

Send exactly one compact escalation containing the decision required, evidence,
safe options, and current checkpoint. Do not poll, wait for, or repeatedly list
the planning thread. If the decision blocks safe work, stop the execution turn;
the planning thread replies to this execution thread when a decision exists.

This restriction governs mid-execution messages. It does not remove the
required terminal callback for a final `BLOCKED` or `FAILED` outcome.

## Hard context rollover

Every implementation context has a hard lifecycle, whether it is a main, peer,
or subagent context. Roll over after two major compactions, or—when telemetry is
available—75M tokens or 500 model calls. Do not spend work merely to measure
unavailable telemetry.

At a hard threshold, finish only the current bounded operation, reach the
nearest safe green checkpoint, do not begin another substantial package, and
create a compact continuation handoff. Continue only in a fresh peer execution
thread after native creation is confirmed. Preserve logical ownership while
changing context; never preserve an exhausted context merely to preserve
ownership. One milestone normally equals one thread, but an unexpectedly large
milestone may roll over this way.

If the native runtime cannot create a peer thread, send a terminal `ESCALATION`
to the planning callback target, report the limitation, and stop at the safe
checkpoint. Never silently fall back to a subagent.

## Git safety

The milestone owner may create its own local commits when the user or plan
requests checkpoints. Before committing:

1. inspect the current branch and `git status`;
2. preserve all pre-existing unrelated changes;
3. stage only exact task-owned paths;
4. inspect `git diff --cached --name-status` and `git diff --cached`;
5. run `git diff --cached --check`; and
6. commit only a coherent green milestone outcome.

Never push, rebase, merge, stash, discard, amend reviewed history, or rewrite
remote history without separate authorization. Use a separate Git integration
owner only when multiple mutable peer threads share a worktree, dirty changes
overlap staging risk, or repository instructions require one.

## Subjective-quality promotion

For slides, frontend or UI/UX, document generation, visual systems, and
creative or prose quality, inventory and self-authored metadata cannot prove
promotion:

1. Define three to five fixed sentinel artifacts before scaling the system.
2. Produce them through the canonical production path.
3. Inspect the actual rendered result at intended viewing size.
4. Apply the plan's observable rubric with an independent qualitative authority.
5. Scale catalogs, components, or recipes only after the sentinel set passes.

Component, recipe, manifest, trace, test, or ledger counts remain supporting
diagnostics. The implementation owner must not be the sole qualitative
authority for a subjective promotion gate.

## Production reachability and replacement safety

A registered abstraction is not an implemented abstraction. A component,
adapter, plugin, or recipe is reachable only when changing or selecting it
changes observable behavior through the canonical production entrypoint.
Catalog membership, configuration references, traces, and metadata counts do
not prove production reachability.

For replacements and refactors, prove parity before deletion: implement the
replacement, migrate the real caller, prove integration or end-to-end behavior
and required parity, and add replacement tests before removing the old path or
tests. Deletion of the old path is not evidence that the replacement is
complete.

## Terminal callback

Every terminal exit must attempt exactly one bounded callback to the exact
planning route before this execution task ends:

- `COMPLETION` only when all milestone gates pass;
- `ESCALATION` with `Status: BLOCKED` when a dependency, decision, permission,
  or environment prevents completion; or
- `ESCALATION` with `Status: FAILED` when the milestone exhausts ordinary repair
  without satisfying its gates.

The packet includes:

```text
Milestone
Status and observable outcome
Commit(s)
Acceptance criteria
Validation
Contract changes
Residual risks
Recommended next milestone
```

Do not attach raw logs, poll for acknowledgement, monitor progress, or claim
program completion. Delivery is a closure gate separate from work status. End
with both `work_status` and `callback_status: sent|unsent`. If native messaging
is unavailable, the target is archived, the route is not safely resolvable, or
dispatch fails, do not invent a host, unarchive the target, or claim delivery.
Put `callback_status: unsent`, the exact target and native error, and the complete
unsent packet in this task's final response so the terminal outcome remains
recoverable.
