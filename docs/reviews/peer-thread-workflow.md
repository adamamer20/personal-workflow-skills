# Plan: Deterministic Codex workflow controller

## Goal

Replace model-owned peer-thread transport and callback bookkeeping with a small,
SDK-first Python controller. Sol remains responsible for planning and material
decisions, Luna executes decision-ready milestones, and independent review gates
promotion. The controller owns lifecycle state, idempotency, worktrees, routing,
recovery, budgets, and durable results so that messages are notifications rather
than the workflow source of truth.

## Current context

- The prior peer-thread program delivered authorized native routing and merged
  plugin lifecycle hooks through `bd626a4` / plugin
  `0.1.4+codex.20260820180447` on `origin/main`.
- The seven-day transport audit supplied by the user found that the hook's exact
  response-envelope assumptions do not match current successful
  `create_thread` and `list_threads` results. Queued creation and reconciliation
  can therefore be misclassified even when a task exists.
- The same audit showed that task messages are not a reliable completion ledger
  and that long-lived planner control loops consume excessive context and
  credits.
- The current primary checkout is intentionally protected: branch
  `agent/thread-handoff-hooks` is six commits behind `origin/main` and contains
  older uncommitted hook work. This plan and its execution start from clean
  branch `agent/python-sdk-controller` at `bd626a4` in an isolated worktree.
- Local Codex CLI is `0.147.0`; `openai_codex` is not installed in the base
  Python environment. Python 3.12 and `uv` are available.
- Official OpenAI documentation describes `openai-codex` as a stable Python
  SDK for Python 3.10+, controlling the local Codex app-server over JSON-RPC and
  shipping a pinned Codex runtime. It documents synchronous/asynchronous thread
  start and run plus per-thread sandbox changes. The exact installed SDK's
  resume, event, structured-output, review, skill-input, and model-effort APIs
  must be proven by a real compatibility sentinel before orchestration depends
  on them.
- The user selected the Python SDK as the primary v1 transport. CLI commands may
  be used as acceptance or diagnostic oracles, but there will not be a second
  CLI-backed production controller.

## Proposed design

Add a typed Python package and `codex-flow` CLI to this repository:

```text
pyproject.toml
src/codex_flow/
  cli.py                 # thin human-facing commands
  config.py              # typed workflow.toml contract
  domain.py              # ids, states, reasons, result contracts
  ledger.py              # SQLite transactions, leases, migrations
  artifacts.py           # atomic readable JSON/JSONL projections
  worktrees.py           # git worktree creation and ownership checks
  controller.py          # deterministic state transitions
  backends/
    codex_sdk.py         # only production Codex transport adapter
tests/
  controller/
workflow.toml            # one routing and limit table
```

Per-repository run state is stored under `.codex-flow/` and excluded from Git:

```text
.codex-flow/
  workflow.db
  runs/<run-id>/
    plan.json
    events.jsonl
    milestones/<milestone-id>/
      capsule.json
      result.json
      review.json
      decision-request.json
      decision.json
  worktrees/<run-id>/<milestone-id>/
```

SQLite is authoritative. JSON and JSONL are atomic, readable projections for
humans, model inputs, and recovery; they never determine state transitions by
themselves.

The initial state machine is deliberately small:

```text
PLANNED -> STARTING -> RUNNING
RUNNING -> NEEDS_DECISION -> RUNNING
RUNNING -> COMPLETED -> REVIEWING -> ACCEPTED
REVIEWING -> REPAIR_REQUIRED -> RUNNING
any non-terminal state -> BLOCKED | FAILED | CANCELLED
```

Every dispatch has logical identity
`<run-id>/<milestone-id>/<role>/<generation>`. A Codex thread id, title, host,
client queue handle, worktree path, or process id is metadata and never workflow
identity.

The SDK adapter owns one app-server connection boundary and normalizes installed
SDK objects/events into controller-owned types. No controller component parses
raw SDK or app-server envelopes outside this adapter. Direct app-server JSON-RPC
is out of scope unless a later explicit milestone replaces the SDK after a
documented capability failure.

Normal execution uses one executor thread, one independent review thread or
native detached review when the installed stable SDK proves that contract, one
repair in the original executor, and one re-review. Ordinary repair resumes the
same executor. A fresh implementation owner is reserved for stable unresolved
findings, a material architecture change, proven owner unavailability, or an
explicit context rollover.

## Contracts and invariants

- The controller is the only mutable owner of workflow state, thread dispatch,
  worktree leases, routing, and recovery.
- One SQLite transaction claims a dispatch identity before any external start;
  a uniqueness constraint prevents duplicate ownership.
- `thread_started` is recorded only from a real SDK thread identity. A transport
  exception before durable identity leaves a recoverable transport state and is
  never reclassified as implementation failure.
- A task's terminal structured result is persisted before any optional
  notification. Notification failure cannot erase or change the result.
- Every state transition validates its allowed predecessor and appends a
  causally ordered event in the same transaction.
- Worktrees are created and leased by the controller from an explicit repository
  root and base commit. Models never choose project ids, branches, or worktree
  paths.
- One milestone has one mutable executor lease. Review is read-only. Repairs
  resume the same executor and lease unless an explicit rollover transition is
  recorded.
- Model and reasoning mappings live only in `workflow.toml`; the controller
  validates them against the runtime before dispatch and fails closed without
  silent substitution.
- Planner, executor, reviewer, and decision outputs use versioned typed
  contracts and JSON Schema at the model boundary. Free-form final prose is
  supplementary, not state authority.
- Reason codes are stable: `decision_required`, `acceptance_ambiguity`,
  `contract_change`, `environment_blocked`, `review_rejected`,
  `context_rollover`, and `transport_failure`.
- `transport_failure` is controller-owned. Model escalation requires a material
  decision or a stable finding/causal class that survives a validated repair.
- No polling loop is introduced. A foreground `run` may consume its own event
  stream; recovery and human inspection use bounded snapshots or explicit
  commands.
- Existing plugin hooks and handoff skills remain available as a migration
  bridge until the SDK controller passes a real medium milestone. They are not
  extended in parallel and are not deleted based only on unit tests.
- The controller never rewrites user Git history, pushes, merges, installs a
  plugin, trusts hooks, or deletes a legacy path without separate authorization
  and the named promotion gate.

## Program scope and non-goals

In scope: the Python package and CLI, stable SDK adapter, SQLite ledger, readable
artifacts, worktree leases, role/routing configuration, structured task
contracts, deterministic tests, prompt-input fixtures, native compatibility
sentinels, documentation, and a bounded migration of the three workflow skills.

Non-goals for v1: a general agent framework; a web UI; remote fleet scheduling;
automatic Git push/merge; provider-agnostic backends; direct app-server protocol
maintenance; recursive planners; subagent orchestration; periodic polling;
automatic model substitution; deletion or disabling of the existing handoff
path before production parity; and automatic Desktop sidebar management.

## Milestone H1 — Prove the stable Python SDK contract

### Outcome

A minimal typed package installs reproducibly and a real, read-only local
sentinel proves which stable `openai-codex` lifecycle capabilities are available
through its pinned runtime. The program either has a supported SDK contract for
the controller or stops with an exact capability gap before workflow logic is
built.

### Mutable ownership

- `pyproject.toml`, `uv.lock`, and `.gitignore` additions for the controller;
- `src/codex_flow/backends/codex_sdk.py` and minimal supporting domain/CLI files;
- focused SDK adapter unit tests and one opt-in real sentinel;
- controller installation and compatibility documentation;
- this canonical plan for terminal evidence only.

### Protected surfaces

- existing plugin hooks, workflow skills, manifests, validators, and audit
  skills;
- the dirty primary checkout and all unrelated worktrees/branches;
- global Codex configuration/authentication, saved tasks, hook trust, remotes,
  and downstream repositories;
- controller state machine, worktree creation, and skill-policy migration,
  which belong to later milestones.

### Implementation boundary

1. Add the smallest distributable Python 3.10+ package using `uv` and pin the
   stable `openai-codex` dependency resolved for this environment.
2. Inspect the installed SDK, then expose only verified operations behind a
   typed adapter: start, run/turn, thread identity, resume, event consumption,
   sandbox, model, reasoning effort, structured output, review, and structured
   skill input. Unsupported optional capabilities must be explicit typed
   capability results, not guessed methods or CLI fallbacks.
3. Add hermetic adapter tests with captured SDK-facing fakes. Keep raw SDK
   objects inside the adapter.
4. Add an opt-in sentinel that starts one read-only local thread in a disposable
   Git repository, records its real id and runtime versions, completes a
   schema-bounded response, resumes the same thread, and captures lifecycle
   events without editing this repository.
5. Exercise a second read-only review or detached-review path only if the stable
   SDK advertises it. Record unsupported status truthfully.
6. Add a manual acceptance capsule for `codex agents`, `codex resume <id>`,
   Desktop `/app`, idle wake/queue behavior, and permission-profile survival.
   These checks are compatibility evidence, not controller transport.

### Acceptance criteria

- `uv sync` installs the stable SDK and its pinned runtime reproducibly on
  Python 3.12 without modifying the user's base Python installation.
- Adapter unit tests cover success, SDK exception before identity, terminal
  failure after identity, unsupported capability, resume, and event ordering.
- The real sentinel records package/runtime versions, an addressable thread id,
  a successful schema-bounded turn, a successful same-thread resume, and the
  observed event sequence.
- The sentinel uses a disposable repository and read-only sandbox and makes no
  change to this repository, global config, hooks, remotes, or existing tasks.
- Model and reasoning effort are either proven through the installed SDK or
  reported as an H1 blocker. No default-model substitution is called success.
- Desktop-openability, idle wake, remote-host behavior, review delivery, skill
  input, and permission-profile survival are each labelled `proven`,
  `unsupported`, `not_exposed`, or `not_run`; none is inferred from thread
  creation alone.

### Validation and promotion gate

- `uv run python -m unittest discover -s tests -v`
- `uv run python scripts/validate.py`
- opt-in real SDK sentinel with retained JSON evidence
- `git diff --check` and complete diff self-review

Promote only when required local start, schema-bounded completion, same-thread
resume, event capture, explicit model/reasoning control, and sandbox isolation
are proven against the stable SDK. A missing required capability returns
`BLOCKED` with the smallest documented decision; it does not silently introduce
CLI or direct app-server transport.

### Review requirement and successor

Self-review the adapter boundary and sentinel side effects. H2 follows only
after H1 promotion.

## Milestone H2 — Implement the durable ledger and state machine

Outcome: SQLite migrations, typed ids/states/reasons, atomic artifact
projections, idempotent dispatch claims, causal events, and crash-recovery
transitions are complete without starting Codex or creating worktrees.

Acceptance: exhaustive transition-table tests, concurrent duplicate-claim test,
transaction rollback tests, artifact-rebuild consistency, corrupt/unsupported
schema failure, and zero external side effects.

Promotion gate: the ledger alone proves one logical owner per dispatch and
truthful separation of transport, execution, review, and terminal outcomes.

Successor: H3.

## Milestone H3 — Add controller-owned worktrees and execution

Outcome: `flow plan`, `flow start`, `flow resume`, `flow status`, and
`flow cancel` connect the H1 adapter to H2 state under explicit repository,
base-commit, mutable-path, protected-path, and validation-command contracts.

Acceptance: controller-created isolated worktree and lease; SDK executor starts
once; crash recovery resumes by durable thread id; duplicate start is denied;
terminal structured result is written before optional notification; transport
failure never consumes an implementation/review cycle.

Promotion gate: one disposable-repository milestone survives an injected
controller crash and resumes to a verified terminal result without duplicate
thread or worktree ownership.

Successor: H4.

## Milestone H4 — Add decisions, review, repair, and limits

Outcome: material decision requests wake a bounded planner turn; completed work
receives independent review; repair resumes the original executor; stable
finding identities and causal classes govern the single escalation boundary;
limits and budgets live in `workflow.toml`.

Acceptance: decision, accepted, repair-required, environment-blocked,
context-rollover, and transport-failure scenarios have deterministic integration
tests; review is read-only; new reviewer scope cannot masquerade as a surviving
finding; thread and compaction limits fail closed.

Promotion gate: one end-to-end disposable milestone exercises review rejection,
same-executor repair, and acceptance with durable evidence and no callback
dependency.

Successor: H5.

## Milestone H5 — Reduce workflow instructions to cognitive roles

Outcome: `plan-work` writes plans/capsules, `execute-milestone` executes one
capsule and writes one result, and a small `workflow-control` skill invokes the
controller. Routing, recovery, callbacks, and state-machine prose are removed
from model-visible skills and owned once by code/configuration.

Acceptance: `codex debug prompt-input` fixtures show one routing policy, the
intended skill only, protected surfaces and acceptance retained, no obsolete
handoff protocol in executor prompts, and an enforced prompt-size budget.

Promotion gate: existing direct workflow remains reachable under an explicit
legacy command while the controller path passes all repository validators and
one medium real milestone.

Successor: H6.

## Milestone H6 — Production pilot and legacy retirement decision

Outcome: run one medium and one large real milestone through the controller,
compare lifecycle correctness and usage against the legacy path, verify local
and remote/Desktop compatibility gates, and make an evidence-backed decision on
disabling hooks and replacing `codex-thread-handoff`.

Acceptance: no duplicate owners; durable terminal evidence survives notification
failure; planner compaction and thread budgets hold; observable repository
outcomes and independent reviews pass; every unrun external gate stays open.

Promotion gate: only proven reachability and required behavioral parity permit
legacy disablement. Deletion is a separate authorized cleanup milestone after
installed-plugin and downstream-pin migration.

## Assumptions

- The user selected an SDK-first controller and authorized implementation of
  the program, but not push, PR, merge, plugin installation/trust, global config
  mutation, downstream updates, or legacy deletion.
- The repository's routing instructions authorize H1 as a substantial,
  objectively verifiable Luna XHigh milestone.
- The stable SDK's pinned runtime may differ from the system `codex` CLI; H1
  records both and treats that separation as intentional unless evidence shows
  an interoperability failure.

## Open findings

- The stable Python SDK's exact resume, streaming event, reasoning-effort,
  structured-output, review, and structured-skill-input surface is not yet
  proven locally.
- Desktop automatic sidebar visibility and remote-host support are compatibility
  gates, not assumptions.
- The supplied audit's underlying archive is not stored in this repository;
  its findings motivate the design but do not substitute for H1 captured
  evidence.
- Existing merged hook build `0.1.4+codex.20260820180447` has not demonstrated
  compatibility with the newer native envelopes described in the audit.

## Current review log

- 2026-08-18 through 2026-08-20: prior program delivered authorized routing,
  retry-free peer recovery instructions, and lifecycle hook build
  `0.1.4+codex.20260820180447`; commits are retained in Git and the merged source
  remains the migration baseline.
- 2026-08-24: user supplied a seven-day transport/lifecycle audit and selected a
  Python-SDK harness. Planning moved to clean branch
  `agent/python-sdk-controller` at `bd626a4`; the stale dirty primary checkout is
  protected. Chosen design is one SDK production backend, SQLite authority,
  controller-owned worktrees, structured results before notifications, and
  capability-gated legacy migration.

## Next execution

Milestone: H1 — prove the stable Python SDK contract.

Dispatch status: queued once as
`client-new-thread:c028534a-299e-4b44-b35e-86a9b7d75401` on native host
`local`; no retry or readiness polling is authorized.

Resolved route: `model=gpt-5.6-luna`, `thinking=xhigh`.

Routing authorization: applicable user-owned `AGENTS.md` substantial-milestone
policy in this task. The native schema advertised the exact pair and accepted
both fields in the queued creation request.

Planning thread: current task; exact callback thread/host route must be supplied
by the native creator when available.

Plan path: `docs/reviews/peer-thread-workflow.md`.

Owned surfaces: H1 mutable ownership only.

Protected surfaces: existing plugin/hook/skill code, dirty primary checkout,
global Codex state, remotes, downstream repositories, and H2+ controller logic.

Acceptance: reproducible stable SDK install; typed verified adapter; real
read-only start, schema-bounded turn, explicit model/reasoning, event capture,
and same-thread resume; capability matrix truthful; decisive validators green;
zero open P0/P1.

Escalate only for: a required stable-SDK capability gap; SDK/runtime
authentication failure after safe diagnostics; unavoidable global-state or
existing-task mutation; a needed architecture/contract change; or a P0/P1
finding that cannot be repaired within H1.

Completion callback: return exactly one terminal `COMPLETION`, `BLOCKED`, or
`FAILED` packet to the planning task using the exact native callback route.
