# Codex Flow repository instructions

## Daily commands

Use the root `Makefile` as the canonical interface:

| Command | Purpose |
| --- | --- |
| `make setup` | Reproducibly install the Python 3.12 environment with `uv` |
| `make check` | Run formatting, lint, strict pytest, plugin validation, compilation, and pre-commit |
| `make test` | Run the full deterministic pytest suite |
| `make test-fast` | Run pytest and stop at the first failure |
| `make format` | Apply Ruff formatting |
| `make lint` | Run Ruff lint checks |

Run the opt-in real SDK sentinel separately. It may start a local Codex
runtime only when explicitly authorized and must write retained evidence:

```bash
make setup
CODEX_FLOW_REAL_SDK=1 uv run codex-flow sdk-compatibility-sentinel \
  --model gpt-5.6-luna --effort medium
```

## Engineering standards

- Prefer clarity over cleverness, one stable type per value, small functions,
  and explicit ownership boundaries.
- Use `uv` for dependency resolution and the checked-in lockfile. The
  supported baseline is Python 3.12 because it is the verified runtime for the
  pinned SDK; do not mechanically raise it to a project-specific baseline.
- Use Ruff, pytest with strict configuration/markers, and pre-commit through
  the root Makefile. Keep tests runnable from the repository root.
- Use Typer for the persistent `codex-flow` CLI, `pathlib` for paths, precise
  PEP 484/695 annotations, typed boundary models, and protocols only at genuine
  architectural boundaries.
- Convert raw SDK payloads at the adapter boundary. Do not use
  `getattr`/`setattr`/`hasattr` for expected interfaces; an interface drift
  must fail visibly in typed code or a focused contract test.
- Use parameterized logging/SQL when those surfaces are introduced. SQLite is
  the intentional later controller ledger; do not add PostgreSQL-only rules.
- Read secrets from environment variables only. Never hard-code credentials,
  paths, hosts, or URLs, and never mutate global Codex configuration,
  authentication, hooks, remotes, or unrelated tasks.

## Semantic density

- Optimize for semantic compression, not abstraction count. Under-abstraction
  is also a defect when a repeated domain decision, relationship, transition,
  or validation remains manually distributed. A useful abstraction makes that
  repeated semantic pattern disappear from call sites.
- Prefer the smallest representation that makes the invariant obvious. New
  vocabulary is more expensive than new lines: extend an existing domain
  concept before adding a class, protocol, model, enum, wrapper, manager,
  result/config/context type, adapter, or service.
- A named abstraction must encode a distinct invariant, domain distinction,
  policy, lifecycle/identity, boundary validation, genuine substitution seam,
  or reusable algorithm. Explicitness, test convenience, forwarding, or “clean
  architecture” alone does not pay that rent.
- Keep strict edges and boring interiors. Parse, validate, and normalize an
  untrusted representation once at the earliest honest boundary, convert it to
  one trusted domain representation, and avoid wrapper chains or repeated
  validation that do not change semantics.
- Functions are the default. Introduce a class for persistent state, identity,
  lifecycle, or policy composition. Do not add a `Protocol` for one production
  implementation unless it is a real independently owned and replaceable
  architectural boundary; tests alone are not sufficient justification.
- Prefer pure policy functions/reducers, declarative specifications, small
  relationship types, and typed boundary codecs when they compress repeated
  decisions or eliminate downstream defensive code. Do not unify incidental
  syntax or genuinely different mechanics merely to reduce line count.
- Put behavior beside the invariant and split modules by coherent reason to
  change, not by line count. Delete pass-through layers and collapse
  config/state/result families when a domain reader would not distinguish them.
- Tests describe externally meaningful guarantees, state transitions, and
  forbidden transitions. Prefer behavior tables and public-boundary tests over
  tests that pin helper decomposition, forwarding methods, or field assignment.

## Program ownership and boundaries

The single active program plan is
`docs/reviews/peer-thread-workflow.md`. Read its current milestone, mutable
ownership, protected surfaces, acceptance, and promotion gate before changing
code. The planning task owns architecture, scope changes, milestone ordering,
the plan, and this instruction file. Each execution task owns exactly one
decision-ready milestone and may not expand into a later milestone.

The `openai-codex` adapter is the only Codex execution transport. Production
workers use the official shared Codex authentication/session store, so the same
SDK thread remains App-visible without making the App lifecycle authority.
Leaf-worker topology does not imply a narrower filesystem sandbox: inherit the
source controller's effective native sandbox and approval authority, bind it in
durable route/capability facts, and revalidate it before thread creation.
Preserve `danger-full-access` when natively granted; permission drift may only
narrow authority. Never hardcode `workspace-write` as a detached-worker
default.
Never force a private `CODEX_HOME`, copy authentication, scrape App state, add a
CLI transport backend, attach to a private Desktop socket, or create a second
App-native worker for the same dispatch. App-native is legacy compatibility,
not the production worker route. SQLite and the detached supervisor are the
single durable workflow authority. Give the SDK adapter, supervisor/IPC,
ledger/schema, worktree manager, controller state machine, routing
configuration, and model-facing contracts one implementation owner at a time;
do not add alternate scaffolding or duplicate production paths.

Worker prompts never own callbacks, peer messages, successor scheduling or
controller supervision. Workers are capability-bound leaves and submit one raw
typed result directly to the harness. The harness atomically closes results and
authorized successors, wakes the source controller immediately on terminal
outcomes, and may arm one durable controller checkpoint (30 minutes by default)
while work remains nonterminal. A checkpoint repeats only after explicit
controller re-arming. Do not use `wait_threads`, repeated status reads or an
open controller turn for supervision; checkpoint and terminal wake-ups must be
harness-owned and App-independent.

The model owns capsule intent, not capsule serialization. In the normal
plan-driven path `workflow-control` receives the canonical plan path and exact
milestone id; a closed harness parser compiles the single typed
`ModelFacingCapsule` and owns JSON projection, identities, routing and durable
digests. Do not ask a model to copy a capsule into a sidecar JSON. Retain the
explicit `--capsule` entrypoint only for low-level integration tests and callers
that already own a typed serialized contract.

Repository workflow skills are cognitive roles around this controller. They may
define intent, acceptance, evidence, and findings, but never own dispatch,
callbacks, routing, retries, successor scheduling, worktree creation, or direct
ledger mutation. Shared schemas and the validator describe these boundaries;
the typed Python contracts remain serialization authority.

Existing plugin hooks, workflow/audit skills, manifests, validators, the
protected primary checkout, remotes, global Codex state, downstream
repositories, and later milestones remain protected unless the canonical plan
explicitly assigns them to the current owner. Do not silently substitute
models, reasoning effort, transports, permissions, or acceptance gates.

## Execution workspace topology

- The program worktree is the sole local integration trunk. It remains active
  until the whole canonical plan closes, and its branch progressively contains
  every promoted milestone. Before mutable fan-out, the integration owner must
  bring that trunk to one coherent verified state, create a local commit that
  contains only authorized surfaces, and record its exact SHA as the frozen DAG
  base. If unrelated dirty baseline bytes cannot be separated safely, fan-out
  remains blocked until the plan records a safe separation.
- A fresh Codex thread or model context does not imply a fresh Git worktree. A
  worktree is an isolated mutable workspace owned by a program or execution
  lane, not by Luna, Astra, a reviewer, or a thread id.
- Reuse the same execution workspace across sequential milestones, context
  rollover, repair, recovery, model changes, and read-only review while mutable
  ownership remains singular. Allocate another worktree only for concurrent
  mutable ownership, protection of pre-existing user changes, or an explicitly
  isolated risky/alternative experiment.
- Managed worktrees for repository `<parent>/<repo>` live only below sibling
  root `<parent>/<repo>.worktrees/`. Name each workspace with a stable semantic
  program slug, or `<program-slug>-<lane-slug>` for a parallel lane; never use a
  task id, client id, model name, or milestone number as the primary identity.
  Use the corresponding `agent/<program-or-lane-slug>` branch by default.
- A parallel mutable lane is semantically a child of the program but uses a
  physical sibling Git worktree at
  `<repo-parent>/<repo-name>.worktrees/<program-slug>-<lane-slug>`. The
  controller creates it from the recorded frozen integration SHA or one exact
  integrated predecessor, with branch `agent/<program-slug>-<lane-slug>`. A
  lane worker never edits, merges into, or otherwise integrates the program
  trunk.
- The canonical plan selects `current_checkout`, `existing_worktree`, or
  `managed_worktree` before task creation and records the exact repository,
  path, branch, base SHA, and lane when applicable. Handoff transports launch
  threads; they do not invent Git topology or silently create another worktree.
- When the native task API cannot address the selected existing workspace,
  fail the handoff closed and preserve the workspace capsule. Never substitute
  a runtime-generated worktree merely because a fresh thread was requested.
- Before `COMPLETION`, every mutable milestone must create at least one coherent
  local commit containing only its owned surfaces after inspecting the staged
  diff and passing `git diff --cached --check` plus its outcome gates. Read-only
  planning, review, evidence work and an explicit no-commit capsule are the only
  exceptions. Independent review and promotion bind to the exact lane commit or
  commit range; a repair creates a successor commit and never amends a reviewed
  commit without invalidating that review.
- Only the controller/integration owner integrates promoted lane commits into
  the program trunk after promotion-blocking P0/P1 findings reach zero. Use an
  explicit traceable Git strategy: a merge commit is the default for true
  fan-out, while cherry-pick or fast-forward requires a recorded reason. Verify
  ancestry and absence of unrelated commits; treat conflict resolution as new
  integration work and rerun proportional integration gates. Update DAG
  readiness only after that integration succeeds, start successors from the new
  integrated tip, and retain lane worktrees/branches until commit, review,
  integration and recovery evidence are durable.
- This policy authorizes the local commits and local integration operations
  required by an accepted program plan. It does not authorize push, rebase,
  history rewrite, discard, remote mutation or implicit cleanup.

## Controller routing and recovery semantics

- For substantial or architecturally uncertain work, Astra Medium owns a distinct
  architecture phase before implementation. It writes a detailed design into
  the single canonical plan: boundaries, contracts, mutable ownership, state
  transitions, failure and recovery behavior, migration, non-goals,
  acceptance, and unresolved decisions. The design must be decision-ready
  enough that the implementation capsule does not ask its executor to invent
  architecture.
- The design also freezes an implementation architecture map before handoff:
  every expected production module/path is labelled `create`, `modify`,
  `preserve` or `remove`; each module has one responsibility and allowed
  dependency direction; primary classes, protocols, persisted/public types and
  entrypoints have names and owners; state, error and serialization boundaries
  are explicit; and every new durable artifact is justified against a bounded
  file/module budget. Luna may split private helpers inside an owned module but
  may not invent another production module, public class, registry, runner,
  schema or entrypoint without a bounded Astra plan update first.
- After freezing that architecture map, planning must attempt to factor the
  program into independently closable vertical milestones with disjoint
  mutable surfaces. Record the milestone dependency DAG and current readiness;
  freeze shared schemas, public or persisted contracts, state authority and
  production entrypoints before dependent fan-out. Every retained serial edge
  names one concrete permitted reason: shared schema, state authority,
  entrypoint, migration order or acceptance dependency. Minimize the safe
  critical path without maximizing milestone count or inventing fake
  boundaries. Each mutable milestone remains single-owner; parallelism is
  only between ready disjoint lanes. Read-only scouts and distinct review
  authorities may overlap, while nested swarm/controller orchestration is
  rejected as a default.
- A single planning/controller turn may issue multiple native START operations
  when each milestone is ready, has one owner, has disjoint mutable surfaces,
  and each peer is started exactly once. This per-milestone rule does not relax
  idempotency: serial dependencies or shared mutable ownership still require
  one START to wait for the relevant predecessor.
- After that design is accepted, Luna XHigh is the single mutable implementation
  owner and normal objective/code reviewer. Return material boundary, public or
  persisted contract, ownership, security/privacy, cost, destructive-behavior,
  or scope changes to Astra Medium for a bounded plan update.
- Model milestones with explicit `objective`, `visual`, and `architecture`
  acceptance modes. These modes name required evidence and do not automatically
  create reviewer tasks. Before dispatching a review, record the concrete
  unresolved risk and smallest sufficient lens. Small bounded reversible changes
  with discriminating tests close through validation and self-review when they do
  not change public/persisted contracts, ownership, trust/security/privacy
  boundaries, migrations or irreversible behavior and have no unresolved
  integration risk, prior finding/non-convergence or explicit review request.
- Visual-judgment implementation and visual-quality promotion review default to
  Astra Low. Semantic orchestration and normal architecture conformance use Sol
  Medium. Visual ambiguity, non-convergence, or material recovery complexity
  escalates to Astra Medium; High is exceptional only. When review is justified,
  use one focused Sol Medium architecture review by default. Escalate a combined
  architecture/security review to Astra Medium only for a significant or
  ambiguous boundary or when Sol cannot close it. Do not duplicate objective and
  architecture reviews when one review resolves the recorded risk; use distinct
  authorities only for materially independent evidence. Passing code tests never
  implies visual acceptance.
- Ordinary recovery defaults to Astra Low. Astra Medium may finish bounded
  recovery or replan while accepted outcome, contracts, security/privacy
  boundary, cost, destructive behavior, and scope remain unchanged.
- `CONTINUE_WITH_REPLAN` is internal and nonterminal. `NEEDS_DECISION` is only
  for genuinely underdetermined user intent or missing user authority.
  `EXTERNAL_BLOCKED` is only for missing credentials, permission, service,
  hardware, or another external prerequisite. `FAILED` means evidence shows the
  goal is not reasonably achievable under accepted constraints.
- Never escalate to the user merely because implementation is difficult, a
  previous model did not converge, or an implementation plan was disproven.

## Descriptive durable naming

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

## Safety and review

- Do not use `eval` or `exec`.
- Make side effects explicit and bounded. Any real SDK sentinel must use a
  disposable Git repository and `Sandbox.read_only`, then compare before/after
  bytes. Archive only the sentinel thread it created after proof.
- Tests, manifests, counts, and status metadata support evidence; they do not
  replace a real observable outcome. A missing required SDK capability is a
  truthful `EXTERNAL_BLOCKED` result with the smallest actionable prerequisite.
- Before committing, inspect the exact staged paths, run `git diff --check`,
  and self-review the complete milestone diff. Do not push, merge, rebase, stash, or
  discard changes without separate authorization.
