## Codex Workflow

### Controller and skill ownership

- The workflow controller owns session lifecycle, model routing, workspaces,
  idempotency, budgets, callbacks, durable state, retries, and recovery
  triggers.
- Skills own bounded reasoning: planning, implementation, review, recovery
  diagnosis, and the durable artifacts described by the shared schemas.
- Skills do not create or manage workers, threads, worktrees, callbacks,
  polling loops, retries, or model fallbacks. A fresh context does not imply a
  fresh workspace.
- Routing is model-independent at the skill boundary: the controller resolves
  role classes from its configuration.
- Native implementation/review/recovery tasks are leaves: no subagents, peer
  dispatch, independent review launches or successors. Implement/test/self-review
  or perform the assigned review, then send one terminal callback to the exact
  controller route. Report pending independent acceptance; delivery is not
  promotion. The controller owns reviews, repairs and successor scheduling.
- One mutable owner controls a surface at a time. Shared contracts and
  production entrypoints have one integration owner.

### Planning and guarantees

- Keep one lightweight current plan and a separate history document. The plan
  owns current outcome, risks, readiness, ownership and next gate; history
  retains closed/superseded decisions and linked evidence. Replace stale status
  instead of appending chronological overlays. Keep active contracts explicitly
  selected and reachable; historical capsules never authorize new work.

- Start from the outcome and material unknowns. Resolve them with existing
  evidence or the smallest bounded experiment before speculative design. Make
  the first feasible implementation milestone a complete production vertical;
  a prerequisite foundation names the concrete obstruction and smallest remedy.
  Verify the oracle accepts a valid alternative and rejects a wrong result when
  its assumptions could invalidate the outcome. Freeze one deliverable and
  non-goals; keep newly discovered ambitions in successors.
- Detail architecture for the next ready milestone after discovery. Later nodes
  retain outcome, prerequisite evidence, decision owner and readiness. Keep
  evidence to observed outcome, exact candidate, important gates, changed
  contracts, blocker and remainder; preserve domain-specific proof when needed.
- Use controller reasoning for material decisions, not continuous supervision.
  Existing deterministic runtime transitions remain harness-owned; these rules
  add no automatic retry or new control plane. Controllers may assign bounded
  read-only scouts or ready disjoint lanes; worker leaf capabilities do not
  expand because a helper would be convenient.
- Once the implementation architecture map is frozen, attempt a dependency
  DAG of independently closable vertical milestones with disjoint mutable
  surfaces and record current readiness. Freeze shared schemas, contracts,
  state authority, and production entrypoints before fan-out. Every retained
  serial edge records a concrete reason—shared schema, state authority,
  entrypoint, migration order, or acceptance dependency. Minimize the safe
  critical path without inventing fake boundaries or maximizing milestone
  count. Milestones remain single-owner; parallelism is only between ready
  disjoint lanes. Read-only scouts and distinct review authorities may overlap,
  but nested swarm/controller orchestration is rejected as a default.
- A planning/controller turn may issue multiple START operations for ready
  disjoint milestones. “One bounded handoff” is per peer and milestone, so
  each receives one owner and one START; dependencies and shared mutable
  surfaces still prohibit parallel starts.
- Before a guarantee becomes a gate, name the required outcome, prevented
  failure, boundary, evidence, cost, and simpler alternative.
- Do not turn an implementation mechanism into a guarantee. Universal claims
  require a named threat model and isolation boundary; otherwise constrain the
  guarantee to an observable product, trust, persistence, API, logging,
  telemetry, callback, or evidence boundary.
- Treat disproportionate implementation complexity as evidence to revisit the
  guarantee or boundary, not as a reason to add another control plane.

### Semantic density

- Optimize semantic compression, not abstraction count. Under-abstraction matters
  when repeated domain decisions do not disappear from call sites. Choose the
  smallest representation that makes the invariant obvious; treat semantic
  vocabulary as a budget requiring an invariant, identity, policy or algorithm.
- Keep strict edges and boring interiors: validate once at boundaries, colocate
  behavior/invariants, remove pass-through layers. Functions are the default;
  classes need state/lifecycle/policy. A one-implementation protocol needs a real
  replaceable boundary. Use typed boundary codecs for domain repetition, not
  incidental syntax. Tests express observable guarantees, not helper structure.

### Execution, review, and recovery

- The milestone owner implements, validates, debugs ordinary failures,
  self-reviews, and repairs valid findings in the assigned workspace.
- Every code milestone requires validation/self-review. Small reversible work
  without a review trigger can close on the direct path outside workflow-control.
  Controller capsules require every declared review authority. Select modes/lenses
  before dispatch from contract, ownership, security/privacy, migration, integration,
  prior-finding or visual risk. Do not skip a declared authority afterwards.
- Review is one general workflow; the selected lens changes its evidence, not
  its ownership or transport boundary.
- Every review invocation names exactly one lens (`spec`, `correctness`,
  `standards`, `contract-risk`, `security`, or `visual`). The default is one
  focused review context. Use Sol Medium for bounded architecture conformance;
  escalate to Astra Medium only for a significant or ambiguous combined
  architecture/security boundary or when Sol cannot close the question. Do not
  duplicate objective and architecture reviews unnecessarily at mode selection;
  all declared capsule authorities remain required. Parallel distinct authorities
  require materially independent evidence and judgments, with both explicitly
  required by the plan.
- Findings record severity separately from promotion impact. P0 is normally
  blocking; P1 blocks when it invalidates the outcome, a protected boundary,
  or safe successor work. A non-blocking finding records its owner and
  hardening destination.
- After two attempts at the same failure without new causal evidence or
  observable improvement, diagnose before a third attempt. Distinguish oracle,
  scope, implementation and environment. The controller considers context
  rollover when work changes nature, a premise changes or compaction loses
  causal context; reuse the workspace and preserve evidence. No automatic
  stronger-model escalation follows.
- Check mergeability against the intended base before declaring readiness;
  relevant integration gates must pass on the actual integrated candidate.
  Record scope start, first vertical, mergeability, merge, external waits,
  known cost and reopenings in existing history; do not add a metrics platform.
- Recovery diagnoses the causal failure and may finish or replan within the
  accepted outcome, contracts, safety boundaries, cost, and scope. It asks for
  a user decision only when those materially change or external authority is
  missing.
- A milestone closes only when its outcome and safety gates pass and no open
  promotion-blocking P0/P1 finding remains. Final integrated review and
  hardening happen after the end-to-end path works.

### Workspace policy

- The program worktree is the sole local integration trunk. Before fan-out,
  freeze a verified authorized commit as DAG base; separate unrelated dirty
  baseline safely. Reuse the workspace across sequential work, rollover and
  repair. New workspaces serve concurrent ownership, dirty-state protection
  or explicitly isolated experiments.
- Parallel lanes are physically sibling Git worktrees below
  <repo-parent>/<repo-name>.worktrees/<program-slug>-<lane-slug>, branch
  agent/<program-slug>-<lane-slug>, from the exact base/predecessor. Use semantic
  names, not model/thread IDs. Workers never modify or integrate the trunk.
- Mutable completion requires an owned coherent commit after staged inspection,
  git diff --cached --check and gates; only read-only or explicit no-commit work
  is exempt. Bind review to exact commit tips/ranges; repairs add successor commits,
  never amend a reviewed commit.
- Only the integration owner integrates after blocking P0/P1=0. Prefer a merge
  commit for fan-out, record alternative strategy, verify ancestry and absence
  of unrelated commits, validate conflicts as new work, and update readiness
  from the new integrated tip. Retain branches/worktrees while work is active,
  dirty, unaccepted or recoverable, until commit/review/integration evidence
  is durable. Local planned operations do not authorize push, rebase, history
  rewrite, discard, remote mutation or implicit cleanup.
