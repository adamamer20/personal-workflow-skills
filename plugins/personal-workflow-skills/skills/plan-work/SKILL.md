---
name: plan-work
description: Turn substantial or architecturally uncertain work into one decision-ready canonical plan with independently closable milestones. Inspect the repository first, ask only material questions, and, when execution was requested, hand the first executable milestone to a fresh peer Codex thread if native peer creation is available. Not for small localized changes.
---

# Plan Work

Leave execution with no need to invent product intent, architecture, ownership,
important contracts, or success criteria. Keep one active plan for the program.

## Inspect and decide

1. Read the relevant repository instructions, active plan, architecture, tests,
   and current worktree before asking questions. Treat established conventions
   as defaults.
2. Form a concrete recommended design first. Use bounded read-only
   reconnaissance helpers only when they materially save context; the planning
   thread owns synthesis and decisions.
3. Ask only questions that can materially change behavior, architecture,
   ownership, public or persisted contracts, security/privacy/data integrity,
   destructive operations, important cost/performance tradeoffs, or acceptance.
   Ask the highest-information question first and include a recommendation.
4. Normally ask 0–4 questions; use up to 6 for complex or high-risk work. Resolve
   low-risk, reversible implementation details from repository evidence.
5. Prefer the simplest design that meets the accepted outcome. Do not add
   speculative extensibility or a parallel production path.

Planning is decision-ready when a capable executor can identify the outcome,
scope and non-goals, architecture and ownership, mutable and protected surfaces,
contracts and failure behavior, acceptance, validation, and promotion gates
without making a material product decision.

## Own one canonical plan

Update exactly one active plan at the path defined by repository instructions
or the user. If no durable path is defined, return the plan in the task unless
the user explicitly asks to create a file. The planning thread owns this plan,
architecture decisions, milestone ordering, scope changes, and program closure.
Execution threads report results; they do not continuously rewrite it.

Keep current decisions, evidence, findings, and next action compact. Retain raw
logs and historical narrative elsewhere or in Git history rather than creating
a second mutable roadmap.

For program-sized work, decompose execution into independently closable
milestones. A milestone must normally fit in one fresh implementation context
and end at a demonstrable green checkpoint. Do not assign an entire
multi-milestone program to one execution thread. Combine adjacent milestones
only when both remain small and coherently owned.

Use this plan shape, omitting empty sections rather than filling placeholders:

```markdown
# Plan: <program>

## Goal
## Current context
## Proposed design
## Contracts and invariants
## Program scope and non-goals

## Milestone M1 — <name>
Outcome
Mutable ownership
Protected surfaces
Dependencies
Implementation boundary
Acceptance criteria
Validation
Promotion gate
Review requirement
Sentinel artifacts (when subjective quality matters)
Successor milestone

## Milestone M2 — <name>
...

## Assumptions
## Open findings
## Current review log

## Next execution
Milestone: M1
Recommended executor: Luna XHigh
Planning thread: <exact thread id when available>
Plan path: <canonical path>
Owned surfaces: ...
Protected surfaces: ...
Acceptance: ...
Escalate only for: ...
Completion callback: <planning thread id>
```

Use concrete paths and real owning components where known. Include small
interface shapes, state diagrams, or payload examples only when they remove a
material ambiguity.

## Select execution topology

Default to sequential execution: one milestone, one fresh peer execution
thread, one mutable owner. Parallel peer threads are an exception for genuinely
independent milestones with disjoint ownership and satisfied dependencies.
Simultaneous code changes should use isolated worktrees unless the repository
has another safe integration boundary.

## Dispatch when execution was requested

If the request is planning-only, stop once the canonical plan is decision-ready.
If implementation was requested:

1. Select the first executable milestone and finalize its compact execution
   capsule.
2. Capability-check for the native `create_thread` task tool. Create a fresh
   peer Codex task/thread only when that native operation is available. Use the
   `codex-thread-handoff` START contract when the bundled skill is available.
3. Recommend Luna XHigh for a substantial milestone and Luna High for bounded
   or mechanical execution. Model selection is routing metadata, not workflow
   identity.
4. Seed the peer with only the capsule, canonical plan path, and this planning
   thread's exact id for material escalation and terminal completion.
5. Confirm dispatch, record the created thread id, and end the planning turn.

The execution task is a peer, never a child/subagent. Do not pass inherited chat
history, poll, wait for progress, or create a persistent orchestrator. If native
peer creation is unavailable, report that limitation and leave the handoff
undispatched; never silently fall back to a subagent or another routing method.

## Continue after an event

Do not monitor execution. When a peer later messages this planning thread with
a material escalation, decide that question once from the plan and evidence,
then send one reply to the exact execution thread. When a terminal milestone
packet arrives, verify its claimed gates in proportion to risk, update the
canonical plan and current review log, select the next now-executable milestone,
and dispatch it to a new peer context. Do not reuse the completed execution
thread as the next milestone owner. Mark the program complete only after every
required milestone and program-level promotion gate passes.

## Final self-review

Before closure, challenge the plan once: ensure no material decision remains
implicit, ownership and protected surfaces do not overlap, milestones are
independently closable, gates prove observable outcomes rather than status
bookkeeping, and the next execution capsule is sufficient but compact.
