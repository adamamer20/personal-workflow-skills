## Personal Workflow Skills

- For a new substantial or architecturally uncertain program, start with a Sol
  High planning thread and use `$plan-work`. Update the single project-owned
  canonical plan under `docs/reviews/` by default, or at the path defined by
  repository instructions.
- If planning-only was requested, stop when the plan is decision-ready. If
  execution was requested, the planning thread selects the first executable
  milestone and uses `$codex-thread-handoff` to create a fresh peer execution
  thread when native peer creation is available. Never silently substitute a
  child/subagent.
- Substantial milestones normally use Luna XHigh with `$execute-milestone`;
  bounded or mechanical work may use Luna High directly.
- Every milestone declares one or more acceptance modes: `objective`, `visual`,
  and `architecture`. Route by the judgment required for acceptance, not by file
  type. Objective code review uses Luna XHigh. Visual-judgment implementation
  uses Sol Medium and independent visual-quality promotion review uses Sol High.
  Architecture/security review uses Sol High. When objective and visual modes
  both apply, require both authorities; passing code tests never implies that a
  rendered result is good. Luna is appropriate for visually adjacent work only
  after the target is frozen and the remaining execution is mechanical and
  objectively verifiable.
- When an owner stops converging, preserve the diff, evidence, findings,
  accepted intent, and remaining gap for a Sol diagnostic continuation. Sol
  finishes a bounded repair, changes implementation strategy, or replans and
  continues when outcome, public/persisted contracts, security boundary,
  material cost, destructive behavior, and scope remain within accepted intent.
  A change of authority or approach is not a terminal condition. Escalate to the
  user only as `NEEDS_DECISION` when intent is genuinely underdetermined or new
  authority is required. Use `EXTERNAL_BLOCKED` only for missing credentials,
  permissions, services, hardware, or other external prerequisites, and
  `FAILED` only when the goal is not reasonably achievable under accepted
  constraints. `CONTINUE_WITH_REPLAN` is internal and nonterminal.
- These defaults are user-owned routing authorization for native peer creation:
  when a fresh peer is created, the handoff must pass the exact pair
  `model=gpt-5.6-luna, thinking=xhigh` for a substantial milestone or
  independent objective/code review, `model=gpt-5.6-luna, thinking=high` for
  bounded/mechanical work, `model=gpt-5.6-sol, thinking=medium` for visual-
  judgment or recovery implementation, and `model=gpt-5.6-sol, thinking=high`
  for visual-quality promotion review, recovery diagnosis,
  architecture/security review, or planning. The most specific applicable user
  instruction wins.
- A plugin installation alone is not authorization to override native task
  settings. If the native schema does not advertise an authorized pair, or
  native creation rejects that pair, stop without retrying, substituting a
  model, or falling back to the configured default. Error text alone does not
  prove that no task was created; the handoff may take one non-waiting
  `list_threads` reconciliation snapshot after an error, but it never retries
  `create_thread`. If no user authorization applies, omit the overrides and
  state that routing was not enforced.
- Execution threads message the planning thread only for a material escalation
  or terminal milestone outcome. Every terminal outcome returns exactly one
  `COMPLETION` or accurately labelled `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, or
  `FAILED` escalation using the exact callback thread/host route. Replanning
  within accepted intent continues automatically and is not terminal. Keep the
  planning thread unarchived while a peer owes it a callback. The planning
  thread never polls execution and execution sends no routine progress updates.
- A failed-looking START is never automatic retry authorization. Reconcile it
  once without creating anything; if the host is unavailable, preserve an
  uncertain recovery capsule. Before replacing a peer whose callback is missing,
  take one non-waiting status snapshot: leave active work alone, resume the same
  thread after an `interrupted` turn, or request callback republication after a
  completed turn. Never create a replacement until the prior owner is proven
  unavailable or terminal and the user explicitly authorizes replacement.
- One milestone normally uses one fresh execution context. Program ownership
  may persist across milestones; task context does not. Use parallel peers only
  for genuinely independent milestones with disjoint mutable surfaces.
- A fresh Codex thread or model does not imply a fresh Git worktree. The
  execution workspace belongs to the program/lane and is reused for sequential
  milestones, context rollover, repair, recovery, model changes, and read-only
  review while mutable ownership remains singular. Create another worktree only
  for concurrent mutable ownership, protection of pre-existing user changes, or
  an explicitly isolated experiment.
- Managed worktrees for `<parent>/<repo>` live under sibling root
  `<parent>/<repo>.worktrees/` and use a semantic `<program-slug>` or
  `<program-slug>-<lane-slug>` directory with matching `agent/<slug>` branch.
  Never name them from a thread/client id or model. The plan chooses
  `current_checkout`, `existing_worktree`, or `managed_worktree`; handoff does
  not invent topology or replace an unavailable exact workspace with a
  runtime-generated worktree.
- Keep small localized tasks and questions on the direct single-owner path.
- Repository `AGENTS.md` files own project-specific plan paths, gates, and
  exceptions.

## Execution Contract

- Before substantial execution, state active scope, non-goals, acceptance and
  promotion checks, owned and protected surfaces, and ownership of shared files
  and contracts.
- Give shared runners, registries, schemas, models, persisted/public contracts,
  and canonical production entrypoints one implementation owner. Do not create
  parallel scaffolding or duplicate production paths.
- Each execution owner implements, tests, repairs, self-reviews, and may create
  safe local commits for its milestone. Preserve unrelated changes and require
  separate authorization for pushes, rebases, merges, stashes, discards, or
  remote-history changes.
- Close a milestone only when required outcome and safety gates pass, the
  execution contract matches the result, and open P0/P1 findings are zero.
  Requirements added after the first green gate are a newly scoped follow-up.

## Planning and Evidence

- Keep exactly one mutable active plan per program. The planning thread owns
  decisions, milestone ordering, scope changes, plan updates, and program
  closure; execution peers return compact evidence and terminal outcomes.
- Keep the plan compact enough that an executor can find its owned action,
  protected surfaces, non-goals, escalation conditions, and closure gate.
- Distinguish outcome or promotion gates, safety or integrity gates, executable
  prerequisites, and diagnostic evidence. Tests, manifests, counts, scores, and
  status bookkeeping support but do not replace observable outcome proof.
- Route every failed gate to one remediation or stop decision. A failed optional
  branch blocks that branch, not the whole program when a safe route remains.
- Escalate to the user only when intent is genuinely underdetermined or an
  operation needs user authority. Difficulty, non-convergence, or a disproven
  implementation plan triggers diagnosis and replanning, not user interruption.

## Universal Invariants

- One mutable owner per surface and one canonical active plan per program.
- One milestone normally equals one fresh execution context; use hard rollover
  at two major compactions, or at 75M tokens or 500 calls when telemetry exists.
- Runtime reality outranks catalog presence: an abstraction is complete only
  when a real caller changes observable behavior through the production
  entrypoint and the path has integration or end-to-end proof.
- Prove replacement reachability and required behavioral parity before deleting
  an old path or its tests.
- Subjective promotion requires fixed sentinel artifacts, real rendered-output
  inspection, and an independent qualitative authority—not inventory or
  self-authored status metadata alone.
