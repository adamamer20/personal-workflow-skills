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
- A milestone whose acceptance depends on subjective visual judgment uses Sol
  Medium by default. This includes slide composition, landing pages, frontend
  or UI design, visual systems, and rendered-document quality. Use Sol High for
  new or system-wide visual direction, weak or conflicting references,
  remediation after repeated visual misses, or the independent final
  qualitative promotion review. Luna is appropriate for visually adjacent work
  only after the target and acceptance criteria are frozen and the remaining
  execution is mechanical and objectively verifiable.
- If Luna completes two implementation/review repair cycles without closing the
  same material blocker, or the same class of finding is reopened, stop that
  Luna context and route the bounded continuation to a fresh Sol Medium task.
  If that Sol Medium continuation also completes two unsuccessful repair cycles
  on the same blocker or finding class, route it once to fresh Sol High. Carry
  the current diff, validation evidence, open findings, and remaining acceptance
  gap at each handoff. If Sol High exhausts ordinary repair, return a terminal
  `BLOCKED` or `FAILED` outcome instead of continuing the loop. Do not weaken the
  gate or silently expand scope. A more specific Sol High route still wins
  immediately for critical visual, architecture, or security work.
- These defaults are user-owned routing authorization for native peer creation:
  when a fresh peer is created, the handoff must pass the exact pair
  `model=gpt-5.6-luna, thinking=xhigh` for a substantial milestone or
  independent normal code review, `model=gpt-5.6-luna, thinking=high` for
  bounded/mechanical work, `model=gpt-5.6-sol, thinking=medium` for visual-
  judgment implementation or a Luna review-loop escalation, and
  `model=gpt-5.6-sol, thinking=high` for a Sol Medium review-loop escalation,
  critical visual direction or promotion review, architecture/security review,
  or planning. The most specific applicable user instruction wins.
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
  `COMPLETION` or accurately labelled `BLOCKED`/`FAILED` escalation using the
  exact callback thread/host route. Keep the planning thread unarchived while a
  peer owes it a callback. The planning thread never polls execution and
  execution sends no routine progress updates.
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
