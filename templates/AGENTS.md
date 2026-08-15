## Personal Workflow Skills

- Keep planning and execution routing independent. When a substantial request
  lacks a decision-ready active plan, or the plan is stale, use the globally
  installed `$grill-me-light` skill and update the single project-owned plan
  under `docs/reviews/` by default, or at the path defined by that repository's
  instructions.
- If the request is planning-only, stop after the plan is decision-ready. When
  execution is requested from a decision-ready plan, use `$sol-luna-route` for
  substantial implementation, audit, debugging, or review work.
- Keep small localized tasks and questions on the direct single-agent path.
- Repository `AGENTS.md` files own project-specific plan paths, gates, and
  exceptions; the skills do not depend on or invoke each other.

## Execution Contract

- Before substantial execution, state the active scope, non-goals, acceptance
  checks, and ownership of shared files and contracts.
- Give shared runners, registries, schemas, models, and public contracts one
  implementation owner. Extend the canonical production route; do not create
  parallel scaffolding or duplicate execution paths.
- When `$sol-luna-route` executes implementation in a worktree, keep its current
  branch. The main agent decides commit boundaries and delegates exact staging,
  verification, and atomic green commits to one dedicated Luna Git Committer;
  other subagents do not commit. Pushes and pull requests still require separate
  authorization.
- Close only when required repository gates pass, the result matches the active
  execution contract, and open P0/P1 findings are zero. Treat requirements
  added after the first green gate as a new scoped follow-up.

## Planning and Evidence

- Keep exactly one mutable active plan per program. Keep current decisions,
  scope and non-goals, owned workstreams, closure gates, implementation order,
  open findings, and a short current review log. Put historical narrative and
  raw evidence in retained artifacts or Git history instead of parallel plans.
- Keep the plan compact enough that an implementer can find the next owned
  action, its non-goals, and its closure gate quickly.
- Distinguish outcome or promotion gates, safety or integrity gates, executable
  prerequisites, and diagnostic evidence. Only outcome and safety gates control
  delivery; tests, manifests, scores, and status bookkeeping support them.
- Route every failed gate to one concrete remediation or stop decision. A
  failed optional branch blocks that branch, not the whole task when another
  safe in-scope route remains.

## Implementation Defaults

- Treat a new abstraction as complete only when the same change connects it to
  a production entrypoint and real caller, defines its ownership boundary, and
  exercises the path with an integration or end-to-end test.
- Default to forward-only changes. Add compatibility shims or dual paths only
  when explicitly requested, and update repository call sites together.
- Run subagents at standard velocity unless the user explicitly asks otherwise.
  Let the invoked skill choose model and reasoning; keep final decisions and
  synthesis with the main agent.
