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

- Start substantial or uncertain work with one canonical plan and a thin,
  production-reachable walking skeleton where feasible.
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

- Optimize for semantic compression, not abstraction count. Under-abstraction
  matters when repeated decisions, relationships, transitions, or validation
  remain distributed. A useful abstraction makes that semantic pattern
  disappear from call sites.
- Prefer the smallest representation that makes the invariant obvious. A new
  named abstraction must encode a distinct invariant, domain distinction,
  policy, lifecycle/identity, boundary validation, genuine substitution seam,
  or reusable algorithm. Explicitness, test convenience, forwarding, or
  stylistic cleanliness alone is not enough.
- Keep strict edges and boring interiors: parse/validate/normalize once at the
  earliest honest boundary, then use one trusted domain representation inward.
  Do not create conversion or wrapper chains unless each step changes meaning.
- Functions are the default. Classes require persistent state, identity,
  lifecycle, or policy composition. A one-implementation protocol requires a
  real independently owned and replaceable boundary; tests alone do not justify
  it.
- Prefer pure policy functions/reducers, declarative specifications, small
  relationship types, and typed boundary codecs when they eliminate repeated
  decisions or downstream defensive code. Do not unify incidental syntax or
  genuinely different mechanics merely to reduce line count.
- Treat semantic vocabulary as a budget. Extend an existing domain concept
  before adding another class, protocol, enum, config/state/result/context type,
  manager, adapter, or service. Keep behavior beside its invariant, split by
  reason to change rather than size, and remove pass-through layers.
- Tests express observable guarantees, transitions, and forbidden transitions;
  they do not pin private helper decomposition, forwarding, or field assignment.

### Execution, review, and recovery

- The milestone owner implements, validates, debugs ordinary failures,
  self-reviews, and repairs valid findings in the assigned workspace.
- A substantial code milestone requires focused validation, self-review and
  at least one independent objective review. Add architecture review when
  ownership, public/persisted contracts, trust boundaries or a named integration
  risk changes. Include the named security boundaries in that same architecture
  review; do not create a separate security review by default. Require visual
  review for visual acceptance. Small local work
  uses proportional checks unless the project names another gate.
- Review is one general workflow; the selected lens changes its evidence, not
  its ownership or transport boundary.
- Every review invocation names exactly one lens (`spec`, `correctness`,
  `standards`, `contract-risk`, `security`, or `visual`). The default is one
  review context; the controller may run parallel contexts only for materially
  distinct, plan-justified lenses with separate evidence.
- Findings record severity separately from promotion impact. P0 is normally
  blocking; P1 blocks when it invalidates the outcome, a protected boundary,
  or safe successor work. A non-blocking finding records its owner and
  hardening destination.
- Recovery diagnoses the causal failure and may finish or replan within the
  accepted outcome, contracts, safety boundaries, cost, and scope. It asks for
  a user decision only when those materially change or external authority is
  missing.
- A milestone closes only when its outcome and safety gates pass and no open
  promotion-blocking P0/P1 finding remains. Final integrated review and
  hardening happen after the end-to-end path works.

### Workspace policy

- Keep one program worktree as the sole local integration trunk until the
  canonical plan closes. Before parallel mutable fan-out, freeze a coherent
  verified local commit containing only authorized surfaces and record its SHA
  as the DAG base. If unrelated dirty baseline bytes are not safely separable,
  block fan-out until the plan defines that separation.
- Reuse the recorded workspace for rollover, repair, review, recovery, model
  changes, and sequential milestones.
- Create a managed workspace only for concurrent mutable ownership, protected
  dirty user state, or an explicitly isolated experiment.
- Managed workspaces live at
  `<repo-parent>/<repo-name>.worktrees/<semantic-lane-name>`; use semantic lane
  names rather than model names, thread IDs, UUIDs, or bare milestone numbers.
- Parallel mutable lanes are semantically children of the program and
  physically sibling Git worktrees named
  `<program-slug>-<lane-slug>`, with branch
  `agent/<program-slug>-<lane-slug>`, created from the frozen integration SHA or
  an exact integrated predecessor. Lane workers never modify or integrate the
  trunk.
- Every mutable milestone creates a coherent owned-surface local commit before
  `COMPLETION`, after staged-diff inspection and
  `git diff --cached --check`. Read-only planning, review/evidence work and an
  explicit no-commit contract are the only exceptions. Reviews bind to exact
  commit tips/ranges; repairs add successor commits rather than amending a
  reviewed commit.
- Only the integration owner merges promoted lane commits after blocking P0/P1
  findings reach zero. Prefer a merge commit for true fan-out; record a reason
  for cherry-pick or fast-forward, verify ancestry and absence of unrelated
  commits, treat conflicts as new integration work, rerun integration gates,
  and update readiness only after success. Successors start from the new
  integrated tip.
- Never remove a workspace while work is active or recoverable, the tree is
  dirty, review is pending, or wanted commits are not integrated.
- Retain lane branches/worktrees until commit, review, integration and recovery
  evidence are durable. Local plan-required commits and integration are
  authorized; push, rebase, history rewrite, discard, remote mutation and
  implicit cleanup are not.
