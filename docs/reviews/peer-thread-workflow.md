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
- H1 is integrated on the canonical branch as `1c04bf7`. The retained real
  sentinel proves stable `openai-codex==0.147.0` local start, addressable thread
  identity, explicit model and reasoning effort, two schema-bounded turns, 45
  ordered lifecycle events, same-thread resume, and read-only sandbox isolation.
  Review, Desktop, idle wake, remote host, and permission profile are truthfully
  `not_exposed`; structured skill input is `not_run`.

## Proposed design

Add a typed Python package and `codex-flow` CLI to this repository:

```text
AGENTS.md                  # controller-specific engineering instructions
Makefile                   # canonical setup/check/test/format/lint entrypoints
.python-version
.pre-commit-config.yaml
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
- Reuse SprintAct's general Python standards in repository-owned form: `uv`
  dependency/lock management, Ruff formatting and linting, strict root-level
  pytest, Typer for the persistent CLI, `pathlib`, precise PEP 484/695 types,
  typed boundary models, small explicit public interfaces, parameterized
  logging, no indirect access to expected interfaces, and one canonical
  execution document.
- Adapt rather than copy SprintAct policy. Legal-data, frontend, Compose,
  service, PostgreSQL-only, `httpx2`, benchmark, and product-agent rules do not
  apply here. SQLite remains the intentional controller ledger, and the Python
  baseline is selected for this repository and the stable SDK rather than
  inherited mechanically from SprintAct.

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

- root `AGENTS.md`, `Makefile`, `.python-version`, `.pre-commit-config.yaml`,
  `pyproject.toml`, `uv.lock`, and `.gitignore` additions for the controller;
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
   stable `openai-codex` dependency resolved for this environment. Adapt the
   reusable SprintAct tooling into root-owned `setup`, `check`, `test`,
   `test-fast`, `format`, and `lint` Make targets; Ruff/pre-commit/pytest
   configuration; and a repository Python-version file. Do not copy monorepo,
   frontend, Compose, database, service, or legal-data tooling.
2. Add a concise root `AGENTS.md` that makes the canonical plan and controller
   ownership explicit and carries the reusable SprintAct Python rules: clarity,
   stable precise types, `pathlib`, Typer, typed boundary models, no global or
   implicit side effects, no `getattr`/`setattr`/`hasattr` for expected SDK
   interfaces, parameterized logging, parameterized SQL, secrets via
   environment only, root tooling, strict tests, and no weakened gates. State
   explicitly that SQLite is the designed local workflow ledger.
3. Inspect the installed SDK, then expose only verified operations behind a
   typed adapter: start, run/turn, thread identity, resume, event consumption,
   sandbox, model, reasoning effort, structured output, review, and structured
   skill input. Unsupported optional capabilities must be explicit typed
   capability results, not guessed methods or CLI fallbacks.
4. Add hermetic adapter tests with captured SDK-facing fakes. Keep raw SDK
   objects inside the adapter.
5. Add an opt-in sentinel that starts one read-only local thread in a disposable
   Git repository, records its real id and runtime versions, completes a
   schema-bounded response, resumes the same thread, and captures lifecycle
   events without editing this repository.
6. Exercise a second read-only review or detached-review path only if the stable
   SDK advertises it. Record unsupported status truthfully.
7. Add a manual acceptance capsule for `codex agents`, `codex resume <id>`,
   Desktop `/app`, idle wake/queue behavior, and permission-profile survival.
   These checks are compatibility evidence, not controller transport.

### Acceptance criteria

- `uv sync` installs the stable SDK and its pinned runtime reproducibly on
  Python 3.12 without modifying the user's base Python installation.
- `make check` is the canonical aggregate gate and covers Ruff format/lint,
  strict pytest, the existing plugin validator, Python compilation, and
  pre-commit without weakening the pre-existing repository tests.
- Root `AGENTS.md` contains only applicable controller/repository rules and
  explicitly rejects SprintAct-specific PostgreSQL, service, frontend, Compose,
  legal-data, and HTTP-client policy.
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

- `make check`
- `uv run pytest`
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

### Completion evidence

Status: complete and integrated as `1c04bf7`.

Evidence: `make check` passed with 29 tests, existing 10-skill validation,
Ruff, compilation, pre-commit, and clean diff checks. Real evidence is retained
at `docs/reviews/evidence/h1-sdk-sentinel.json`; the disposable repository and
controller worktree were unchanged by both read-only turns, and the sentinel's
owned thread was archived after proof.

## Milestone H2 — Implement the durable ledger and state machine

### Outcome

The controller has one typed, versioned SQLite ledger and deterministic state
machine that can claim logical ownership, record causally ordered transitions,
survive process restart, and rebuild readable artifacts without starting Codex,
creating a worktree, or depending on messages.

### Mutable ownership

- `src/codex_flow/domain.py` for H2 ids, enums, and typed records;
- new `src/codex_flow/ledger.py` and `src/codex_flow/artifacts.py`;
- a narrow internal schema/migration module only if it keeps SQL ownership
  clearer than embedding versioned SQL in `ledger.py`;
- focused H2 tests under `tests/`;
- compatibility documentation only for H2 ledger/storage contracts when needed.

### Protected surfaces

- the H1 SDK adapter, sentinel, retained evidence, CLI behavior, dependency
  versions, root tooling, and repository instructions;
- existing plugin hooks, workflow/audit skills, manifests, validators, and
  marketplace behavior;
- worktree creation, SDK execution orchestration, routing configuration,
  decisions/review/repair, notifications, and model-facing skill migration;
- the canonical plan, dirty primary checkout, global Codex state, remotes,
  downstream repositories, and unrelated worktrees.

### Dependencies

- H1 commit `1c04bf7` and its real sentinel are integrated and green.
- Use Python's standard `sqlite3`; H2 has no need for an ORM, SQLModel,
  PostgreSQL, network dependency, or SDK call.

### Implementation boundary

1. Define validated value types for run, milestone, role, generation, dispatch,
   event, and schema version. Empty or malformed ids fail before SQL.
2. Implement the exact state set `PLANNED`, `STARTING`, `RUNNING`,
   `NEEDS_DECISION`, `COMPLETED`, `REVIEWING`, `REPAIR_REQUIRED`, `ACCEPTED`,
   `BLOCKED`, `FAILED`, and `CANCELLED`, with one explicit allowed-transition
   table. Terminal states have no outgoing transition.
3. Create one versioned SQLite schema owning runs, milestones, dispatch claims,
   current state, and append-only events. Use foreign keys, uniqueness and check
   constraints, parameterized SQL, UTC timestamps, explicit transactions,
   bounded busy timeout, and a migration marker. Reject newer/unknown schemas;
   migrate older owned schemas transactionally.
4. Claim dispatch identity
   `<run-id>/<milestone-id>/<role>/<generation>` in the same transaction that
   records its initial event. Repeating the same logical claim is idempotent and
   returns the established record; conflicting ownership fails without mutation.
5. Perform every state change and its next monotonically ordered event in one
   transaction. Validate expected predecessor so stale writers cannot advance a
   milestone. Roll back both current state and event on any failure.
6. Represent pre-identity transport failure, post-identity execution failure,
   review rejection, and terminal outcomes as distinct typed reasons; none may
   silently consume or increment another category's attempt.
7. Write atomic JSON/JSONL projections under `.codex-flow/runs/<run-id>/` from a
   committed ledger snapshot. Projections are rebuildable and non-authoritative;
   partial replacement or projection failure cannot change SQLite state.
8. Add explicit open/close/reopen behavior and recovery queries for non-terminal
   dispatches. H2 must not decide whether to retry, resume, replace, or contact a
   model; it exposes facts for H3.

### Acceptance criteria

- The complete allowed and forbidden transition matrix is exhaustively tested,
  including terminal immutability and stale expected-state rejection.
- Multiple processes or connections racing for the same dispatch identity
  establish exactly one record; the loser receives the existing idempotent
  result or a typed conflict, never a second owner.
- Fault injection proves transaction rollback leaves neither a state-only nor
  event-only write and preserves monotonically ordered event sequence.
- Close/reopen tests recover identical current state, dispatch identity,
  ordered history, and non-terminal recovery facts.
- Schema tests cover first creation, same-version reopen, transactional supported
  migration, corrupt metadata, foreign-key enforcement, and newer/unsupported
  version rejection.
- Artifact tests rebuild byte-stable canonical JSON/JSONL from the ledger,
  replace files atomically, and prove write/projection failure cannot mutate the
  ledger or become workflow authority.
- Security/safety tests cover parameterized values containing SQL metacharacters,
  repository-local path validation, symlink/path-escape rejection, and no secret,
  prompt body, or raw SDK response persistence in generic ledger fields.
- H2 tests make no SDK call, create no Codex task or Git worktree, and require no
  network, credentials, global config, plugin hook, or external service.

### Validation

- `make check`
- focused H2 tests with a temporary filesystem and multiple SQLite connections
- `git diff --check` and complete staged-diff self-review
- protected-path diff proving H1 adapter/sentinel/evidence and plugin surfaces
  are unchanged

### Promotion gate and review requirement

The implementation candidate passes every acceptance check with zero self-review
P0/P1, then receives one fresh independent Luna XHigh read-only review of the
exact commit. H2 promotes only with zero open P0/P1 on ledger correctness,
concurrency, transactions, migration safety, path safety, and scope adherence.
Ordinary repair returns to the same H2 executor and is re-reviewed. A fresh
implementation context is reserved for explicit context rollover or the
configured repeated-failure circuit breaker.

### Current candidate

Rejected implementation candidate: `6db6146` (parent `fab4cb6`, the executor's
cherry-picked equivalent of planning commit `256233a`). The candidate changes
only `domain.py`, new `ledger.py`, new `artifacts.py`, package exports, and
focused H2 tests. Executor and planning-owner reruns of `make check` are green
with 39 tests. Independent review verdict is `REPAIR_REQUIRED`, P0=0, P1=5,
P2=1; this commit is evidence only and must not be integrated or promoted.

Required repair decisions:

- `H2-INT-001`: remove every runless milestone shorthand. Ledger operations
  require explicit `run_id` plus `milestone_id`; no lexicographic first-match or
  cross-run event aggregation is permitted.
- `H2-SEC-001`: do not attempt semantic secret detection in arbitrary strings.
  Remove arbitrary metadata/event-data and free-text reason persistence from H2,
  or replace it with explicit allowlisted typed fields and identifier-like
  diagnostic codes that cannot carry prompts, raw SDK responses, credentials,
  or general text. H1 exception messages never become durable automatically.
- `H2-SCHEMA-001`: validate the owned schema, not only table/column names. Check
  primary keys, uniqueness, checks, foreign keys, indexes, migration marker and
  version-specific schema identity; reject counterfeit or weakened v2 schemas
  and orphan/duplicate rows before use.
- `H2-SEC-002`: validate the ledger file and every relevant ancestor on every
  open/reopen, rejecting symlink or repository escape before connecting. Tests
  replace the database path after close and must prove reopen fails closed.
- `H2-SEC-003`: artifact creation and replacement use anchored directory file
  descriptors with no-follow semantics so an ancestor swap after validation
  cannot redirect any write outside the repository.
- `H2-API-001`: remove speculative aliases, compatibility names, duplicate
  wrappers, and broad exports. Retain one canonical name per H2 concept and only
  the public surface required by H1 plus the H2 plan.

Required regression expansion: exercise all 121 state pairs through the real
Ledger API; use a genuine constrained v1 fixture and migration rollback test;
cover fault injection after event insertion; cover same-identity and
conflicting-generation process races; cover alternate sensitive keys/values and
H1 exception details; cover counterfeit constraintless schemas, reopen symlink
swaps, and ancestor-swap projection attacks.

Repair candidate: `d32e190` on parent `6db6146`; full H2 review range is
`fab4cb6..d32e190`. Executor and planning-owner reruns of `make check` are green
with 46 tests. The repair remains unpromoted pending fresh independent re-review
of the full range and every stable finding.

### Successor milestone

H3 — add controller-owned worktrees and SDK execution.

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

- Review, Desktop automatic sidebar visibility, idle wake, remote-host support,
  and permission-profile survival are not exposed by the H1 stable SDK surface;
  structured skill input exists but remains unexercised. These are optional or
  later compatibility gates, not inferred capabilities.
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
- 2026-08-24: user requested bringing SprintAct's Python tooling and
  `AGENTS.md` standards into this repository. Selected an adapted import of the
  reusable root toolchain and Python engineering rules, with explicit exclusion
  of SprintAct product/service/database policy and preservation of SQLite as the
  controller ledger.
- 2026-08-24: H1 commit `7f41eaa` returned green and was integrated as
  `1c04bf7`. Verified retained evidence for two real schema-bounded turns on one
  resumed thread, explicit Luna/medium routing, 45 ordered events, unchanged
  read-only repositories, truthful optional-capability labels, and a clean
  executor worktree. Selected H2 as the next executable milestone.
- 2026-08-24: H2 implementation candidate `6db6146` returned with a clean
  worktree and zero self-review P0/P1. Planning verification confirmed the
  five-path scope and reran `make check` successfully with 39 tests. Candidate
  promotion is withheld pending the required independent review of transition
  completeness, concurrency/idempotency, transactional rollback and migration,
  metadata/path safety, and projection authority.
- 2026-08-24: H2R independently rejected `6db6146` with P0=0/P1=5/P2=1.
  Reproduced defects are ambiguous cross-run milestone mutation, alternate-key
  sensitive-text persistence, counterfeit constraintless v2 acceptance,
  symlink bypass on ledger reopen, and ancestor-swap artifact escape; speculative
  aliases are P2. The reviewer independently confirmed the pure 121-pair table,
  single-owner races, stale-writer exclusion, rollback behavior, genuine-v1
  migration feasibility, and absence of external side effects. Repair cycle 1
  returns to the same executor under the decisions above.
- 2026-08-24: H2-R1 repair `d32e190` returned on top of immutable candidate
  `6db6146`, changing the same five H2 paths. It claims all six stable findings
  closed and adds real-ledger 121-pair coverage, constrained-v1 migration and
  rollback, post-event rollback, conflicting-generation races, sensitive-text
  exclusion, counterfeit-schema rejection, reopen symlink rejection, and an
  ancestor-swap projection regression. Planning reran `make check` successfully
  with 46 tests. Promotion remains withheld for fresh full-range re-review.

## Next execution

Milestone: H2R2 — independently re-review full H2 range
`fab4cb6..d32e190` for promotion.

Dispatch status: not yet dispatched.

Resolved route: `model=gpt-5.6-luna`, `thinking=xhigh`.

Routing authorization: applicable user-owned `AGENTS.md` substantial-milestone
policy in this task. The native schema advertised the exact pair and accepted
both fields in the queued creation request.

Planning thread: current task; exact callback thread/host route must be supplied
by the native creator when available.

Plan path: `docs/reviews/peer-thread-workflow.md`.

Owned surfaces: read-only inspection and validation of exact target `d32e190`,
its repair `6db6146..d32e190`, and full H2 range `fab4cb6..d32e190`; no source,
test, plan, artifact, Git-history, or external-state mutation.

Protected surfaces: every repository path and candidate commit; H1
adapter/sentinel/evidence, root tooling/instructions, plugin and skill code,
dirty primary checkout, global Codex state, remotes, downstream repositories,
unrelated worktrees, and H3+ execution/controller logic.

Acceptance: independently reproduce or refute closure of every stable finding;
review the full H2 design beyond supplied tests; verify all 121 ledger edges,
real v1 migration/rollback, event atomicity, process races, durable-field safety,
owned-schema verification, reopen/path race safety, anchored projections, and
minimal public API; enforce `AGENTS.md` direct-interface rules; report
`ACCEPTED` only with zero P0/P1 and no material scope deviation.

Escalate only for: any surviving or new reproducible P0/P1, material H2 plan or
repository-instruction deviation, changed review target, inability to inspect
the exact full range, or a required external side effect. Return findings; do
not repair them in the review task.

Completion callback: return exactly one terminal `COMPLETION`, `BLOCKED`, or
`FAILED` packet to the planning task using the exact native callback route.
