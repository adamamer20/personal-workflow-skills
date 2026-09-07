## Personal Workflow Skills

### Follow-through and instruction scope

- Carry the user's accepted objective through implementation, required review,
  repair and handoff. A status question or correction steers the same task.
- Reuse authorization already given. Resolve routine implementation choices
  from context and continue useful work before asking about a material decision.
  If a skill causes a pause, cite its exact instruction and explain the missing
  authority; do not treat a procedural preference as a new approval gate.
- Plan the next executable milestone in detail. Record later milestones and
  dependencies at the detail needed now; defer live cutover and deployment
  prerequisites until the milestone that actually needs them.
- Controllers delegate ready independent lanes when useful.
  Keep one mutable owner per surface and one lifecycle controller. Small tasks
  stay direct; delegation is not a mandatory extra layer.
- Select independent review from concrete risk, not milestone size or habit.
  Before dispatch, record the exact unresolved risk and review lens. Review is
  justified for a changed public/persisted contract, ownership or trust boundary,
  security/privacy exposure, migration or irreversible effect, unresolved
  integration uncertainty, prior non-convergence/finding, explicit user request,
  or visual acceptance that needs independent rendered judgment. A small bounded
  reversible change with discriminating tests and none of those triggers closes
  with validation and self-review. Do not launch a reviewer merely because code
  changed, a milestone completed, or a reviewer role exists.
- Reuse successful checks for unchanged bytes and environments. Broaden or
  repeat checks only for a changed dependency, new failure or unresolved risk.
- Keep updates concise and concrete. Put the result first, use plain language,
  and distinguish work delivered, independently accepted and installed.
- Keep global instructions reusable and repository instructions local. Put
  detailed procedures in the relevant skill; avoid copying whole guides into
  every AGENTS.md. Model routing belongs in configuration and this policy,
  while cognitive skill bodies remain model-independent.

### Controller boundary

- The focused reusable baseline is available in `templates/AGENTS.workflow.md`;
  merge its sections into a repository's instructions without replacing local
  project policy.
- The workflow controller is the sole owner of lifecycle, routing, workspaces,
  idempotency, budgets, callbacks, durable state, retries, and recovery.
  Skills provide bounded cognitive work and typed artifacts; they do not build
  a second transport or control plane.
- Controller configuration maps role classes to model settings. Skill bodies
  remain model-independent and refer to shared capsule, evidence, finding,
  review, recovery, result, and visual-contract schemas.
- Native task creation, callback delivery, polling, retry, successor
  scheduling, worktree invention, and ledger mutation belong to the controller,
  never to a cognitive skill.

### Architecture-first milestone graph

- After the implementation architecture map is frozen, planning must attempt
  to factor the program into independently closable vertical milestones with
  disjoint mutable surfaces. Record a milestone dependency DAG and each
  milestone's current readiness before execution.
- Freeze shared schemas, public or persisted contracts, state authority, and
  production entrypoints before fan-out. Every retained serial edge names one
  concrete reason: shared schema, state authority, entrypoint, migration order,
  or acceptance dependency. Minimize the safe critical path without
  maximizing milestone count or inventing fake boundaries.
- Each mutable milestone has one owner and closes independently. Parallelism is
  only between ready milestones with disjoint ownership; read-only scouts and
  distinct review authorities may overlap. Nested swarm/controller
  orchestration is rejected as a default, so a milestone never becomes a
  second lifecycle controller.
- One planning/controller turn may issue multiple native START operations for
  ready milestones when ownership and mutable surfaces are disjoint. The
  singularity rule remains per peer and milestone: each is started exactly
  once, while serial dependencies and shared mutable ownership remain serial.

### Proportional validation

Use the smallest discriminating checks for changed behavior during execution;
at closure run each affected semantic partition and every explicit package,
integration, or promotion gate. Shared contracts, test collection,
configuration, and packaging require the full repository gate. Tests and
bookkeeping support evidence but do not replace observable outcome proof.
Record finding severity separately from `promotion_blocking`; close with no
open promotion-blocking findings, while deferred findings name `defer_to`.

### Semantic density

- Optimize for semantic compression, not abstraction count. Under-abstraction
  is also a defect when a repeated domain decision, relationship, transition,
  or validation remains manually distributed. A useful abstraction makes that
  repeated semantic pattern disappear from call sites.
- Prefer the smallest representation that makes the invariant obvious. New
  vocabulary is more expensive than new lines. Extend an existing domain
  concept before adding another named abstraction.
- A class, protocol, model, enum, wrapper, manager, adapter, service, or
  result/config/state/context type must encode a distinct invariant, domain
  distinction, policy, lifecycle/identity, boundary validation, genuine
  substitution seam, or reusable algorithm. Explicitness, forwarding, test
  convenience, and stylistic cleanliness alone do not justify it.
- Keep strict edges and boring interiors. Parse, validate, and normalize once
  at the earliest honest boundary into one trusted domain representation; do
  not repeat validation or conversions that add no meaning.
- Functions are the default. Classes require persistent state, identity,
  lifecycle, or policy composition. A one-implementation protocol requires a
  real independently owned and replaceable architectural boundary; tests alone
  are not sufficient justification.
- Prefer pure policy functions/reducers, declarative specifications, small
  relationship types, and typed boundary codecs when they compress repeated
  decisions or eliminate downstream defensive code. Do not unify incidental
  syntax or genuinely different mechanics merely to reduce line count.
- Keep behavior beside the invariant, split modules by coherent reason to
  change rather than line count, remove pass-through layers, and collapse
  config/state/result families a domain reader would not distinguish.
- Tests express observable guarantees, transitions, and forbidden transitions.
  Avoid tests that pin helper decomposition, forwarding methods, or field
  assignment.

- For a new substantial or architecturally uncertain program, start with a Astra
  Medium architecture thread and use `$plan-work`. Update the single project-owned
  canonical plan under `docs/reviews/` by default, or at the path defined by
  repository instructions. Write a detailed decision-ready design covering
  boundaries, contracts, ownership, state transitions, failure and recovery,
  migration, non-goals, acceptance, and unresolved decisions before execution.
  Freeze an implementation architecture map too: exact expected paths marked
  create/modify/preserve/remove, one responsibility per module, allowed
  dependency direction, named primary classes/protocols/public or persisted
  types/entrypoints, explicit state/error/serialization boundaries, and a
  justified budget for new durable artifacts. Executors may split private
  helpers within an owned module but must return any new production module,
  public class, registry, runner, schema or entrypoint to planning first.
- If planning-only was requested, stop when the plan is decision-ready. For
  normal execution, the planning thread selects the first executable
  milestone and invokes `$workflow-control` with the canonical plan path and
  exact milestone id. `$workflow-control` is the packaged controller
  entrypoint; the planner does not create a peer or serialize a sidecar
  capsule. `$codex-thread-handoff` is only an explicit legacy compatibility or
  deliberate comparison route: never select it silently, combine it with
  `$workflow-control`, or use it as the normal execution path.
- Substantial milestones use Luna XHigh with `$execute-milestone`; bounded or
  mechanical work follows the same objective route unless the controller
  explicitly escalates.
- After Astra Medium freezes the architecture, Luna XHigh becomes the single
  mutable implementation owner. Luna resolves mechanical details but returns
  material boundary, contract, ownership, security/privacy, cost, destructive-
  behavior, or scope changes to Astra Medium for a bounded plan update.
- Every milestone declares one or more acceptance modes: `objective`, `visual`,
  and `architecture`. Acceptance modes name the evidence needed; they do not by
  themselves require a reviewer task. Route a review only when a recorded risk
  trigger needs independent judgment, and select the smallest sufficient lens.
  When selected, objective code review uses Luna XHigh. Visual-judgment implementation
  defaults to Astra Low; independent visual-quality promotion review uses Astra Low.
  When selected, bounded architecture-conformance review uses Sol Medium. Visual ambiguity,
  non-convergence, or material recovery complexity escalates to Astra Medium;
  a combined review escalates to Astra Medium for significant or ambiguous
  boundaries or when Sol cannot close the question; High is an explicit
  exceptional escalation only. Do not duplicate objective and architecture
  reviews when one focused review can resolve the recorded risk. Use distinct
  authorities only when their evidence and judgments are materially independent.
  When objective and visual modes both apply, preserve both kinds of evidence;
  passing code tests never implies that a rendered result is good. Ordinary recovery defaults to Astra Low. Luna
  is appropriate for visually adjacent work only after the target is frozen and
  the remaining execution is mechanical and objectively verifiable.
- When an owner stops converging, preserve the diff, evidence, findings,
  accepted intent, and remaining gap for the selected recovery route. Astra
  Medium may finish a bounded repair, change implementation strategy, or replan
  while outcome, contracts, security boundary, cost, destructive behavior, and
  scope remain within accepted intent.
  A change of authority or approach is not a terminal condition. Escalate to the
  user only as `NEEDS_DECISION` when intent is genuinely underdetermined or new
  authority is required. Use `EXTERNAL_BLOCKED` only for missing credentials,
  permissions, services, hardware, or other external prerequisites, and
  `FAILED` only when the goal is not reasonably achievable under accepted
  constraints. `CONTINUE_WITH_REPLAN` is internal and nonterminal.
- These defaults are user-owned routing authorization for an explicitly selected
  legacy peer route: when a fresh peer is created, the handoff must pass the exact
  pair `model=gpt-5.6-luna, thinking=xhigh` for execution or independent
  objective/code review, `model=gpt-5.6-sol, thinking=medium` for semantic
  orchestration/decision or architecture conformance, `model=gpt-6-astra,
  thinking=medium` for planning and explicit visual/recovery escalation, with
  `model=gpt-6-astra, thinking=low` for visual implementation and ordinary recovery, and
  `model=gpt-6-astra, thinking=low` for visual-quality review. The pair
  `model=gpt-6-astra, thinking=high` is an explicit exceptional escalation only.
  Escalations are explicit controller decisions; no automatic escalation engine
  is introduced. The most specific applicable user instruction wins. Every Luna task uses `speed=fast` by
  default, including implementation and review, unless a more specific user
  instruction selects another speed. If the native schema advertises `speed`,
  pass `speed=fast`; otherwise rely on the app's configured fast speed and do
  not claim that speed enforcement occurred.
- A plugin installation alone is not authorization to override native task
  settings. If the native schema does not advertise an authorized pair, or
  native creation rejects that pair, stop without retrying, substituting a
  model, or falling back to the configured default. Error text alone does not
  prove that no task was created; the handoff may take one non-waiting
  `list_threads` reconciliation snapshot after an error, but it never retries
  `create_thread`. If no user authorization applies, omit the overrides and
  state that routing was not enforced.
- Native implementation, review and recovery tasks are leaves: no subagents,
  peer dispatch, independent review launches or successor scheduling. Implement,
  test and self-review, or perform the assigned review. The controller owns
  independent reviews, repairs and successors; delivery is not promotion.
  Every terminal outcome returns exactly one `COMPLETION` or
  accurately labelled `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, or `FAILED`
  callback to the exact controller thread/host, with pending acceptance explicit.
  Send only terminal outcomes or material escalations, not routine updates.
  Keep the planning thread unarchived while a callback is owed; it never polls execution.
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
- The program worktree is the sole local integration trunk and stays active
  until the whole plan closes. Before parallel mutable fan-out, its integration
  owner creates one coherent verified local commit containing only authorized
  surfaces and records its SHA as the frozen DAG base. If unrelated dirty bytes
  cannot be separated safely, fan-out waits for a plan-owned separation.
- Managed worktrees for `<parent>/<repo>` live under sibling root
  `<parent>/<repo>.worktrees/` and use a semantic `<program-slug>` or
  `<program-slug>-<lane-slug>` directory with matching `agent/<slug>` branch.
  Never name them from a thread/client id or model. The plan chooses
  `current_checkout`, `existing_worktree`, or `managed_worktree`; handoff does
  not invent topology or replace an unavailable exact workspace with a
  runtime-generated worktree.
- Parallel lane worktrees are semantic children but physical siblings at
  `<parent>/<repo>.worktrees/<program-slug>-<lane-slug>`, created from the
  frozen integration SHA or exact integrated predecessor with branch
  `agent/<program-slug>-<lane-slug>`. Lane workers never edit or integrate the
  trunk. Every mutable milestone commits only its owned surfaces before
  `COMPLETION`, after staged-diff inspection and
  `git diff --cached --check`; read-only planning/review/evidence and explicit
  no-commit contracts are the only exceptions.
- Independent review binds to an exact lane commit or range. Repairs create
  successor commits instead of amending reviewed history. Only the
  controller/integration owner integrates promoted commits after blocking
  P0/P1 findings reach zero, normally with a merge commit for true fan-out;
  cherry-pick or fast-forward needs a recorded reason. Verify ancestry and
  absence of unrelated commits, treat conflicts as new integration work, rerun
  proportional gates, update readiness only after success, and start successors
  from the new integrated tip. Retain lane worktrees/branches until durable
  commit, review, integration and recovery evidence exists.
- Accepted plans authorize required local commits and local integration only;
  push, rebase, history rewrite, discard, remote mutation and implicit cleanup
  remain separately authorized.
- Keep small localized tasks and questions on the direct single-owner path.
- Repository `AGENTS.md` files own project-specific plan paths, gates, and
  exceptions.

### Outcome and evidence priority

Outcome and evidence are ordered and non-negotiable:

1. Accepted observable outcome and user intent.
2. Required safety, integrity, lineage, isolation, recovery, and production/independent-proof guarantees.
3. Explicitly designated hard external constraints (legal/protocol/compatibility/deployment ceilings), with a recorded conflict policy.
4. Secondary proxy and optimization metrics (lines, files, duration, tokens, coverage, complexity, scores, inventories).

A lower-priority item never authorizes weakening a higher-priority item.
Numeric targets are secondary unless the accepted plan marks them hard external constraints. Even a hard cap never silently authorizes removing a required check: use proof-preserving replacement or return a bounded replan or genuine decision when it conflicts. Never report success merely because a proxy is exact.

Repositories must identify non-negotiable proof gates and record the conflict
policy for every hard cap. A proxy target never authorizes deleting a required
check; preserve the observable guarantee or replan.

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
- Give every new or renamed durable path and code/contract identifier a stable,
  descriptive semantic name based on capability, domain, responsibility, or
  observable behavior. This includes files, modules, classes, functions,
  methods, variables, constants, tests, fixtures, CLI commands, public exports,
  evidence records, and generated artifacts. Never couple them to a temporary
  milestone, task, thread, model, or sequence label such as `h6_*`, `s1_*`, or
  `milestone-*`.
- A published historical path or persisted protocol/schema identifier may keep
  a numbered label only when compatibility or provenance requires it. Record
  each exception with its exact identifier, reason, immutable/versioned status,
  and compatibility check. Plan safe reference-preserving renames; never bulk
  rename symbols, imports, packaging paths, commands, links, fixtures, or
  evidence blindly.
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
