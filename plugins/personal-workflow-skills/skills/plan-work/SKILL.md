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

## Resolve execution routing

When a plan selects a peer milestone, resolve a concrete native route from the
most specific governing user instruction. An explicit user request or an
applicable user-owned `AGENTS.md` routing policy is authorization to apply the
pair; installing this skill is not authorization. Record the native model id
and reasoning value in the execution capsule, rather than only a Luna/Sol
label or a recommendation.

Use the task class and the authorized policy to select one of these exact
pairs when the current `create_thread` schema advertises both fields and
values:

| Task class | Native `model` | Native `thinking` |
| --- | --- | --- |
| Substantial decision-ready milestone | `gpt-5.6-luna` | `xhigh` |
| Bounded or mechanical milestone | `gpt-5.6-luna` | `high` |
| Visual-judgment implementation | `gpt-5.6-sol` | `medium` |
| Recovery implementation after demonstrated non-convergence | `gpt-5.6-sol` | `medium` |
| Independent objective/code review | `gpt-5.6-luna` | `xhigh` |
| Independent visual-quality review | `gpt-5.6-sol` | `high` |
| Architecture/security review, recovery diagnosis, or planning | `gpt-5.6-sol` | `high` |

Classify every milestone by one or more explicit acceptance modes before
applying the generic milestone-size route:

- `objective`: correctness is established by code, contracts, tests, or other
  deterministic evidence;
- `visual`: acceptance requires judgment of rendered composition, hierarchy,
  density, typography, responsiveness, or overall visual quality; and
- `architecture`: acceptance requires a material system-boundary, security, or
  architectural judgment.

Derive implementation and review authorities independently for every required
mode. Objective code review uses Luna XHigh. Visual-quality implementation uses
Sol Medium and its independent qualitative promotion review uses Sol High.
Architecture review uses Sol High. A milestone with objective and visual modes
needs both code correctness and rendered-quality gates; passing one never
implies passing the other. Use Luna for visually adjacent work only when the
target is frozen and the remaining change is mechanical and objectively
verifiable. The presence of frontend, CSS, slide, or document files alone does
not determine the route.

When an execution owner demonstrably stops converging, change the problem-
solving authority or approach; do not terminate the milestone merely because a
previous model or plan failed. Preserve the current diff, validation evidence,
findings, accepted intent, and remaining gap in a diagnostic continuation
packet. Sol then classifies the continuation and normally keeps working:

1. finish a bounded remaining repair;
2. replace a failed implementation strategy and continue;
3. update an implementation-level architecture assumption and continue when
   outcome, public/persisted contracts, security boundary, cost, and scope stay
   within accepted intent;
4. return `NEEDS_DECISION` only when user intent is genuinely underdetermined or
   materially different valid choices require user authority;
5. return `EXTERNAL_BLOCKED` only for a missing permission, credential, service,
   hardware, or other external prerequisite; or
6. return `FAILED` only when evidence shows the goal is not reasonably
   achievable under the accepted constraints.

`CONTINUE_WITH_REPLAN` is an internal workflow event, not a terminal result or
user escalation. A routing handoff changes authority and approach; it does not
weaken acceptance, silently expand scope, or end the milestone by itself. Bias
recovery toward completion. Never escalate merely because implementation is
difficult, the original approach was wrong, or a previous model did not
converge.

If no applicable user authorization exists, mark the capsule
`routing_status: not_authorized`, omit native overrides, and do not claim that
the route was enforced. If an authorized pair is absent from the advertised
schema, the creator must report the route as unsupported and stop; it must not
substitute another model, reasoning value, or configured default. A route that
the native call rejects is likewise a failed dispatch, not a retry opportunity.
The START contract never retries creation after any error text; it performs one
non-waiting peer-list reconciliation because a rejected-looking response is not
proof that no task exists.

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
Acceptance modes: objective | visual | architecture
Implementation and review authorities
Execution workspace:
  mode: current_checkout | existing_worktree | managed_worktree
  repository: <absolute repository root>
  path: <exact checkout/worktree path>
  branch: <branch when applicable>
  base_sha: <resolved full SHA when applicable>
  lane: <semantic program/lane slug when managed>
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
Resolved route: model=<native model id>, thinking=<native reasoning value>
Routing authorization: <governing user instruction or not_authorized>
Planning thread: <exact thread id when available>
Plan path: <canonical path>
Owned surfaces: ...
Protected surfaces: ...
Acceptance: ...
Escalate only for: ...
Completion callback: threadId=<planning thread id>, hostId=<exact native host id when available>
```

Use concrete paths and real owning components where known. Include small
interface shapes, state diagrams, or payload examples only when they remove a
material ambiguity.

## Select execution topology

Default to sequential execution: one milestone, one fresh peer execution
thread, one mutable owner. Parallel peer threads are an exception for genuinely
independent milestones with disjoint ownership and satisfied dependencies. A
fresh thread or model context does not imply a fresh Git worktree: worktrees are
owned by programs or mutable execution lanes, not models or task ids.

Select the execution workspace before dispatch. Use the current checkout when
it is safe and has one mutable owner. Reuse an existing program/lane worktree for
sequential milestones, context rollover, review, repair, recovery, and model
changes. Allocate a new worktree only for concurrent mutable ownership,
protection of pre-existing user changes, or an explicitly isolated experiment.

For repository `<parent>/<repo>`, every managed worktree lives under sibling
root `<parent>/<repo>.worktrees/` and uses a semantic `<program-slug>` or
`<program-slug>-<lane-slug>` directory with corresponding `agent/<slug>` branch.
Do not derive its identity from a thread/client id, model, or bare milestone
number. Record the exact workspace mode, repository, path, branch, base SHA, and
lane in the capsule. If native task creation cannot address that exact selected
workspace, leave dispatch unsupported; never substitute a runtime-generated
worktree.

## Dispatch when execution was requested

If the request is planning-only, stop once the canonical plan is decision-ready.
If implementation was requested:

1. Select the first executable milestone, resolve its authorized exact native
   pair and execution workspace, and finalize a compact execution capsule
   carrying the resolved exact native pair as `model` and `thinking`, routing
   authorization, and the exact workspace contract.
2. Capability-check the native `create_thread` task tool and its advertised
   `model` and `thinking` fields. Create a fresh peer Codex task/thread when
   native creation is available; if a route is authorized, require that exact
   pair before creating, while a `not_authorized` capsule may create without
   overrides. Use the `codex-thread-handoff` START contract when the bundled
   skill is available.
3. Seed the peer with only the capsule, canonical plan path, and this planning
   thread's exact callback `threadId` plus its natively returned `hostId` when
   available for material escalation and every terminal outcome. Never derive a
   callback host from a project or execution environment. This requires passing
   the same exact pair as native `model` and `thinking` arguments when
   authorization and schema support are confirmed.
4. Keep confirmed dispatch distinct from confirmed model enforcement: a
   returned thread id proves creation, while enforcement is confirmed only by
   the native response or contract acknowledging the exact pair.
5. Confirm the single logical dispatch, record the created thread id and
   routing status, and end the planning turn. Never retry a rejected or
   unsupported route and never silently use the configured default. After any
   creation error, the START contract may take one non-waiting `list_threads`
   reconciliation snapshot, but it never makes another create call.

The execution task is a peer, never a child/subagent. Do not pass inherited chat
history, poll, wait for progress, or create a persistent orchestrator. If native
peer creation is unavailable, report that limitation and leave the handoff
undispatched; never silently fall back to a subagent or another routing method.
Keep the planning thread unarchived and routable while any execution peer owes
it a terminal callback.

Before replacing any outstanding milestone whose START was uncertain or whose
terminal callback is missing, use the handoff recovery contract once. Reconcile
an uncertain START without creating anything. For a known peer, take one
non-waiting status snapshot: leave an active turn alone, resume the same thread
after an `interrupted` turn, or request callback republication when the task is
already complete. Never create a replacement merely because a server/client
error, interruption, or missing callback occurred. A replacement requires a
separate explicit user decision after the prior owner is proven unavailable or
terminal and the duplicate-work risk is reported.

## Continue after an event

Do not monitor execution routinely. When a peer later messages this planning thread with
a material escalation, decide that question once from the plan and evidence,
then send one reply to the exact execution thread. When a terminal milestone
packet arrives, verify its claimed gates in proportion to risk, update the
canonical plan and current review log, select the next now-executable milestone,
and dispatch it to a new peer context. Do not reuse the completed execution
thread as the next milestone owner. Mark the program complete only after every
required milestone and program-level promotion gate passes.

If the user explicitly asks to recover a missing handoff or interrupted task,
perform the single RECOVER_START or RECOVER_THREAD snapshot described above and
return; do not turn recovery into polling.

## Final self-review

Before closure, challenge the plan once: ensure no material decision remains
implicit, ownership and protected surfaces do not overlap, milestones are
independently closable, gates prove observable outcomes rather than status
bookkeeping, and the next execution capsule is sufficient but compact.
