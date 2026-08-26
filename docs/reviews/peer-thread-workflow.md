# Plan: Deterministic Codex workflow controller

## Goal

Replace model-owned peer-thread transport and callback bookkeeping with a small,
controller-first Python harness. Sol remains responsible for planning and
material decisions, Luna executes decision-ready milestones, and independent
review gates promotion. The controller owns lifecycle state, idempotency,
worktrees, routing, recovery, budgets, and durable results so that messages are
notifications rather than the workflow source of truth. SDK-headless execution
must start, recover and complete with the Codex App closed. When open, the App
is only an optional projection for visible native workers and one terminal
wake-up notification; it is never lifecycle, liveness or result authority.

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
```

SQLite is authoritative. JSON and JSONL are atomic, readable projections for
humans, model inputs, and recovery; they never determine state transitions by
themselves.

Execution workspaces are program/lane resources rather than task resources. For
repository `<parent>/<repo>`, a managed workspace lives at
`<parent>/<repo>.worktrees/<program-slug>` or
`<parent>/<repo>.worktrees/<program-slug>-<lane-slug>` with matching
`agent/<slug>` branch. Sequential milestones, context rollover, review, repair,
recovery, and model changes reuse it while mutable ownership remains singular.

The initial state machine is deliberately small:

```text
PLANNED -> STARTING -> RUNNING
RUNNING -> NEEDS_DECISION -> RUNNING
RUNNING -> COMPLETED -> REVIEWING -> ACCEPTED
REVIEWING -> REPAIR_REQUIRED -> RUNNING
any non-terminal state -> BLOCKED | FAILED | CANCELLED
```

`BLOCKED` is the durable H2 spelling for an external prerequisite failure only;
the model/controller result contract exposes it as `EXTERNAL_BLOCKED`.
`NEEDS_DECISION` is reserved for genuinely underdetermined user intent or
missing user authority. `FAILED` means the accepted goal is not reasonably
achievable under its constraints. `CONTINUE_WITH_REPLAN` is a causal internal
event that returns to `RUNNING`, never a terminal state.

Every dispatch has logical identity
`<run-id>/<milestone-id>/<role>/<generation>`. A Codex thread id, title, host,
client queue handle, worktree path, or process id is metadata and never workflow
identity.

The SDK adapter owns the headless bundled-runtime connection boundary and
normalizes installed SDK objects/events into controller-owned types without a
running Codex App. The App-native boundary does not attach to a private or
undocumented Desktop socket: when the App is open it may emit one typed visible
task action and bind the returned thread/host identity into the same claimed
dispatch. Every worker closes directly through the harness-owned result IPC;
the App cannot close or recover a dispatch. No controller component parses raw
SDK or app-server envelopes outside these boundaries. Direct app-server
JSON-RPC remains out of scope.

Every milestone declares one or more acceptance modes: `objective`, `visual`,
and `architecture`. The controller derives implementation and review authorities
per mode rather than from filenames or one generic reviewer role. Normal
objective execution uses one executor plus an independent code reviewer. Visual
quality uses Sol for render-aware implementation and a separate Sol High
qualitative reviewer; a mixed objective/visual milestone must pass both. An
architecture gate uses Sol High. Passing one authority never implies passing
another.

Repair normally resumes the same executor. Demonstrated non-convergence creates
a diagnostic continuation for a recovery authority, which finishes locally,
changes implementation strategy, or replans and continues. It is not an
automatic terminal or user escalation. A fresh owner is also valid for proven
unavailability or explicit context rollover.

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
  root and base commit only when isolation is required. Models never choose
  project ids, branches, or worktree paths. A fresh model thread does not create
  a fresh worktree.
- One milestone has one mutable executor lease. Review is read-only. Repairs
  resume the same executor and lease unless an explicit rollover transition is
  recorded.
- Model and reasoning mappings live only in `workflow.toml`; the controller
  validates them against the runtime before dispatch and fails closed without
  silent substitution.
- Planner, executor, code-reviewer, visual-reviewer, architecture-reviewer,
  recovery, and decision outputs use versioned typed contracts and JSON Schema
  at the model boundary. Free-form final prose is supplementary, not state
  authority.
- Acceptance modes, implementation authority, and each required promotion
  authority are durable milestone inputs. The controller may require multiple
  independent reviews for distinct modes without treating them as repeated
  review waves.
- The base reason codes are `decision_required`, `acceptance_ambiguity`,
  `contract_change`, `environment_blocked`, `review_rejected`,
  `context_rollover`, and `transport_failure`. H4 adds typed
  `continue_with_replan` and `architecture_replan` recovery outcomes without
  turning them into terminal failure categories.
- `transport_failure` is controller-owned. A surviving finding or disproven plan
  triggers diagnosis/replan; only underdetermined intent or missing user
  authority becomes `NEEDS_DECISION`.
- No polling loop is introduced. The detached supervisor blocks on local IPC
  and explicit deadlines, takes one recovery snapshot at startup, and receives
  raw terminal results directly from workers. Human inspection uses bounded
  status snapshots or explicit commands; normal execution never calls
  `wait_threads`, `read_thread` or a source-controller model.
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

In scope: the Python package and CLI, stable SDK adapter, optional App-native
projection boundary, detached repository supervisor, durable SQLite queue,
capability-bound local worker-result IPC, readable artifacts, worktree leases,
role/routing configuration, structured task contracts, deterministic tests,
prompt-input fixtures, native compatibility sentinels, packaging/service
management, documentation, and a bounded migration of the three workflow
skills.

Non-goals for v1: a general agent framework; a custom web UI; remote fleet
scheduling; automatic Git push/merge; provider-agnostic backends; direct
app-server protocol maintenance or private Desktop socket discovery; recursive
planners; subagent orchestration; periodic polling; automatic model
substitution; and deletion or disabling of the existing handoff path before
production parity. The native Codex task list/sidebar is the App-native UI;
building a second dashboard is not required for v1.

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
`EXTERNAL_BLOCKED` with the smallest actionable prerequisite; it does not
silently introduce CLI or direct app-server transport.

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
- The local filesystem threat model treats processes running as the controller's
  Unix UID as one trusted authority. Path integrity rejects symlinks, path
  substitution, pre-existing/mid-boundary hardlinks, changed inode identity, and
  non-cooperative state observed at every owned transaction boundary. It does
  not claim atomic isolation from a malicious same-UID process that can invoke
  `link(2)` or inspect/open the controller's files between kernel syscalls; POSIX
  regular files provide no such boundary. Cross-user access remains governed by
  directory/file permissions. This is a local workflow-integrity contract, not
  a same-account hostile-process sandbox.

### Validation

- `make check`
- focused H2 tests with a temporary filesystem and multiple SQLite connections
- `git diff --check` and complete staged-diff self-review
- protected-path diff proving H1 adapter/sentinel/evidence and plugin surfaces
  are unchanged

### Promotion gate and review requirement

The implementation candidate passes every acceptance check with zero self-review
P0/P1, then receives one fresh independent read-only integrity review of the
exact commit. H2 promotes only with zero open P0/P1 on ledger correctness,
concurrency, transactions, migration safety, path safety, and scope adherence.
A rejected candidate is diagnosed and repaired against concrete findings; model
or context changes improve the approach but never form an attempt-count terminal
gate. User intervention is required only for a genuine material decision or new
authority, not because a repair is difficult.

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

Rejected repair candidate: `d32e190` on parent `6db6146`; full H2 review range
is `fab4cb6..d32e190`. Executor, planning-owner, and independent-review reruns
of `make check` are green with 46 tests, but the second independent review found
five P1 integrity escapes and two P2 boundary defects. This commit is evidence
only and must not be integrated or promoted.

Stable closure from the second review: `H2-INT-001`, `H2-SEC-001`, and
`H2-SEC-003` are closed on the tested Linux path; the speculative aliases and
wrappers from `H2-API-001` are removed. `H2-SCHEMA-001` and `H2-SEC-002` remain
open, and the review added `H2-INT-002`, `H2-INT-003`, `H2-API-002`,
`H2-API-003`, and `H2-API-004`.

Required continuation decisions:

- Make the transition contract immutable to callers and keep Ledger behavior
  bound to that canonical immutable representation. No exported mutable mapping
  may change terminal immutability or any allowed edge at runtime.
- Reject non-string identifiers before SQL. Constructors validate the supplied
  type and value; they never stringify arbitrary objects.
- Validate event-kind/dispatch relationships before mutation. A dispatch-claim
  event requires its matching durable dispatch, and normal state-transition
  events cannot carry a dispatch id. Every successful public write must leave a
  ledger that immediately closes and reopens successfully.
- Remove public mutable access to the raw SQLite connection. The connection is
  private; tests and diagnostics use only narrow typed/read-only queries.
- Verify the canonical owned schema structurally from exact SQLite metadata and
  PRAGMAs or a recomputed canonical fingerprint, not SQL substring matching.
  Fresh and migrated v2 databases must converge on the same owned DDL identity.
- Replay every milestone history on open: sequences are contiguous, every
  `from_state` equals the replay state, every edge is allowed, dispatch-claim
  events correspond exactly to dispatch rows, and the final replay state equals
  `milestones.current_state`. Reject orphaned, duplicate, discontinuous, or
  otherwise non-causal authority before use.
- Pin the database inode before SQLite connects and anchor its parent directory
  with no-follow file descriptors. Validate file identity with `fstat` and use
  the pinned descriptor path for SQLite so a hardlink substitution between path
  validation and connect cannot redirect writes. Exercise journal, locking,
  close, failed-open cleanup, and reopen behavior on the supported Linux runtime.
- Use direct `os.O_DIRECTORY` access on the supported Linux runtime; do not use
  an indirect fallback for this security-critical interface.

Repeated Luna non-convergence on the same schema/path-integrity classes produced
a diagnostic continuation for a fresh Sol Medium recovery owner, without
weakening H2 acceptance or opening H3.

Rejected Sol continuation candidate: `a01596d` on parent `d32e190`; full H2
review range is `fab4cb6..a01596d`. It changes only `domain.py`, `ledger.py`,
`artifacts.py`, and focused H2 tests. The executor and planning owner both ran
`make check` successfully with 53 tests; focused H2 has 24 passing tests, diff
checks are clean, and the executor worktree is clean. Independent Sol High
review denied promotion with P0=0/P1=3/P2=2. This commit remains evidence only
and must not be integrated or promoted.

Stable closure from the Sol High review: `H2-INT-001`, `H2-SEC-001`,
`H2-SEC-003`, `H2-INT-003`, `H2-API-002`, `H2-API-003`, and `H2-API-004` are
closed. `H2-SCHEMA-001`, `H2-SEC-002`, and `H2-INT-002` remain P1;
`H2-API-001` remains partially open at P2 and new `H2-SEC-004` is P2.

Required Sol repair-cycle-2 decisions:

- Canonical schema identity covers the complete non-internal `sqlite_master`
  object inventory, including object type, name, owning table, and exact owned
  SQL where present. Reject every extra non-`sqlite_*` table, index, view, or
  trigger before authority; continue validating internal autoindexes through
  the exact constraint and PRAGMA checks. Fresh and migrated v2 inventories
  must remain identical.
- Audit every public mutator so a reported-successful create, claim, or
  transition re-reads and validates its exact durable row/state/event inside the
  transaction before commit. A trigger or other noncanonical object cannot
  erase or rewrite a successful result silently, even if introduced after open.
- Treat one successful first open as binding the Ledger instance to that exact
  database device/inode. Preserve the expected identity across `close()` and
  require it on `open()`/`reopen()`; never silently adopt another valid v2 file.
  Reject `st_nlink != 1` at pinning and around every write transaction, and
  revalidate the descriptor, directory entry, expected identity, and link count
  before commit so a hardlink cannot export writes outside the repository.
- Distinguish opening an existing file from atomically creating a missing file.
  Track ownership of a newly created inode. If first open fails, remove only
  that still-matching, single-link, empty/uncommitted owned inode through the
  pinned parent descriptor; never delete a substituted path or a pre-existing
  file. Preserve concurrent-first-opener behavior and close all descriptors.
- Make the transition predicate itself the single canonical policy in code,
  with no mutable/rebindable backing mapping or class attribute. Ledger calls
  that predicate directly. Any exported transition mapping is a derived
  read-only diagnostic whose mutation or rebinding cannot affect validation;
  remove `StateMachine` if it provides no distinct required contract.
- Restore root `codex_flow.__init__` exports to the exact H1 public baseline.
  H2 remains available through explicit `codex_flow.domain`,
  `codex_flow.ledger`, and `codex_flow.artifacts` modules; do not re-export H2
  records, diagnostics, policy constants, projector internals, or exception
  taxonomy from the package root without a real caller.

Ordinary bounded repair resumes the same Sol Medium execution task and worktree.
The next independent review either promotes H2 or returns concrete evidence for
diagnosis and continued repair/replan. A failed check does not itself terminate
the milestone, choose a model ladder, or require user intervention.

### Successor milestone

H3 — add controller-owned worktrees and SDK execution.

## Milestone H3 — Add controller-owned worktrees and execution

### Outcome and acceptance modes

`codex-flow plan`, `start`, `resume`, `status`, and `cancel` form the first
agent-usable vertical slice. The controller validates and leases the selected
current checkout or semantic managed Git worktree from an explicit
repository/base commit, starts or resumes exactly one SDK executor under
the effective native Codex permission/profile, runs explicit validation, and
persists a typed terminal result before any projection or notification.

Acceptance modes: `objective`, `architecture`.

### Mutable ownership

- `src/codex_flow/worktrees.py`, `controller.py`, and narrow typed additions to
  `domain.py`;
- H3 schema migration and typed ledger methods in `ledger.py` for repository,
  worktree lease, SDK thread identity, turn identity, capsule, validation, and
  terminal result facts;
- `backends/codex_sdk.py` and a narrow typed native-profile projection to
  support permission/profile inheritance and fresh-process resume through the
  proven stable high-level SDK;
- `cli.py`, artifact projections, focused tests, `workflow.toml` only if H3
  requires a minimal single-route default, and retained H3 sentinel evidence.

### Protected surfaces and non-goals

- H1 transport remains the only Codex backend; no CLI/app-server fallback;
- H2 history and schema migrations are forward-only and canonical;
- plugin/skill migration, multi-authority review, decisions, repair policy,
  automatic notifications, remote hosts, push/merge, and legacy retirement are
  H4+;
- models never select repository roots, base refs, worktree paths, mutable or
  protected paths, validation commands, model ids, or reasoning effort. Native
  Codex owns the default permission authority; a capsule may only narrow it.

### Contracts and failure behavior

1. A versioned `ExecutionCapsule` contains run/milestone identity, workspace
   mode (`current_checkout`, `existing_worktree`, or `managed_worktree`),
   absolute repository/workspace roots, semantic program/lane slug, branch,
   resolved full base SHA, normalized mutable/protected path sets, explicit
   validation argv/timeout, executor model/effort, prompt input, and a strict
   structured-output schema, and permission mode (`inherit_native` by default,
   or explicit `read_only`). Prompt bodies remain in owned capsule artifacts,
   not generic ledger metadata/events.
2. `WorktreeManager` uses argument-vector Git subprocesses, validates the
   repository and ancestors without symlinks, resolves the supplied base before
   mutation, and leases the selected workspace. When a new managed worktree is
   explicitly required, it creates only sibling
   `<repo-parent>/<repo-name>.worktrees/<program-or-lane-slug>` with matching
   `agent/<slug>` branch. Same-contract acquisition is idempotent; sequential
   milestones, rollover, review, repair, recovery, and model changes reuse it;
   conflicts cover changed repository/base/path/branch/lane/owner facts.
3. `plan` validates and durably records the capsule without external side
   effects. `start` claims the dispatch, creates/records the worktree, starts one
   SDK thread, persists its real identity immediately, executes the turn, runs
   validation, and persists the terminal result in that causal order.
4. A fresh controller process can `resume` only from durable facts. When thread
   identity exists it re-resolves native configuration and atomically persists
   the meet of prior effective permission authority and the current native
   authority before adapter creation or SDK `thread_resume`. Native tightening
   is inherited, native broadening retains the prior restriction, and changed
   provider/routing/catalog/discovery compatibility fails closed under a typed
   reason distinct from permission change. It then uses the same worktree/route.
   `start` or `resume` never creates a second dispatch, lease, worktree, or thread.
5. The unavoidable crash window after an external SDK start but before durable
   identity fails closed as an explicit uncertain pre-identity transport fact;
   automatic recovery never starts another thread. The promotion sentinel
   injects its resumable crash only after identity is committed.
6. Validation runs from explicit argv with bounded timeout in the leased
   worktree. Result status and validation observation commit before artifact
   projection. Projection/notification failure cannot change the authoritative
   result.
7. `cancel` is idempotent, terminal, and never deletes a worktree, archives a
   task, or discards Git changes automatically. `status` is read-only and emits
   stable JSON plus concise human output.
8. The SDK child uses the normal bundled app-server launch path and a typed,
   fail-closed projection of the active native Codex configuration. Provider,
   model catalog, MCP, skill, plugin, memory, project, hook, shell-environment,
   and permission semantics are preserved; model/effort/cwd remain capsule
   inputs. Mutable sessions/databases/logs use a private `CODEX_HOME`. Provider
   authentication remains environment-reference based. Literal MCP headers and
   every stdio environment entry are converted to process-only references; no
   raw value is projected or persisted.
9. `inherit_native` supplies no SDK approval or sandbox override while current
   native authority equals the execution's durable authority. A `read_only`
   capsule supplies only the stricter read-only sandbox override. Schema v8
   retains schema v7's causal baseline and schema v6's immutable native
   compatibility identity separately from sanitized effective permission facts
   and digest, and adds accepted terminal workspace authority for successor
   preflight. Before resume, the permission meet is committed atomically; an
   SDK override is supplied only when required to retain a prior stricter
   sandbox/approval authority. The lattice has no broadening value and fails
   closed if monotonic restriction cannot be established.
10. Worktree selection, mutable/protected path checks, Git-authority snapshots,
    and validation are ownership and evidence controls, not an OS containment
    boundary. H3 does not claim to contain hostile same-UID native code beyond
    the effective native Codex permissions. Repository-external effects are
    governed by that native authority and are not unconditionally reclassified
    as controller failure.

### Acceptance and validation

- exhaustive hermetic tests cover capsule/path/semantic-slug validation,
  current-checkout selection, sibling-root managed-worktree ownership, workspace
  reuse, same-contract idempotency, conflicting parallel lanes, dirty/protected path checks,
  subprocess failure, stale writers, duplicate `start`, result-before-
  projection ordering, cancel idempotency, and every crash injection boundary;
- fresh-process adapter tests prove resume initializes a new SDK client and
  rejects identity change without a CLI/direct-RPC fallback;
- deterministic parity tests prove a blank private home selects built-in
  `openai`, the projected profile selects exact `codex-lb` provider facts,
  unrestricted and restricted native profiles are inherited on the next
  execution, no broadening mode exists, global config bytes are unchanged, and
  explicit read-only mode is the only SDK permission override;
- crash/concurrency regressions prove `danger-full-access` to `read-only`
  resumes exactly once under read-only after durable rebind,
  `read-only` to `danger-full-access` remains read-only, incompatible native
  compatibility changes make no adapter/external call, and capsule read-only
  remains monotonic;
- no-follow runtime-home regressions cover existing and ancestor symlinks,
  non-directory/device substitution, clean private creation, and unchanged
  external target content/metadata; cancelled/terminal executions reject every
  public turn/checkpoint mutator after close/reopen;
- recursive discovery regressions reject root, parent, nested, multi-node, and
  dangling cycles on all three native surfaces before adapter construction;
- managed and existing worktree regressions cover empty `.codex-flow` and
  out-of-scope directory creation/removal/rename, while mutable-root empty
  topology remains usable under deterministic scan bounds;
- close/reopen successor regressions reject predecessor output content,
  deletion, type, symlink, hardlink, empty-directory, and Git-authority drift
  before baseline/lease/adapter authority;
- lexical, relative, trailing-component, and symlink workspace aliases share
  one physical lease and thread identity; noncanonical legacy keys fail closed;
- structural config regressions scan alternate header and secret values across
  private runtime files, ledger/artifacts, exceptions, and repository bytes,
  while the active native profile still loads through the pinned runtime;
- a disposable real Git repository sentinel plans and starts a bounded editing
  milestone, injects a controller stop after durable SDK identity, resumes in a
  fresh process, runs validation, and reaches one terminal structured result;
- retained evidence records one dispatch, one worktree lease/path, one SDK
  thread identity, a semantic program/lane workspace reused across resume,
  ordered turns/events, before/after protected-path hashes,
  validation output digest, final Git diff/commit facts, sanitized effective
  provider/profile/permission facts and digest, unchanged global native config,
  and no duplicate owner;
- `make check`, focused H3 tests, `git diff --check`, complete diff self-review,
  and zero open P0/P1.

### Promotion gate and successor

Promote only when the real disposable sentinel survives the injected post-
identity crash and finishes through `codex-flow resume` with no duplicate thread
or worktree and a durable terminal result. Deterministic boundary fault injection,
not the retained real sentinel alone, proves result-before-projection ordering.
H4 follows.

Successor: H4.

### Completion evidence

Status: H3 implementation, deterministic validation, and the corrected real
sentinel are complete. On 2026-08-25 the user accepted exact candidate
`544c1c8` as pilot-ready and explicitly unblocked H4 rather than continuing an
open-ended adversarial review/repair loop. This is a successor-safety decision,
not a claim that H3 is finally production-promoted or defect-free: concrete
findings reached by H4 remain classified by severity and promotion impact, and
the integrated H6 system gate retains cross-cutting hardening authority.
The user superseded the mandatory Bubblewrap containment contract: H3 now
inherits the same native Codex permission authority and automatically follows
later native restrictions. The bounded Git-authority snapshot remains required
for HEAD/branch, refs, reflogs, index, repository/worktree configuration, and
stable operation metadata, but it is evidence/contract enforcement rather than
an OS sandbox claim. Schema v8 retains schema v7 per-milestone baselines and
causal turn-start authority, and adds accepted terminal HEAD/content facts for
predecessor verification while preserving schema v6 monotonic permission and
Git-authority evidence. The single corrected real SDK run reached
the first SDK turn and injected post-turn crash boundary, then fresh-process
resume failed because the native runtime had legitimately added private config
state and reprojection treated that private change as source drift. The
launcher now atomically restores the validated projection for each fresh SDK
process, with a regression covering runtime-added private config. The user then
authorized one new run for exact commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`.
That single Python-SDK/bundled-app-server sentinel passed through `codex-lb`
with inherited native `danger-full-access`/`never` authority, one dispatch and
thread identity, injected post-turn crash, fresh-process resume, an allowed
workspace edit, equal Git-authority digests, and unchanged source Codex config
bytes. Retained evidence records the sanitized provider/profile/permission
facts and digests without secrets. External-write denial is not an H3 gate.
The objective review of `3014c568824e3f5d1474766f6b17f5b1dddddf49`
returned P0=0/P1=2/P2=2. The repair makes native permission changes monotonic
across resume, moves runtime-home setup to descriptor-anchored no-follow
creation, preserves terminal cancellation immutability, and updates the schema
and compatibility truth. The retained real sentinel was not rerun: the repair
does not change its proven SDK/provider/model route, while permission tightening
and unsafe-path rejection are deterministic gates and global native config is
protected.

## Milestone H4 — Add decisions, review, repair, and limits

Outcome: each milestone carries explicit objective/visual/architecture
acceptance modes; routing produces separate executor, code-reviewer,
visual-reviewer, architecture-reviewer, and recovery authorities as required.
Material decision requests wake a bounded planner turn; repair resumes the
original executor when useful; stable finding identities and causal classes
drive diagnosis rather than attempt counts; limits and budgets live in
`workflow.toml`.

H4 is delivered in two independently useful slices. H4-A is the early walking
skeleton: one objective milestone is executed, independently reviewed,
rejected, repaired by the same durable owner, and accepted through the
production SDK controller without callback authority. H4-B then adds distinct
visual and architecture authorities, bounded planner decisions, and the
complete H4 promotion scenario. H4-A must change observable repository behavior
before H4-B deepens the policy surface.

Acceptance: completed, continue-with-replan, needs-decision, repair-required,
external-blocked, context-rollover, and transport-failure scenarios have typed
deterministic integration tests. Replanning continues automatically when intent,
public/persisted contracts, security/privacy boundary, material cost,
destructive behavior, and scope are unchanged. Review is read-only; mixed
acceptance modes require every distinct authority; new reviewer scope cannot
masquerade as a surviving finding; thread and compaction limits fail closed.
Every finding records severity separately from `promotion_blocking`, a concrete
reason, and `defer_to` when non-blocking. A gate blocks only dependent successor
work; isolated non-propagating findings remain visible for the system gate.

Promotion gate: one end-to-end disposable milestone exercises objective and
visual review, review rejection, automatic architecture replan, repair, and
acceptance with durable evidence and no callback dependency. Separate scenarios
prove `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, and `FAILED` are mutually distinct
and that implementation difficulty alone produces none of them.

Mutable ownership: `workflow.toml`, `config.py`, H4 domain/result schemas,
controller/ledger/artifact extensions, role prompt templates, focused tests, and
retained H4 evidence. H3 worktree and SDK transports are extended, never
duplicated. Plugin skills remain protected until H5.

Implementation boundary:

- `workflow.toml` owns model/effort mappings for planner, executor,
  code-reviewer, visual-reviewer, architecture-reviewer, and recovery roles plus
  turn, repair, compaction, validation, and wall-clock limits. Runtime validation
  rejects unavailable routes without substitution.
- Capsules declare required acceptance modes. Objective review uses a fresh
  read-only Luna reviewer; visual-quality review uses a fresh read-only Sol High
  reviewer over fixed rendered evidence; architecture review and recovery
  diagnosis use Sol High. Multiple modes require all distinct authorities.
- Typed reviewer output carries stable finding id, causal class, severity,
  promotion impact and reason, evidence, acceptance criterion, optional
  `defer_to`, and whether it survives the exact prior repair. New scope cannot
  impersonate a surviving finding.
- A valid rejection resumes the executor/lease when useful. Non-convergence
  creates a diagnostic continuation. Recovery returns finish-local,
  change-strategy, `CONTINUE_WITH_REPLAN`, `NEEDS_DECISION`,
  `EXTERNAL_BLOCKED`, or `FAILED`; only the last three are terminal/user-visible
  outcomes, with the semantics frozen above.
- A bounded planner turn answers `NEEDS_DECISION` only when existing intent can
  resolve it; genuinely underdetermined product/contract/authority decisions are
  persisted for the user. Notification remains non-authoritative.
- Limits fail closed before another external turn, preserve the last durable
  checkpoint, and never silently change model, effort, sandbox, or acceptance.

H4-A implementation boundary and gate:

- add canonical role/limit configuration plus typed objective-review finding,
  repair, recovery, decision, budget, and lifecycle contracts to the existing
  controller/ledger/artifact path; do not duplicate H3 transport, workspace, or
  state authority;
- implement objective execution -> read-only review -> rejection -> same-owner
  repair -> fresh read-only re-review -> acceptance, with stable finding
  identity, explicit promotion impact, causal event history, and durable result
  before projections;
- exercise `CONTINUE_WITH_REPLAN`, `NEEDS_DECISION`, `EXTERNAL_BLOCKED`,
  `FAILED`, context rollover, transport failure, stale review, and exhausted
  limits hermetically, keeping their semantics mutually distinct;
- run one bounded real disposable objective pilot through the Python SDK and
  `codex-lb`, producing an observable repository edit, one deliberate review
  rejection, one repair, final acceptance, and retained sanitized evidence;
- close H4-A with focused/full gates, self-review, and zero findings that are
  promotion-blocking for H4-B. Do not require another broad H3 review or label
  isolated hardening as blocking merely because it is severe.

H4-B implementation boundary and gate:

- derive distinct executor, objective/code-review, visual-review,
  architecture-review, recovery, and decision authorities from capsule
  acceptance modes plus `workflow.toml`; reject unavailable or aliased required
  authorities without model, effort, permission, provider, or transport
  substitution;
- keep objective review on the accepted H4-A lifecycle and add fixed rendered
  evidence plus an independent visual authority, an independent architecture
  authority, and bounded recovery/planner decisions on the same controller,
  SQLite authority, lease, checkpoint, finding, and projection path;
- require every declared mode to accept. Preserve stable finding and repair
  lineage across authority changes, distinguish new scope from surviving
  findings, and retain a non-blocking finding without blocking an unrelated
  successor gate;
- make a rejected architecture finding produce a durable bounded
  `CONTINUE_WITH_REPLAN`, then execute the accepted revised strategy without
  changing user intent, public/persisted contracts, security/privacy boundary,
  material cost, destructive authority, or milestone scope. Only genuinely
  underdetermined or externally blocked cases may leave the autonomous path;
- run one bounded disposable real multi-authority pilot through the Python SDK
  and `codex-lb`: objective execution, independent objective and visual review,
  an architecture rejection, bounded recovery/replan, same-workspace repair,
  fresh distinct-authority re-review, and final acceptance over an observable
  repository edit and fixed rendered evidence. Retain sanitized causal facts;
- close H4 with focused/full gates, clean worktree, truthful compatibility and
  retained evidence, and zero findings that are promotion-blocking for H5.
  H4-B does not package plugins, simplify skills, run H6 medium/large pilots,
  retire legacy behavior, or reopen a broad H3/H4-A audit.

Validation includes hermetic scenario tests for every role/mode/result,
surviving-versus-new findings, recovery replans, budget exhaustion, stale
reviewers, notification failure, and result ordering, plus one disposable real
SDK milestone that is rejected, repaired, separately code/visual reviewed, and
accepted with durable evidence.

### Completion evidence

Status: H4 is complete and accepted at implementation commit `662d006`.
H4-A established the objective rejection/repair/re-review walking skeleton;
H4-B derives distinct executor, objective/code, visual, architecture, recovery,
and decision authorities from capsule modes and `workflow.toml`, requires every
declared authority to accept, and fails closed on missing or aliased roles.

The retained real multi-authority pilot at
`docs/reviews/evidence/h4-b-multi-authority-pilot.json` used the Python SDK and
`codex-lb` on one reused thread for two turns. It made an observable authorized
edit, fixed rendered evidence by digest, passed independent objective and
visual review, received an architecture rejection, durably recorded a
boundary-preserving `CONTINUE_WITH_REPLAN`, repaired in the same workspace, and
reached `ACCEPTED` after fresh distinct-authority re-review. Evidence contains
only sanitized hashes, counts, route, lifecycle kinds, and terminal status.
Planning independently reran the 11 focused H4 tests and full `make check` with
286 tests plus formatting, lint, cross-skill validation, compileall, and
pre-commit; the exact worktree is clean and H4-A evidence is unchanged. No
finding is known to block H5. This closes H4 without claiming H5 packaging or
H6 integrated-pilot/retirement work.

Successor: H5.

## Milestone H5 — Reduce workflow instructions to cognitive roles

Outcome: `plan-work` writes plans/capsules, `execute-milestone` executes one
capsule and writes one result, and a small `workflow-control` skill invokes the
controller. Routing, recovery, callbacks, and state-machine prose are removed
from model-visible skills and owned once by code/configuration.

Acceptance: `codex debug prompt-input` fixtures show one routing policy, explicit
acceptance modes, distinct code/visual/architecture authorities, completion-
biased recovery, the intended skill only, protected surfaces and acceptance
retained, no obsolete handoff protocol in executor prompts, and an enforced
prompt-size budget.

Promotion gate: existing direct workflow remains reachable under an explicit
legacy command while the controller path passes all repository validators and
one medium real milestone.

Mutable ownership: the three workflow skills, a new minimal `workflow-control`
skill, plugin manifest/version/marketplace metadata as required, prompt-input
fixtures, CLI help/docs, controller model-facing schemas, and H5 tests/evidence.

Implementation boundary:

- `plan-work` owns intent, decomposition, acceptance modes, and decision-ready
  capsules; `execute-milestone` owns only implementation judgment inside one
  capsule; `workflow-control` invokes the controller and reports durable status.
- Remove native task creation, callback, recovery, model-routing, attempt-loop,
  and ledger prose from model-visible skills only after the controller path is
  reachable and proven. Keep one explicit legacy command during H5/H6; no hidden
  dual routing.
- Typed Python is the model-facing authoring language for capsules/results;
  JSON/JSONL remains controller serialization. Prompt fixtures must contain the
  intended skill once, acceptance/protected surfaces, and no obsolete peer-
  transport policy.
- Enforce a measured prompt-size budget and compare before/after prompt inputs.
  Validator success alone is not behavioral proof.

Validation: repository/plugin validators, focused prompt fixture assertions,
CLI help and package install, legacy reachability, `make check`, diff checks,
and a real medium milestone initiated by a Codex agent through
`workflow-control`/`codex-flow` rather than native peer tools.

### Completion evidence

Status: H5 is complete and accepted at implementation commit `b57a353`.
`plan-work` and `execute-milestone` now expose only their bounded cognitive
contracts, the new `workflow-control` skill owns controller invocation and
durable status, and `$codex-thread-handoff` remains an explicit separately
selectable legacy route that cannot be mixed into the controller path. Plugin
source metadata advances to `0.1.5+codex.20260825000000`; no installed plugin or
global Codex state was mutated.

Prompt fixtures measured `plan-work` at 3,063 bytes versus 14,193 before,
`execute-milestone` at 3,043 versus 14,310, and `workflow-control` at 1,716
under its 4,000-byte cap. The retained real medium pilot at
`docs/reviews/evidence/h5-workflow-control-medium.json` was initiated through
`workflow-control`/`codex-flow`, used Luna/medium via the canonical controller,
made only its authorized repository edit, preserved the protected file, and
reached `result_durable`/`completed`. Planning independently verified the six
focused H5 tests, CLI help, isolated wheel build, exact evidence, diff hygiene,
full `make check` with 292 tests and 11 validated skills, and a clean worktree.
No finding is known to block H6.

Successor: H6.

## Milestone H6 — Production pilot and legacy retirement decision

### H6-R prerequisite — make the model-facing control boundary reachable

Before the production pilots, repair the proven H5 reachability gap: `codex-flow
schema` publishes `ModelFacingCapsule`, while `codex-flow control` currently
loads only the repository-bound `ExecutionCapsule` projection. The bootstrap
repair is implemented directly in this existing worktree because the broken
boundary cannot execute itself. Its promotion proof must then invoke the fixed
`codex-flow control` entrypoint with a serialized `ModelFacingCapsule`; no native
peer task or `$codex-thread-handoff` is part of this route.

Outcome: `control` accepts the exact closed model-facing schema, validates its
declared acceptance authorities against `workflow.toml`, and deterministically
projects it into the single existing controller execution path. The projection
owns stable run/milestone identity, the selected checkout's physical Git facts,
the executor route, inherited native permission, default bounded validation,
and the model-facing result schema. Existing repository-bound execution
capsules remain an explicit internal/controller compatibility input for fixed
H3/H5/H6 pilots; malformed or ambiguous shapes fail before durable state.

Mutable ownership: one new projection boundary module, the narrow `control`
CLI integration, focused H5/H6 reachability tests, compatibility documentation,
and this canonical plan. Preserve the H6 CLI/ledger/test changes already in the
worktree and integrate without discarding or rewriting them.

Protected surfaces: controller/ledger state semantics, worktree manager, SDK
adapter and native-profile projection, workflow routes and limits, plugin
installation/cache/trust, global Codex state, primary checkout, remotes,
downstream repositories, and the explicit legacy handoff source.

Acceptance modes: `objective` and `architecture`, with distinct
`code-reviewer` and `architecture-reviewer` authorities. A model-facing capsule
round-trips through `control` into one durable execution; identical input has
stable identity and cannot duplicate work; route and authority drift fail
closed; invalid input creates no controller state; protected and pre-existing
worktree changes remain unchanged; focused tests and full `make check` pass;
and the installed wheel is refreshed and proves the same model-facing command
against a disposable repository before H6 pilots begin.

Non-goals: redesigning H4 orchestration, changing model routes or limits,
inventing a second transport, weakening mutation/protected-surface checks,
installing or trusting plugin hooks, deleting legacy behavior, or modifying the
user's dirty primary checkout.

### H6-C prerequisite — Git-native workspace policy and App-native dispatch

The R6A production pilot proved the SDK/controller path can complete a real
milestone and independently promote its repository outcome, but the controller
misclassified Git-ignored frontend build output as protected or out-of-scope
source mutation. The same pilot also proved that a thread created by the
controller's private SDK app-server is not automatically a visible task in the
active Codex desktop app. H6-C closes both product gaps before replacement
pilots continue.

Outcome: workspace integrity follows Git authority, and a second App-native
hosting mode makes controller-owned workers visible as ordinary Codex app
conversations without weakening the durable harness. Tracked files are checked
for protected integrity; tracked changes and non-ignored untracked files are
checked for mutable-scope ownership; Git-ignored files and directories are
excluded from ordinary source-mutation and directory-topology deltas. Explicit
sensitive runtime/configuration roots remain protected by their existing
dedicated integrity mechanisms rather than by accidental inclusion of all
ignored build output.

The App-native boundary is host-mediated, not a second scheduler or an
undocumented socket client. The controller transactionally claims a logical
dispatch and emits one closed action containing route, selected existing
workspace, bounded prompt, and result contract. The hosting Codex app performs
the native non-blocking task creation and returns its thread/host identity; a
closed bind operation records that identity against the exact outstanding
claim. Repeated prepare/bind calls are idempotent, conflicting or invented
identities fail closed, and status/recovery continue to use SQLite. SDK-headless
execution remains supported and cannot be silently substituted for an
App-native request.

Mutable ownership: `src/codex_flow/controller.py`, the narrow App-native
boundary and typed contracts, required ledger/schema and CLI integration,
`src/codex_flow/projection.py`, the source `workflow-control` skill, focused
tests and compatibility documentation. H6-C owns integration and validation of
the existing uncommitted H6-R candidate; it must preserve all unrelated user
changes and retained evidence.

Protected surfaces: this canonical plan and repository instructions; native
Codex configuration/authentication/permissions; installed plugin caches and
trust; the primary checkout, remotes, downstream repositories and legacy
handoff source; existing accepted H1-H5 evidence; SDK provider/config/discovery
projection; and all paths outside the declared mutable set. No worker may push,
merge, rebase, stash, discard, install/trust a plugin, or discover/attach to a
private Desktop app-server endpoint.

Acceptance modes: `objective` and `architecture`, with distinct
`code-reviewer` and `architecture-reviewer` authorities. Objective acceptance
requires regression proof that `.next`, `node_modules` symlinks, caches and
other ignored build output neither change protected digests nor appear in
ordinary mutation/topology scope, while tracked protected edits and
non-ignored untracked out-of-scope files still fail. It also requires closed
prepare/bind/status contracts, stable logical identity, exact route/workspace
projection, no duplicate ownership, and headless compatibility. Architecture
acceptance requires one SQLite authority, one implementation owner, no direct
Desktop socket/app-server protocol dependency, fail-closed host acknowledgments
and explicit separation between App-native and SDK-headless dispatch.

Promotion gates: focused adversarial tests, full `make check`, Ruff and
`git diff --check`, complete diff self-review with zero P0/P1, wheel/package
verification, and one bounded real App-native pilot launched from a visible
Codex app thread. The pilot must bind a real native thread id, remain visible in
the app, use the selected existing worktree without creating a duplicate, make
only its authorized change, and persist a terminal typed result. If the current
host does not expose a required native task action, report that exact capability
as `EXTERNAL_BLOCKED`; do not fall back to the private SDK server and claim UI
visibility.

Non-goals: a custom run dashboard, automatic sidebar manipulation, remote fleet
scheduling, autonomous multi-milestone polling, SDK removal, legacy deletion,
plugin installation/trust, or weakening explicit sensitive-path protection.
H6-C's historical successor was H6-D. The definitive App-independence
requirement supersedes that route; H6 resumes with fresh medium and large pilots
only after H6-E promotes under the detached-supervisor acceptance contract.

### Milestone H6-D — superseded donor: App ref and raw-envelope normalization

The bounded visible H6-C pilot proved the host-mediated task path, real visible
thread binding, selected-worktree reuse and the requested one-line repository
edit. It also proved two App-native promotion defects. First, Codex App writes
host-owned checkpoint and capture refs below `refs/codex/turn-diffs/**` in the
repository's shared common Git directory during an ordinary turn; the v1 Git
authority hashes that volatile namespace and therefore converted the otherwise
valid pilot into `integrity_failure`. Second, the native `create_thread` action
does not accept `output_schema`: the worker returned usable JSON only because
the host improvised the contract and terminal ingestion used a hand-constructed
projection. `wait_threads` is a deliberately compact progress/summary surface,
not the lossless terminal message authority.

This milestone was planned at commit `678105a` but was superseded before
execution by the definitive App-independence requirement. Its uncommitted donor
diff is not a separate implementation owner and must not be discarded or
rewritten before the H6-E executor captures its exact bytes. H6-E absorbs the
following useful, still-required pieces:

- `src/codex_flow/controller.py`: exact semantic normalization of only
  `refs/codex/turn-diffs/**`, including shared-common-directory ref/reflog
  treatment, plus the raw-result completion seam;
- `src/codex_flow/contracts.py`: bounded strict parsing of one complete raw
  `ModelFacingResult`, the canonical schema digest and the in-band result
  envelope formatter;
- `src/codex_flow/app_native.py`: readable version-1 actions and a version-2
  action that binds the result-contract digest without claiming native
  `create_thread` accepts `output_schema`.

The donor's working-tree SHA-256 values at planning time are
`controller.py=38a591b1e5c4712013a9b3acd470b40501900a35c38f74623998f1298c5a0c1d`,
`app_native.py=4eeefe47032ceb5ae920f5ae16981065f47a359b8216dfbd1a8f1b139dfc7d27`
and
`contracts.py=1b4c278d66010dc0105d21612d2b77acfc8ae0b98595c8acc85abe4d7d5cb3ea`.
The planning task preserves all three byte-for-byte. The H6-E implementation
owner may integrate and repair them inside its declared mutable surfaces after
capturing the baseline.

The remainder of the old design is rejected: App-host `read_thread`,
`wait_threads`, a host-authored result object, a source-controller callback, or
an open App cannot be lifecycle, liveness, recovery or terminal-result
authority. The detailed H6-D acceptance text below is retained only as donor
history; H6-E is the sole executable milestone and its stricter gates win.

Historical H6-D outcome: fix exactly those two defects without weakening any other controller
boundary. Git authority semantically normalizes only the exact App-owned
`refs/codex/turn-diffs/` namespace, whether refs are loose or packed and whether
their reflogs live in the shared common Git directory. `HEAD`, the checked-out
branch, index, config, ordinary refs and reflogs, history/operation state,
protected paths, tracked mutations and non-ignored out-of-scope mutations remain
fail-closed. App-native action preparation no longer represents
`output_schema` as an argument accepted by native task creation. Instead, one
controller-owned formatter appends a compact canonical `ModelFacingResult`
schema-version-1 JSON envelope/template and the instruction to emit that JSON
object alone, without prose or a Markdown fence, to the native prompt.
Terminal ingestion accepts the full raw terminal `agentMessage` text for the
exact bound thread, applies the existing bounded strict JSON decoder to the
entire text, constructs `ModelFacingResult`, and only then enters the existing
capability-, identity-, validation- and integrity-checked durable completion
path. A `wait_threads` summary, excerpt, commentary item, inferred object or
host-rewritten dictionary is never terminal-result authority.

Design and ownership:

- `git_authority_snapshot()` keeps its bounded `HEAD`, current branch, Git-dir,
  index, config and operation-state facts. Replace raw all-ref hashing with one
  canonical, bounded ordinary-ref projection derived from Git's ref inventory:
  parse complete ref records strictly, discard a ref only when its full name is
  exactly below `refs/codex/turn-diffs/`, sort and hash every remaining ref name,
  object id and symbolic target. Apply the same exact-subtree exclusion to
  loose-ref and ref-log metadata in the common directory; do not use prefix,
  substring or general `refs/codex/**` exclusions. Packed ordinary refs remain
  protected through the semantic inventory. `HEAD` and ordinary reflogs retain
  transient-history detection, including commit-then-reset.
- The canonical result-envelope formatter lives beside
  `ModelFacingResult`/`model_facing_result_schema()` and is the sole source used
  by App-native prompt construction and its tests. New App-native actions use a
  versioned host contract that binds the result-contract digest but does not
  instruct the host to pass `output_schema` to `create_thread`. Existing
  persisted version-1 action/status rows remain readable and recoverable; no
  second result type, transport, scheduler or ledger authority is introduced.
- The `codex-flow app-result --agent-message PATH` CLI/controller terminal
  boundary reads one bounded raw UTF-8 `agentMessage` payload; the former
  host-authored `--result` JSON-file input is not an alternate authority. It
  rejects BOMs, invalid UTF-8, leading/trailing prose,
  Markdown fences, concatenated JSON, non-object roots, missing/extra keys,
  invalid enums/types and over-limit content, and then delegates the typed
  result to the unchanged `complete_app_native()` authority. Host orchestration
  must obtain the final full message through native `read_thread` for the bound
  `thread_id` and copy its complete raw `agentMessage` text without rewriting;
  `wait_threads` may wait for lifecycle state but its summary text must never be
  ingested.

Mutable ownership:

- `src/codex_flow/controller.py` only for the exact Git-authority normalization
  and raw-result completion entrypoint;
- `src/codex_flow/app_native.py`, `src/codex_flow/contracts.py`,
  `src/codex_flow/projection.py` and `src/codex_flow/cli.py` only for the
  versioned native action, canonical prompt envelope and raw `agentMessage`
  ingestion boundary;
- `tests/test_h3_controller.py`, `tests/test_h5_workflow_control.py`,
  `tests/test_h6_app_native.py` and new focused fixtures required by the two
  defects;
- `docs/reviews/evidence/h6-d-app-native-pilot.json` as the one sanitized,
  retained visible-pilot record.

Protected surfaces:

- `docs/reviews/peer-thread-workflow.md`, `AGENTS.md`, `workflow.toml`, the
  existing uncommitted line in
  `docs/reviews/codex-controller-compatibility.md`, and every other pre-existing
  user change;
- ledger schema/state semantics and `src/codex_flow/ledger.py`; worktree lease,
  mutation-scope and protected-digest behavior outside the owned
  `controller.py` functions; `src/codex_flow/worktrees.py`,
  `src/codex_flow/domain.py`, `src/codex_flow/config.py`,
  `src/codex_flow/native_profile.py`, `src/codex_flow/backends/codex_sdk.py`,
  `src/codex_flow/h6_pilot.py`, all plugin sources, accepted H1-H5 evidence and
  unrelated H6-C behavior;
- installed plugin caches/trust, global Codex configuration/hooks/state, the
  primary checkout, remotes, downstream repositories, legacy handoff source,
  and every path outside the mutable set. No push, merge, rebase, stash,
  discard, plugin installation/trust, legacy disablement/deletion or private
  Desktop socket/app-server discovery is authorized.

Non-goals: broad Git-ignore policy changes; excluding all `refs/codex/**`, all
unknown host refs, ordinary ref logs or packed refs; weakening current-branch,
index, config, history, protected-path or mutation-scope checks; changing
`ModelFacingResult` fields/status semantics; adding native structured-output
support that the host action does not expose; parsing task summaries; changing
routes, budgets, SQLite schema or SDK-headless structured-output behavior; and
running the medium/large H6 parity pilots or making the legacy-retirement
decision.

Acceptance modes: `objective` and `architecture`. The objective authority is
the independent `code-reviewer` (Luna XHigh); the architecture authority is the
independent `architecture-reviewer` (Sol Medium). Both must review the complete
H6-D candidate once, with at most one bounded repair for concrete blockers.
Promotion requires P0=0/P1=0 from both authorities; one authority cannot waive
the other.

Objective acceptance and regression validation:

- In a repository with at least two worktrees sharing one common Git directory,
  create, update and delete loose and packed refs plus reflogs strictly below
  `refs/codex/turn-diffs/**` between baseline and completion. The normalized Git
  authority and a valid App-native completion remain stable. Prove exact-name
  discrimination: `refs/codex/turn-diffs-evil/**`, `refs/codex/other/**`,
  heads, tags and remotes still change the digest and fail completion.
- Retain or extend adversarial regressions proving changes to `HEAD`, current
  branch, index, local/worktree config, ordinary reflogs, merge/rebase/sequencer
  state and commit-then-hard-reset history fail closed. Tracked protected edits,
  tracked out-of-scope edits, non-ignored untracked out-of-scope files and
  terminal-capture races still fail; Git-ignored build-output behavior remains
  unchanged.
- Assert that a newly prepared App-native host action maps only supported
  native creation inputs and carries no `output_schema` argument. Its prompt
  contains exactly one deterministic canonical envelope/template whose digest
  is bound by the closed action/receipt contract; SDK-headless turns continue
  to receive `output_schema` exactly as before. Persisted version-1 App-native
  actions remain readable without being silently reinterpreted as version 2.
- Feed terminal ingestion the full raw valid `agentMessage` and prove one typed,
  idempotent durable result for the exact bound host/thread/capability. Reject a
  truncated or summarized `wait_threads` projection and every malformed form
  listed above before ledger mutation; compare database bytes/state before and
  after each rejection. Reject a correct message for the wrong host/thread or
  stale/cancelled dispatch through the existing closed checks.
- Run the focused H3/H5/H6-D tests, Ruff on every changed Python path,
  `git diff --check`, the complete `make check`, exact dirty-baseline/status and
  protected-surface comparisons, and a full candidate self-review. Preserve the
  existing compatibility-document insertion byte-for-byte.

Packaging and install acceptance: after source gates pass, build one wheel into
a fresh temporary directory; run `uvx --from <exact-wheel> codex-flow --help`,
schema output and the focused App-native prepare/raw-ingest checks against a
disposable Git repository. Then install that exact wheel into fresh temporary
`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` roots and repeat the same CLI checks from the
installed executable. The isolated install must not mutate the user's normal
tool directory, plugin cache/trust, global Codex state or the live H6 ledger,
and source-tree imports must be unavailable during the wheel/install checks.

Architecture acceptance: one SQLite/controller authority and one
implementation owner remain; the exclusion is exact, semantic and bounded
rather than a general App trust bypass; shared-common-dir/worktree behavior is
explicit; v1 action recovery is preserved; the prompt schema/template and
strict parser have one source of truth; raw response ownership is the exact
bound thread's full terminal `agentMessage`; summaries and host-created result
objects are non-authoritative; SDK-headless behavior remains separate; and no
private Desktop/app-server protocol, second transport or schema migration is
introduced.

Visible promotion pilot: run exactly one bounded App-native task from the
installed isolated wheel against a disposable Git repository/worktree. Bind its
real visible native thread, make one declared sentinel-file edit, wait only for
lifecycle completion, read the full raw terminal `agentMessage` from that exact
thread, and ingest that raw text. Retain sanitized evidence of native identity,
supported create inputs, raw-message digest/length (not lossy summary text),
the exact `refs/codex/turn-diffs/**` before/after delta, unchanged normalized Git
authority, successful protected/mutation/validation checks and durable
`COMPLETED`. Confirm the selected worktree gained no duplicate and the source
worktree's pre-existing compatibility-document change and all other dirty bytes
are unchanged. Failure to expose the full raw terminal message is
`EXTERNAL_BLOCKED`; do not substitute `wait_threads`, inferred JSON or the SDK
server. This historical promotion route is superseded; H6 medium/large parity
pilots resume only after H6-E promotion.

### Milestone H6-E — detached supervisor and App-independent completion

#### Outcome and acceptance modes

`codex-flow control` durably queues a workflow and returns after the harness has
accepted ownership. A harness-owned detached supervisor, running as the same OS
user but independently of the invoking terminal and Codex App, executes the
queue, accepts worker results, closes milestones and schedules already-authorized
successors. SQLite remains the only lifecycle and result authority. The source
controller model spends zero tokens and makes zero lifecycle tool calls while a
worker runs.

The milestone declares `objective` and `architecture` acceptance. Luna XHigh is
the independent objective/code authority; Sol Medium is the independent
architecture, lifecycle and security-boundary authority. Both review the exact
candidate once; at most one bounded repair addresses concrete P0/P1 blockers.
Promotion requires P0=0/P1=0 from both. No App-visible or notification success
can waive a durable harness gate.

#### Fixed process and authority model

- The invoking `codex-flow` CLI is a short-lived producer/status client. It
  validates and projects the capsule, commits a queue item, wakes the supervisor
  through local IPC and exits. It never owns the worker after enqueue.
- One repository-bound supervisor owns queue claims and state transitions for
  that repository's `.codex-flow/workflow.db`. It is a deterministic Python
  process, not a model turn. A durable epoch/lease row plus verified PID/process
  birth identity prevents two live supervisors; a stale lease may be stolen
  only after expiry and a liveness check. PID files and socket existence are
  diagnostics, never authority.
- Each model worker runs outside the supervisor process. The SDK-headless runner
  uses only the stable `openai-codex` adapter and pinned runtime. An App-native
  runner may ask the open App to create/display a visible native worker, but the
  worker receives the same result-submission capability and must close through
  the same harness IPC boundary.
- The worker submits its complete raw, bounded `ModelFacingResult` directly with
  `codex-flow worker-submit` over the controller-owned local socket. Neither the
  App, the source controller thread, `read_thread`, `wait_threads`, prose, a
  summary nor a host-created dictionary may translate or submit the result.
- The supervisor strictly decodes, validates and binds the raw bytes to the
  dispatch/generation/capability, rechecks validation and workspace/Git
  integrity, records the terminal result and transition in one SQLite
  transaction, and only then projects artifacts or considers a successor.
- Successors are finite edges already authorized by the durable capsule and H4
  state machine: required review, bounded repair/recovery, or the next declared
  milestone action. Scheduling is a deterministic transaction, not a recursive
  planner or controller-model turn. A material contract/scope/security decision
  still becomes the existing typed decision state.

#### Daemon lifecycle, packaging and service management

The installed wheel exposes the existing `codex-flow` command and an internal
supervisor entrypoint from the same exact distribution. Production Linux uses a
generated per-repository user-systemd unit with `Restart=on-failure`, an exact
absolute installed executable, canonical repository/state-root arguments, a
private runtime directory and no App dependency. `codex-flow supervisor
install|start|status|stop|uninstall` validates the canonical repository identity
and exact wheel version. Install/uninstall are explicit user operations; normal
`control` may start an already-installed unit but may not silently modify user
service configuration. Tests generate and exercise units under temporary
`XDG_CONFIG_HOME`/runtime roots; this milestone does not install a real user
service or mutate the user's normal tool/plugin configuration.

For development and hermetic tests, `codex-flow supervisor run --foreground`
uses the identical supervisor loop. A bounded detached-spawn fallback is
allowed only when configuration explicitly selects it: `Popen(start_new_session=True,
close_fds=True)` plus a one-shot readiness pipe, exact executable/version and
canonical repository identity. It must not pretend to provide boot-time restart.
If neither an installed user service nor the explicitly selected detached mode
can provide the requested recovery contract, enqueue fails `EXTERNAL_BLOCKED`
before a worker call. Supervisor startup performs one recovery scan, then blocks
on its local socket/timers; it never periodically polls SQLite or Codex tasks.

#### Durable schema, queue and idempotency

Advance the SQLite schema from v9 in one serialized, crash-atomic migration.
The exact names may follow repository conventions, but the following facts are
mandatory and closed-schema validated:

- one supervisor authority row: repository/state-root identity, epoch, random
  owner nonce hash, PID and process-birth identity, acquired/renewed/expiry
  times, executable/version digest and requested shutdown state;
- one dispatch queue row per logical dispatch/generation: backend
  (`sdk_headless` or `app_native`), immutable capsule/action/route/workspace and
  result-contract digests, state (`queued`, `claimed`, `starting`, `running`,
  `result_submitted`, `finalizing`, terminal), availability/deadline, claim
  epoch/nonce, attempt number and bound SDK/App thread identity when known;
- one capability row per attempt: dispatch/generation, allowed operation
  `submit_result`, schema/workspace/backend binding, issued/expiry/consumed
  facts, random-token hash and accepted raw-result digest; plaintext capability
  bytes never enter SQLite, logs, artifacts or prompts;
- one successor/outbox fact that makes terminal-result commit and successor
  enqueue atomic, and one optional terminal-notification fact with source task,
  payload digest and outcome (`not_applicable`, `unavailable`, `attempted_ok`,
  `attempted_failed`). A database constraint permits at most one notification
  attempt per terminal dispatch.

Enqueue, claim, bind, submit, finalize and successor scheduling are idempotent
only for byte-identical immutable facts. Conflicts fail closed. A repeated raw
submission with the same capability/result digest returns the recorded terminal
fact without another transition; any different result, consumed token reuse,
wrong dispatch/generation/backend/workspace/schema, stale epoch or terminal
mutation is rejected with exact database bytes unchanged. Queue selection is
FIFO by durable sequence among eligible items, with explicit route/workspace
lease constraints and bounded retry/recovery counts; no wall-clock ordering is
used as identity.

#### Local IPC, authentication and security boundaries

The supervisor listens on a Unix-domain socket below a controller-owned runtime
directory created descriptor-first with no-follow checks, directory mode 0700
and socket mode 0600. Every request is length-prefixed/canonically encoded,
bounded before allocation, versioned and closed. Linux peer credentials must
match the controller OS uid. The same-uid process boundary remains the product
trust boundary, but same uid alone grants no workflow mutation: a 256-bit
single-purpose capability is also required.

The plaintext capability is delivered to the worker in a descriptor-anchored
0400 capability file below the private runtime root (or an inherited read-only
file descriptor for the SDK child), never as an environment variable, argv,
model prompt, SQLite value or artifact. The worker CLI reads it, connects to the
bound socket and submits one raw UTF-8 payload of at most 65,536 bytes. The
capability binds dispatch id, generation, backend, workspace identity and
`ModelFacingResult` schema digest, expires, is consumed transactionally and is
removed best-effort after durable closure. Crash recovery can reissue a new
attempt capability only after invalidating the old attempt and proving the old
worker dead or incapable of submission; ambiguous live work is never duplicated.
Socket substitution, symlink/hardlink/special-file paths, oversized frames,
partial writes, invalid UTF-8/BOM, unknown keys, replay, cross-repository use and
concurrent conflicting submissions are rejected before workflow mutation.

Workers have no direct SQLite write authority and controller state remains
outside their mutable surfaces. Native Codex sandbox/approval/provider/profile
inheritance remains unchanged. Secrets, capability bytes, raw prompts and raw
results are excluded from logs and sanitized retained evidence. No network
listener, privileged daemon, private Desktop socket, direct app-server JSON-RPC,
global Codex config change or App authentication scraping is introduced.

#### Recovery and terminal protocol

On clean start or crash restart the supervisor takes one transactional snapshot
and reconciles each nonterminal item:

- `queued` work is claimable once; a claim committed without spawn is returned
  to eligible state after its expired supervisor epoch;
- a known-live SDK runner remains owned and is allowed to submit; a dead runner
  before thread identity is retried within the typed transport budget; a dead
  runner after durable SDK identity resumes that exact SDK thread through the
  existing checkpoint contract, never starts a duplicate;
- a bound App-native runner is never completed from App status. If it survives
  App closure it submits normally. If it is proven dead before submission, the
  queue records a recoverable App-worker loss and waits for the configured
  App-native recovery action; it is never silently converted to SDK-headless.
  Failure to resume/recreate the visible native worker without the App is an
  App-native capability limitation, not loss of the durable workflow fact;
- `result_submitted`/`finalizing` rows are replayed from the stored bounded raw
  result digest/body to exactly one terminal transaction. Terminal rows are
  immutable. Projection, notification or process death after commit cannot
  reopen them.

The worker terminal protocol is: finish owned work; write one complete schema-v1
`ModelFacingResult`; invoke `codex-flow worker-submit --capability-file <path>
--result-file <path>`; receive a small acknowledgement containing only dispatch,
terminal status and result digest; then exit. The result file and capability
path must be controller-provided private paths, and the raw result is retained
only in the bounded controller record/projection required by the existing
contract.

After terminal commit, an already-open App adapter may make exactly one
best-effort native `send_message` notification to the recorded source task with
`COMPLETION`, run/milestone id and durable status. The attempt is outbox-backed,
non-authoritative and never retried: unavailable App records `unavailable`, an
API error records `attempted_failed`, and success records `attempted_ok`.
Notification happens after successor enqueue and cannot affect completion,
review scheduling or recovery. The source controller is never required to call
`wait_threads` or poll; notification is only a wake-up hint.

#### Adapter boundaries and App-closed truth table

- SDK-headless: start, execute, result submission, review/recovery successors,
  terminal commit and status work with the Codex App never started, closed
  before enqueue, or closed during execution. This is the mandatory production
  route and must have a real App-closed sentinel.
- App-native: the App is permitted only to create/display a visible native
  worker and to attempt the terminal notification. Binding identity does not
  grant it ledger authority. The worker submits directly to harness IPC. If the
  App/runtime keeps the worker alive after the window closes, the workflow must
  durably complete; if closing the App kills the worker and no documented native
  resume exists, H6-E must retain a recoverable nonterminal fact and report the
  exact App-native capability gap. It may not invent completion or substitute
  SDK-headless.
- With the App closed it is impossible for codex-flow to create a new App UI
  task, add/update sidebar or thread-visible content, wake a visible source
  task, or deliver a native notification. These are optional UI projections,
  never SDK-headless lifecycle or result requirements.

#### Mutable ownership

- `src/codex_flow/ledger.py`, `src/codex_flow/controller.py`,
  `src/codex_flow/cli.py` and `src/codex_flow/projection.py` for the v10 queue,
  supervisor-owned transitions, result closure and command boundary;
- `src/codex_flow/supervisor.py`, `src/codex_flow/worker.py`,
  `src/codex_flow/ipc.py` and `src/codex_flow/service.py` as the sole new process,
  worker, local-protocol and service-management implementations;
- `src/codex_flow/backends/codex_sdk.py`, `src/codex_flow/app_native.py` and
  `src/codex_flow/contracts.py` only for runner integration, App projection and
  the retained H6-D normalization/raw-envelope donor;
- `pyproject.toml` and `uv.lock` only for exact entrypoints/package data; focused
  controller/ledger/App/SDK/IPC/service tests, fixtures and temporary service
  templates; `docs/reviews/codex-controller-compatibility.md` and one sanitized
  `docs/reviews/evidence/h6-e-detached-supervisor.json` record.

The H6-E executor is the one mutable owner of all these surfaces, including the
three-file donor. Shared ledger/schema, public CLI/contracts and production
entrypoints have no parallel owner.

#### Protected surfaces and non-goals

Protected: this canonical plan and `AGENTS.md`; `workflow.toml` routes/limits;
`src/codex_flow/domain.py`, `src/codex_flow/config.py`,
`src/codex_flow/native_profile.py`, `src/codex_flow/worktrees.py` and existing
H4 acceptance semantics except the named integration seams; `src/codex_flow/h6_pilot.py`;
all plugin/skill sources, manifests, validators, accepted H1-H5 evidence and
legacy handoff code; installed plugins/trust/hooks, normal user service/config
roots, global Codex authentication/state, the primary checkout, other
worktrees, remotes and downstream repositories; every pre-existing change
outside the three-file donor and every path not expressly mutable.

Non-goals: a general distributed scheduler, remote/network IPC, multi-user or
root service, custom UI/dashboard, App sidebar automation, undocumented App
attachment, provider/backend abstraction, route/model/effort changes, recursive
planning, periodic `wait_threads`/task/status/SQLite polling, controller-model
keepalives, automatic Git push/merge/rebase/stash/discard, real user-service or
plugin installation, legacy retirement, medium/large H6 parity pilots, or
claiming App-native UI operations work while the App is closed.

#### Objective acceptance and adversarial tests

1. Install/build: `make check`, focused tests, Ruff, `git diff --check` and full
   diff self-review pass. Build one wheel in a fresh directory; run help/schema,
   v9-to-v10 migration, foreground supervisor, detached/service-template,
   enqueue/status and worker-submit checks via `uvx --from <exact-wheel>` with
   source imports unavailable. Install the same wheel into temporary
   `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, `XDG_CONFIG_HOME` and runtime roots and
   prove generated units use the exact installed executable/version. Do not
   install/start a real user unit.
2. Queue/idempotency: cover empty/one/500-item FIFO queues, 501/beyond configured
   limits, concurrent producers, two supervisor contenders, lease expiry,
   claim-before-spawn crash, spawn-before-observation crash, duplicate and
   conflicting raw submissions, stale generations, expired/revoked tokens,
   result-commit/projection/notification boundaries and atomic successor enqueue.
3. IPC/security: cover fragmented and coalesced frames, zero/maximum/oversized
   payloads, invalid version/type/UTF-8/BOM/JSON/schema, wrong uid where
   injectable, token guessing/replay/cross-dispatch/cross-repository use,
   socket/capability path substitution, symlink/hardlink/special files,
   concurrent same/different submissions and log/artifact secret scans.
4. Recovery: inject process death at every durable boundary. Restart with one
   fresh supervisor and prove no duplicate model turn, worker, result,
   transition, successor or notification. Resume an identified SDK thread;
   safely retry a pre-identity transport loss; retain rather than guess an
   ambiguous live worker; finalize a submitted result exactly once.
5. Zero-poll/token proof: instrument all controller adapters and native task
   tools during a deliberately blocked worker. From enqueue until direct result
   submission, assert zero `wait_threads`, `read_thread`, list/status polling,
   source-task messages, controller model/API calls and controller-model token
   usage. The supervisor blocks on socket/timer readiness; no status-read count
   scales with elapsed wall time. Only the documented singleton-lease renewal
   write may run at its bounded cadence.
6. App independence: with the App process proven absent, run a real disposable
   SDK-headless worker through direct IPC, observable file change, validation,
   required successor scheduling and durable terminal completion. Open then
   close the App during a second SDK-headless run and prove identical authority.
   For one bounded App-native sentinel, close the App after visible bind and
   before result submission: accept durable completion only from the worker's
   direct IPC; otherwise retain and report the exact recoverable capability gap.
7. Notification: terminal commit and successor scheduling precede notification.
   Test App absent, success, injected failure and supervisor crash around the
   outbox. Each terminal dispatch has zero attempts when no App is available or
   exactly one attempted outcome when available; no path retries or changes the
   terminal bytes/status.
8. Donor/protection: retain exact `refs/codex/turn-diffs/**` normalization and
   adversarial near-prefix/ordinary-ref/history checks; retain strict raw-result
   envelope/parser and v1 action recovery. Capture the initial three donor
   hashes above and prove all unrelated starting bytes/status, protected paths,
   global state and selected worktree topology remain unchanged.

#### Architecture promotion gate and successor

The architecture reviewer must confirm one SQLite authority, one supervisor and
one implementation owner; event-driven no-poll operation; capability-confined
same-uid IPC; crash-atomic migration and transitions; explicit process and App
failure semantics; no secret/token leakage; immutable terminal results; direct
worker-to-harness raw result ownership; exact package/service identity; and
truthful App-closed limitations. The objective reviewer independently confirms
runtime behavior and regressions. Retained sanitized evidence must include
process/service identity digests, queue/epoch transitions, worker/capability and
raw-result digests (never secrets/raw sensitive text), App-absent proof,
zero-poll/token counters, crash points, notification outcome, Git/workspace
integrity and exact wheel identity.

After H6-E promotion, H6 resumes with one medium and one large parity pilot and
the typed legacy-retirement decision. H6-E itself neither runs those pilots nor
retires anything.

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

Acceptance modes: `objective`, `architecture`, plus `visual` for any pilot whose
outcome includes rendered quality.

Implementation boundary and evidence:

- run one medium and one large real repository milestone through the canonical
  controller entrypoint with fixed capsules, explicit routes, real worktrees,
  structured execution/review/repair, and durable retained evidence;
- compare duplicate ownership, terminal-result durability, recovery behavior,
  planner prompt/compaction use, model turns, wall time, and notification
  independence against the retained legacy baseline without inventing cost or
  quality claims;
- exercise local SDK compatibility directly. Record Desktop, idle wake, remote
  host, permission-profile, and native-review capabilities as proven,
  unsupported, not-exposed, or not-run; an unavailable optional capability does
  not falsify the local controller pilot;
- make a typed retirement decision: `retain_legacy`, `disable_hooks_keep_manual`,
  or `ready_for_separate_cleanup`. Default to retention unless production
  reachability and required behavioral parity are both proven.

Promotion requires both pilots to deliver observable repository outcomes,
independent required-mode reviews with zero P0/P1, one recovery/notification-
failure scenario, green repository gates, retained evidence, and an explicit
legacy decision. H6 never deletes installed plugins, global hooks, downstream
pins, or legacy code; any cleanup remains a separately authorized follow-up.

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
- Desktop visibility is an optional H6 App-native projection. It is never
  inferred from SDK thread creation and never required for SDK-headless
  execution, durable completion or recovery. No undocumented Desktop-owned
  app-server attachment is permitted.
- The visible H6-C pilot proved App-native visibility and native identity
  binding, but also exposed false Git-integrity failure from
  `refs/codex/turn-diffs/**` and the lack of native `output_schema`. The partial
  H6-D donor addresses exact ref normalization and strict raw envelopes. H6-E
  absorbs those changes while replacing App/source-thread lifecycle ownership
  with the detached supervisor and direct worker submission contract.
- With the App closed, SDK-headless workflows must remain fully functional.
  Creating/updating visible App UI threads and native notifications are
  impossible and optional. Whether a bound App-native worker survives an App
  close is an acceptance fact to measure, not a prerequisite for headless
  correctness or authority to infer a result.
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
- 2026-08-24: the second independent Luna review rejected `d32e190` with
  P0=0/P1=5/P2=2 despite a green `make check`. It confirmed closure of explicit
  run/milestone identity, arbitrary durable text exclusion, anchored artifact
  writes on Linux, and speculative API aliases. It reproduced counterfeit
  schema and non-causal-history acceptance, a pre-connect hardlink substitution
  that redirects SQLite writes, caller mutation of the exported transition
  table, invalid event/dispatch combinations that make committed databases
  unreopenable, and public raw-connection mutation; it also found indirect
  `O_DIRECTORY` access and non-string identifier coercion. H2 remained
  unintegrated and H3 blocked; the evidence was handed to a fresh Sol Medium
  recovery owner for bounded diagnosis and repair.
- 2026-08-24: Sol Medium continuation `a01596d` returned on exact parent
  `d32e190`, changing four authorized H2 paths. It reports immutable transition
  policy, strict identifier types, pre-mutation event/dispatch validation,
  private SQLite access, canonical DDL identity, full causal replay, and a
  parent-anchored pinned-inode Linux connection through `/proc/self/fd`.
  Planning verified the parent and path scope and reran `make check`
  successfully with 53 tests in the clean executor worktree. H2 remains
  unintegrated and H3 remains blocked pending one fresh independent Sol High
  review of the exact repair and full H2 range.
- 2026-08-24: independent Sol High review denied `a01596d` with
  P0=0/P1=3/P2=2 despite a green `make check`. It confirmed seven prior finding
  classes closed, but reproduced an accepted extra trigger/view/index that
  deletes a run while `create_run` reports success, writes escaping through a
  pre-existing hardlink, silent adoption of a different valid ledger across
  close/reopen, mutation of the transition policy's private backing dictionary,
  broad unused root exports, and a zero-byte residue after failed first open.
  H2 remains unintegrated and H3 remains blocked. The same executor receives one
  bounded repair against the concrete findings above.
- 2026-08-24: the user replaced attempt-count routing ladders with typed
  acceptance and completion-biased recovery. Future milestones declare
  objective, visual, and architecture modes and receive distinct authorities.
  Non-convergence triggers diagnosis, strategy change, or architecture replan
  and continued execution. Only underdetermined intent or new user authority is
  `NEEDS_DECISION`; missing external prerequisites are `EXTERNAL_BLOCKED`; only
  proven infeasibility is `FAILED`. The already-dispatched H2 repair remains the
  sole owner and will receive one final independent integrity verification
  before promotion; no automatic model ladder or user interruption follows from
  a failed check.
- 2026-08-24: H2 repair `269a75b` closed the remaining schema-inventory,
  durable-write, inode-continuity, transition-policy, root-export, and failed-
  first-open findings on top of `a01596d`. Planning verification did not promote
  it: the focused suite reproduced a concurrent-open authority read that could
  observe a dispatch row and its causal event from different SQLite snapshots,
  producing `CorruptSchemaError` during a four-process idempotent claim race.
- 2026-08-24: follow-up `fc6d286` makes authority validation snapshot-consistent
  and adds an explicit concurrent-open regression. Planning verification passed
  the focused H2 suite ten consecutive times and full `make check` with 62
  tests. H2 remains unintegrated pending one fresh independent Sol High review
  of exact target `fc6d286` and full range `fab4cb6..fc6d286`.
- 2026-08-24: independent Sol High review denied `fc6d286` with
  P0=0/P1=3/P2=0. It closed the prior main-schema, inode/hardlink, transition-
  mutation, root-export, failed-open, and concurrent-open classes, but
  reproduced same-connection TEMP schema/metadata mutation that returns success
  then fails reopen, an authority-free `PLANNED` through `ACCEPTED` history, and
  a public transition without an expected predecessor token. The same H2
  workspace/executor owns the bounded repair; H3 remains blocked.
- 2026-08-24: the user made workspace identity program/lane-owned. Fresh
  threads, model changes, reviews, repairs, recovery, rollovers, and sequential
  milestones reuse the same workspace while mutable ownership is singular.
  Managed worktrees move to semantic sibling `<repo>.worktrees/<program[-lane]>`
  paths; H3 and the workflow skills must enforce this before controller use.
- 2026-08-24: repairs `3186662` and `124107b` closed TEMP/schema-metadata
  mutation, authority-free execution history, optional stale-writer tokens,
  mixed-snapshot recovery facts, and the deterministic late-link commit
  boundary. Planning reran focused H2 and full `make check` successfully with 68
  tests; the full seven-commit H2 chain is integrated as `51e1f30..a858d6f`.
- 2026-08-24: a final reviewer verified the exact target/scope and green
  aggregate gates, then was twice stopped by the platform security filter while
  evaluating a continuously hostile same-UID hardlink race. Planning resolved
  the architecture rather than retrying: same-UID processes are explicitly one
  trust boundary, while all repository-path, substitution, cooperative
  concurrency, and deterministic transaction-boundary guarantees remain
  required and green. Under that product threat model H2 has zero open P0/P1 and
  is promoted; H3 is unblocked.
- 2026-08-24: H3 implements the first agent-usable SDK controller vertical
  slice: versioned capsules, SQLite v3 execution facts, program/lane-owned
  current/existing/managed worktree leases, workspace-write SDK execution,
  durable external-call and identity checkpoints, fresh-client resume,
  explicit validation, with result-before-projection ordering proved by the
  deterministic boundary fault injection rather than inferred from the real
  sentinel, and
  `plan/start/resume/status/cancel`. The real disposable sentinel passed with
  one dispatch, one semantic managed worktree, one thread id, 43 ordered SDK
  events, an injected post-turn process boundary, successful fresh-client
  `thread_resume`, unchanged protected paths, and a durable terminal result.
  Hermetic tests and repository gates are green; H3 promotion remains pending
  independent objective and architecture review of the exact candidate.
- 2026-08-24: the second Luna review cycle reopened two P1 mutation-containment
  classes: repository-external writes were invisible to Git-only audits, and
  commit-then-reset plus Git-dir configuration mutations could erase their
  final-tree evidence. A fresh Sol Medium continuation sealed the production
  SDK child with a bounded OS mount policy, added durable schema-v4 sandbox and
  Git-authority facts, and passed 86 focused tests plus the 109-test aggregate
  gate. The first live repair attempt exposed an unintended writable synthetic
  tmp root and the second exposed a DNS break caused by hiding `/run`; both
  launcher defects were repaired and regression-covered. The final actual-SDK
  attempt reached the provider but returned `usage_limit_exceeded` before the
  executor turn. H3 promotion and H4 remain blocked on one unchanged-model live
  sentinel rerun after credit/reset; no denied-write evidence is inferred from
  the deterministic probe or the absence of an external file change.
- 2026-08-24: the user corrected the H3 permission contract. Harness agents
  must inherit native Codex permissions rather than add mandatory Bubblewrap
  containment. The continuation removes hard-coded workspace-write/deny-all
  production overrides, projects the active native provider/profile into
  private mutable runtime state, adds monotonic optional read-only restriction,
  and keeps worktree/Git controls as ownership and evidence boundaries only.
  The prior usage-limit diagnosis is superseded by the missing `codex-lb`
  provider projection; H4 remains blocked pending corrected deterministic and
  real-sentinel evidence plus independent promotion review.
- 2026-08-24: the one authorized corrected real sentinel run reached the SDK
  turn and injected post-turn crash boundary, but resume rejected native
  runtime-added private config as a projection conflict. Deterministic repair
  now restores the validated projection atomically between SDK processes and
  keeps mutable sessions private. The run was not repeated; corrected terminal
  provider/profile, Git-parity, and global-config evidence remain open.
- 2026-08-24: the user authorized exactly one new corrected sentinel for exact
  commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`. It passed on the actual
  Python SDK/bundled app-server route using `gpt-5.6-luna`/`medium` and the
  active `codex-lb` profile. Evidence records inherited native
  `danger-full-access`/`never`, one dispatch and identity reused across the
  injected fresh-process resume, the validated workspace edit, unchanged Git
  authority, unchanged global Codex config bytes, and sanitized profile and
  provider digests. H3 has zero self-reviewed P0/P1; H4 remains blocked pending
  independent objective and architecture promotion review.
- 2026-08-24: objective promotion review of `3014c568` returned
  P0=0/P1=2/P2=2: resume rejected native tightening instead of monotonically
  rebinding it, runtime-home mkdir/chmod could follow a symlink before lstat,
  cancelled executions accepted a late turn observation, and compatibility
  documentation was stale. The existing Sol Medium owner repaired these in
  schema v6 with an atomic permission meet before adapter creation, distinct
  compatibility failure, descriptor-anchored no-follow runtime preparation,
  terminal mutator guards, and deterministic crash/concurrency/path regressions.
  Focused H2/H3/SDK validation passed 100 tests, the tightening concurrency
  regression passed five additional consecutive runs, and `make check` passed
  all 123 tests plus formatting, lint, validator, compileall, and pre-commit.
  Full H3 self-review found zero open P0/P1.
  The prior passing real sentinel remains the production-route evidence; no new
  live run was consumed because transport/provider/model behavior did not
  change. H4 remains blocked pending fresh objective and architecture reviews.
- 2026-08-24: objective re-review of `e580d291` returned P0=0/P1=1/P2=0:
  terminal ledger calls could still acknowledge identical thread-identity and
  terminal-result payloads without changing durable state. The bounded repair
  now applies one terminal-execution guard before every equality/idempotency
  branch across all 12 public H3 execution, checkpoint, lease, and integrity
  mutators. Controller cancellation retains command-level idempotency through
  a read-only terminal precheck. A table-driven close/reopen regression covers
  COMPLETED, FAILED, and CANCELLED against every mutator and proves exact
  database bytes, typed rows, lifecycle events, and workflow events remain
  unchanged after each rejected stale call; a complementary regression retains
  contractual nonterminal idempotency. Focused H2/H3/SDK tests passed 103 tests
  and `make check` passed all 126 tests plus formatting, lint, validator,
  compileall, and pre-commit; `git diff --check` is clean. Full H3 self-review
  found zero open P0/P1. The passing real sentinel was not rerun because this
  repair changes only hermetic ledger acknowledgement semantics, not the proven
  provider/profile/permission route. H4 remains blocked pending fresh objective
  and architecture promotion reviews.
- 2026-08-24: architecture review of `f61c12de` returned P0=0/P1=3/P2=1.
  Repair `02160dc` restricts the `.codex-flow` mutation exemption to the actual
  repository-bound state tree, requires a persistent causal turn-start fact and
  exact durable-fact matching for turn idempotency, and adds schema-v7
  per-milestone workspace baselines while preserving stable program/lane lease
  identity and its original base SHA. Managed and existing worktree regressions
  reject non-authoritative `.codex-flow` writes; close/reopen tests reject
  skipped turn order and retain matching idempotency; two disjoint sequential
  milestones reuse one managed workspace and recover from both a rejected
  preflight mutation and an injected post-baseline/pre-external stop without
  remaining in `STARTING`. Evidence wording now attributes result-before-
  projection ordering to the deterministic boundary fault injection; the
  retained real sentinel proves only its observable production-route facts.
  Focused H2/H3/SDK validation passed 108 tests, `make check` passed all 131
  tests plus formatting, lint, validators, compileall, and pre-commit, and both
  repair and full-range diff checks are clean. Full H3 self-review found zero
  open P0/P1. The real sentinel was not rerun because the SDK/provider/model,
  native permission, and resume transport route did not change. H4 remains
  blocked pending fresh independent objective and architecture promotion review
  of the exact repaired candidate.
- 2026-08-24: architecture promotion review of `02160dc` returned the sole
  P1 H3-ARCH-004: native discovery compatibility used only shallow root
  metadata. Repair `84b3065` replaces it with deterministic descriptor-anchored
  recursive identities for `skills`, `plugins`, and `memories`. Sorted relative
  paths, types, ownership/identity metadata, file-content digests, internal
  symlink targets, roots, and absolute ancestors are covered; external,
  dangling, or cyclic links, hardlinks, special files, substitutions, unsafe
  scan races, and explicit entry/depth/path/per-file/total-byte limit overflow
  fail closed without truncation. `verify_sources()` now compares the original
  captured snapshots before adapter construction and again during private-home
  preparation. Same-length nested drift on all three surfaces and structural,
  symlink, hardlink, special-file, limit, ancestor, and post-turn fresh-resume
  attacks are regression-covered; compatibility failure remains typed and
  distinct from monotonic permission rebinding with zero adapter/resume calls.
  Native-profile sanitized facts advance to v2, but SQLite remains schema v7:
  its existing opaque digest field safely represents the stronger identity,
  and a prior-algorithm in-flight reopen fails typed and closed. Focused
  H2/H3/SDK validation passed 129 tests and `make check` passed all 152 tests
  plus formatting, lint, validators, compileall, and pre-commit. Repair and
  full-H3 diff checks are clean; full-H3 self-review found zero open P0/P1.
  The retained real sentinel was not rerun because the SDK/provider/model,
  permission, and resume transport path did not change. H4 remains blocked
  pending fresh independent objective and architecture promotion review of
  this exact repair.
- 2026-08-24: final architecture review confirmed five promotion blockers.
  Implementation `06a332e` closes H3-ARCH-004A by rejecting discovery-root,
  active-ancestor, multi-node, nested, and dangling link cycles on all native
  discovery surfaces before adapter construction. It closes H3-ARCH-003A with
  schema-v8 accepted terminal HEAD/mutable-root/Git-authority facts verified
  before any successor baseline, lease, adapter, or external call; migrated v7
  predecessors without reconstructable terminal facts require explicit
  reconciliation. It closes H3-ARCH-001A with bounded no-follow directory-
  topology deltas that expose empty-directory creation/removal/rename while
  exempting only the actual repository-bound controller state tree. It closes
  H3-ARCH-005 by canonicalizing physical repository/workspace paths before
  capsule serialization and every execution/lease key or lookup, with legacy
  noncanonical rows failing typed and closed. It closes H3-ARCH-006 through a
  structural native-config projection: literal MCP HTTP headers and sensitive
  stdio environment values become process-only environment references, while
  raw authorization, proxy-authorization, cookie, arbitrary header, API key,
  token, password, and client-secret bytes never enter durable private state.
  The pinned runtime accepts the active projected `codex-lb` profile with zero
  ephemeral secret references; its documented `env_http_headers` surface was
  exercised directly without a provider turn. Focused H2/H3/SDK validation
  passed 177 tests and `make check` passed all 200 tests plus formatting, lint,
  validators, compileall, and pre-commit. The retained real sentinel was not
  rerun because the active SDK/provider/model/permission route and projected
  config are unchanged. Full-H3 self-review has zero open P0/P1; H4 remains
  blocked pending fresh independent objective and architecture promotion
  review of the exact repaired candidate.
- 2026-08-24: the latest final review confirmed four remaining propagation
  blockers. Implementation `eb25987` closes H3-ARCH-003B with one canonical,
  double-captured pre-external authorization operation used before adapter
  construction and every SDK start/resume/turn boundary. The durable
  per-milestone baseline now binds its exact Git-authority digest at the same
  checkpoint; restart and fresh resume reverify HEAD, content, empty-directory
  topology, protected paths, config/index/refs/history, physical lease facts,
  and every accepted predecessor before external work. Authorization races
  fail closed and restore the last safe durable checkpoint when no call was
  made. H3-ARCH-005B is closed by transaction-ordering physical lease
  selection before contender checks: the insertion winner reacquires
  idempotently, unrelated capsule-only PLANNED rows are not active owners, and
  losing lexical/symlink aliases receive `WorkspaceLeaseConflict`. Eight
  threaded and four process interleavings plus identity-crash recovery prove
  one runnable owner, adapter, thread, and retained lease. H3-ARCH-006B is
  closed by a field-typed MCP server schema that rejects every unknown,
  noncanonical, nested, and colliding header shape before projection while
  retaining canonical native MCP fields and process-only secret references.
  H3-ARCH-007 is closed by the shared recursive schema predicate: all objects
  are closed and fully required, arrays have explicit items, scalar/schema
  keywords are closed, and byte/depth/property/item limits are enforced again
  after SDK decoding. Focused adversarial validation passed 50 tests; the full
  H3 suite passed 181 tests, the combined H2/SDK suite passed 50 tests, and
  `make check` passed all 254 tests plus formatting, lint, validators,
  compileall, and pre-commit. The active native profile still projects its
  three configured MCP servers with zero ephemeral secret references. The
  retained real sentinel was not rerun because SDK/provider/model/permission,
  bundled app-server transport, and the canonical sentinel schema are
  unchanged. Full-H3 self-review found zero open P0/P1. H4 remains blocked
  pending fresh independent objective and architecture promotion review of
  this exact repaired candidate.
- 2026-08-25: implementation `ca151fe` repairs the H3-ARCH-006C, H3-ARCH-007A,
  and H3-ARCH-007B candidate gaps on top of repair parent `3efa338`. Literal
  stdio `CODEX_HOME` is now a deterministic process-only collision alias with
  a fixed argv shim that restores the source-bound name only in the MCP child;
  the SDK/app-server keeps its private runtime home and provider/cross-source
  conflicts remain typed and fail closed. The strict decoder explicitly
  decodes byte inputs as UTF-8 without BOM before parsing, and all capsule,
  result, event, projection, and adapter JSON paths use that canonical loader.
  Capsules detach each caller Mapping/Sequence once into owned immutable data
  before validation, and `plan` independently canonicalizes before durable
  writes. Adversarial environment, pinned-runtime parser, child-delivery,
  decoder, durability, mutation, digest, and close/reopen coverage is included;
  the retained real sentinel was not rerun because the SDK/provider/model,
  permission, and resume transport route remain unchanged. H3 remains blocked
  pending fresh independent objective and architecture promotion review; H4
  remains blocked.
- 2026-08-25: after the subsequent independent review was interrupted by the
  platform security gate, the user explicitly ended the open-ended H3
  review/repair loop and accepted exact branch head `544c1c8` as pilot-ready so
  H4 can begin. This decision does not relabel H3 as defect-free or finally
  production-promoted. H4 must classify any concrete inherited defect it
  reaches by both severity and successor promotion impact, while H6 retains the
  final integrated hardening gate. H4-A is selected as the next executable
  slice; another broad H3 review is not one of its prerequisites or promotion
  gates.
- 2026-08-25: H4-A implementation `fafa29b` adds the typed `workflow.toml`
  role/limit authority and durable review, repair, recovery, decision, budget,
  lifecycle, and projection facts on the existing controller and SQLite
  connection. Planning independently verified exact path scope, the retained
  sanitized pilot, seven focused tests, full `make check` with 282 tests, and a
  clean worktree. The single real disposable Python-SDK/`codex-lb` pilot used
  Luna XHigh for two turns on one thread, made an observable authorized edit,
  received one deliberate promotion-blocking P1 rejection, repaired through
  the same durable owner, passed a fresh read-only re-review, and reached
  `ACCEPTED`. H4-A is promoted as the walking skeleton; H4-B is now executable
  without another H3 or H4-A review cycle.
- 2026-08-25: H4-B implementation `662d006` completes the multi-authority
  workflow. Capsules now derive fail-closed distinct executor, objective/code,
  visual, architecture, recovery, and decision routes; immutable rendered
  evidence, finding survival/new-scope lineage, non-blocking deferral,
  boundary-preserving architecture replans, planner decisions, and result-before-
  projection artifacts share the accepted H4/SQLite authority. Planning
  verified the sanitized real pilot, 11 focused tests, full `make check` with
  286 tests, exact diff hygiene, H4-A evidence preservation, and a clean
  worktree. The pilot reused one SDK thread for two `codex-lb` turns and reached
  `ACCEPTED` after objective/visual review, architecture rejection,
  `CONTINUE_WITH_REPLAN`, same-workspace repair, and fresh all-authority
  re-review. H4 is complete; H5 is the next program milestone but was not
  authorized for dispatch by the H4-only continuation request.
- 2026-08-25: the user explicitly authorized continuing through the end of the
  program. H5 and, after its verified terminal callback, H6 may now dispatch in
  fresh sequential execution contexts from this planning owner without another
  routine authorization prompt. This does not authorize plugin installation or
  trust, global/remote mutation, legacy deletion, or bypass of either
  milestone's evidence and promotion gates.
- 2026-08-25: H5 implementation `b57a353` packages the controller-backed
  workflow source and reduces model-visible planning/execution instructions to
  cognitive contracts. Planning verified prompt-input reductions, the 1,716-
  byte `workflow-control` skill, explicit non-mixable legacy reachability,
  plugin metadata `0.1.5+codex.20260825000000`, CLI/schema help, an isolated
  wheel build, six focused tests, full `make check` with 292 tests and 11
  validated skills, and the sanitized real medium pilot. That pilot entered
  through `workflow-control`/`codex-flow`, made only its authorized edit, and
  reached durable completion. H5 is promoted; H6 is the now-executable final
  integrated promotion and legacy-decision milestone.
- 2026-08-25: H6-R implemented the deterministic `ModelFacingCapsule` projection
  and made `codex-flow control` reachable through the packaged SDK controller.
  Focused tests, Ruff, diff checks, isolated-wheel packaging and a prior full
  307-test gate were reported green; later draft integration reached 310 tests
  before the user intentionally interrupted the aggregate run. A real R6A pilot
  nevertheless completed and independently returned `PROMOTE` with zero P0/P1,
  6,715 backend tests, 42 expected skips and 397 frontend tests, but controller
  closure failed because ignored `.next` and `node_modules` output was treated
  as source mutation. Its private SDK thread also did not appear in the active
  Codex app. The user selected Git-native ignored-artifact semantics and an
  App-native visible-worker mode while retaining SDK-headless execution; H6-C
  is now the next executable prerequisite and owns both fixes.
- 2026-08-25: the user raised the milestone wall-clock budget from 15 minutes
  to four hours (`14400` seconds) after the H6-C implementation demonstrated
  that substantial controller work plus the full repository gate can exceed
  the former nominal budget. Per-command and validation timeouts remain bounded
  independently; the longer milestone budget does not authorize polling,
  duplicate workers, weaker gates, or silent recovery.
- 2026-08-25: the user selected architecture-first routing for future
  substantial work. Sol Medium first writes a detailed decision-ready design in
  the canonical plan; only then does Luna XHigh implement it as the single
  mutable owner. Luna XHigh remains the objective/code reviewer and Sol Medium
  checks architecture conformance when declared. Sol High is reserved for
  critical security, irreversible/system-wide decisions or explicit
  escalation. One review per authority and at most one bounded repair for
  concrete blockers replaces repeated broad review waves.
- 2026-08-26: H6-C implementation commit `6feb927` supplied Git-native ignored-
  artifact semantics and the host-mediated App-native prepare/bind/result path.
  The visible pilot bound real task `01a03d56-1658-7e02-a082-890b26251106`,
  reused the selected worktree and produced only the requested existing
  compatibility-document insertion plus a complete result envelope. Durable
  closure nevertheless failed with `integrity_failure`: Codex App created or
  updated host-owned `refs/codex/turn-diffs/**` in the shared common Git
  directory after the baseline. The pilot also proved native `create_thread`
  cannot receive `output_schema`; terminal JSON was reconstructed outside a
  canonical raw-message boundary. H6-C is not promoted. H6-D is the sole next
  milestone and fixes exactly those two defects while preserving the existing
  uncommitted compatibility-document line and all other user changes.
- 2026-08-26: the user made App independence definitive before H6-D execution.
  Commit `678105a` contains the now-superseded H6-D plan; the uncommitted
  three-file donor is retained byte-for-byte by planning and classified above.
  H6-E replaces host callback/read/poll closure with a harness-owned detached
  supervisor, durable SQLite queue, capability-bound direct worker result
  submission, event-driven restart/recovery and post-terminal at-most-once App
  notification. SDK-headless execution is mandatory with the App absent;
  App-native UI creation/update remains truthfully unavailable while closed.
  H6-E is the sole next executable milestone.

## Next execution

Milestone: H6-E — Detached supervisor and App-independent completion.

Next executable capsule (authoritative typed authoring form):

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make codex-flow complete SDK-headless workflows with the Codex App closed by moving queue, "
        "worker lifecycle, direct raw-result closure, recovery and successor scheduling into one "
        "harness-owned detached supervisor and durable SQLite authority."
    ),
    decomposition=(
        "Integrate the three-file H6-D donor and add the crash-atomic v10 queue, supervisor lease, attempt capability, successor and notification-outbox facts.",
        "Implement packaged service/detached lifecycle, event-driven local IPC and capability-bound direct raw ModelFacingResult submission for SDK-headless and App-native workers.",
        "Implement exact restart recovery, immutable terminal closure, optional at-most-once App notification and truthful App-closed limitations without wait_threads or controller-model polling.",
        "Run adversarial unit/integration/migration/package/service gates, App-closed and App-close sentinels, independent objective and architecture reviews, and retain sanitized evidence.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "With the App absent, a real SDK-headless worker completes through direct capability-bound IPC, durable terminal commit and successor scheduling; controller-model token and lifecycle-tool usage while it runs are zero.",
        "One crash-atomic v9-to-v10 SQLite migration and event-driven supervisor provide singleton process ownership, FIFO bounded queueing, exact idempotency and restart recovery without duplicate worker, turn, result, transition, successor or notification.",
        "The worker submits one complete raw bounded ModelFacingResult through the controller CLI/socket; strict dispatch, generation, backend, workspace, schema, peer and single-use capability binding rejects malformed, replayed or conflicting submissions before mutation.",
        "The App is only an optional visible-worker and terminal-notification projection: closure never reads wait_threads/read_thread or awaits a source callback, and notification is post-terminal, best-effort and attempted at most once.",
        "The retained donor continues to normalize only refs/codex/turn-diffs/** and supplies the canonical raw-result envelope/parser while ordinary Git authority, v1 recovery and SDK structured behavior remain fail-closed.",
        "Focused adversarial tests, make check, Ruff, diff hygiene, exact-wheel/temporary-install/service checks, App-closed and App-close sentinels, and exact dirty-baseline protection pass with sanitized retained evidence.",
        "Independent objective and architecture reviews of the exact candidate both report P0=0/P1=0.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Implement only H6-E from the canonical plan in the selected existing worktree. "
        "Capture and preserve the complete dirty baseline, then integrate rather than discard the exact three-file H6-D donor. "
        "Use one mutable owner for schema, queue, supervisor, IPC, adapters, CLI and packaging. "
        "Prove SDK-headless completion with the App absent, direct raw worker submission, crash/restart idempotency, zero wait_threads/read_thread/controller-model polling, and post-terminal at-most-once optional notification. "
        "Test exact wheel and temporary service management without installing a real user service or mutating global Codex/plugin state. "
        "Obtain the declared independent reviews, repair at most once for concrete blockers, retain sanitized evidence, and return one schema-valid ModelFacingResult. "
        "Do not run medium/large parity pilots, retire legacy paths, change routes, or claim App UI operations work while the App is closed."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

Planning validation (2026-08-26): the exact capsule above instantiated and
projected successfully against this checkout as
`run_id=model-36d442572b67827298bb7dd891c780c3`,
`milestone_id=milestone-8e374b232309f1f4cecfb4b6fa6c6010`,
`workspace_mode=existing_worktree`, route `gpt-5.6-luna/xhigh`. Its 23 mutable
and 14 protected surfaces are canonical repository-relative paths with zero
overlap; the execution prompt is 919 bytes under its 12,000-byte cap. The
independent routes remain `code-reviewer = Luna XHigh` and
`architecture-reviewer = Sol Medium` from `workflow.toml` and repository
instructions. This planning-only task does not dispatch any executor or
reviewer.

Planning owner: source task `01a038ae-62ee-7910-ae89-6c13c2e0112c`.

Plan path:
`/home/adam/personal-workflow-skills.worktrees/python-sdk-controller/docs/reviews/peer-thread-workflow.md`.

Execution workspace:

- mode: `existing_worktree`
- repository: `/home/adam/personal-workflow-skills`
- path: `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`
- branch: `agent/python-sdk-controller`
- base SHA: `678105a0e55f89525e8adc0ff90d4ac7c7e75c34`
- lane: `python-sdk-controller`

Starting dirty state is exactly the three H6-D donor files listed and hashed in
the milestone plus this canonical planning change. The executor captures status,
diff and hashes before edits, reuses this worktree and branch, and creates only
disposable repositories/runtime/service roots for tests and sentinels. No other
worktree, repository, installed service, plugin, global configuration or task is
mutable.

The executor owns only the capsule's mutable surfaces, one bounded repair,
validation, the two independent reviews and one durable terminal result. The
planner retains scope, ordering, this plan and program closure. Medium/large
parity pilots and the legacy decision are successors only after H6-E promotion.

Escalate only for genuinely underdetermined user intent or a material public,
persisted, security, destructive-behavior, cost or scope decision. Missing
systemd/App/runtime capability is a typed external/capability fact with the
safe SDK-headless or foreground route retained when its acceptance still holds;
implementation difficulty or App UI absence never authorizes polling, invented
completion, backend substitution, global mutation or weakened gates.

### H6-E terminal audit and bounded repair decision — 2026-08-26

H6-E returned one corrected raw `ModelFacingResult` that parses as the complete
schema-v1 agent message (`1665` bytes, SHA-256
`841def5184f141094138942fc2aad0825ad28dc6351c994ac1257c6dba870562`).
Controller-owned revalidation passed `make check` with 333 tests, protected
surface equality and `git diff --check`, but durable closure correctly recorded
`failed` / `integrity_failure`. The result is therefore implementation evidence,
not promotion authority.

The failure has two reproduced causes. Git authority still changes when the
Codex host updates exact `refs/codex/snapshots/**` refs, although H6-D normalized
only `refs/codex/turn-diffs/**`. Separately, the App controller-state digest
includes `.codex-flow/runs/**`, even though those files are derived lifecycle
projections expected to change at bind and terminal projection. History,
mutable-surface confinement and protected bytes all passed. The candidate also
lacks dedicated supervisor/service/IPC test modules and retained evidence does
not contain the exact wheel, App-absent, crash-point and service identities
required by the H6-E gate. Independent review must not start from this candidate.

The repair boundary is exact:

- normalize only the two proven host-owned volatile namespaces
  `refs/codex/turn-diffs/**` and `refs/codex/snapshots/**`, including matching
  packed-ref and reflog treatment, while rejecting near-prefixes and preserving
  every branch, tag, remote, user ref, HEAD, index, config and ordinary history
  authority;
- make the controller-state digest cover immutable controller inputs and
  unknown state, while excluding only the ledger/lock/runtime and derived
  `.codex-flow/runs/**` projections that the controller itself must mutate;
  capsule bytes remain included and tampering still fails closed;
- add direct migration/queue/supervisor/IPC/worker/service tests and the named
  adversarial bounds, then retain exact sanitized wheel/service/App-absent,
  zero-poll, recovery and integrity evidence rather than a summary claim; and
- replay the failed App-native integrity sentinel and one App-absent real
  SDK-headless direct-submission sentinel. No App-native leaf guarantee may be
  invented; that path remains explicitly unsupported/fail-closed.

This is one bounded repair milestone because the authority fixes, regression
tests and retained evidence share the same controller/ledger integration owner.
Splitting them would create two owners for the same persisted and production
boundaries. The objective and architecture reviews remain two parallel,
first-class, read-only successors after the repaired candidate is frozen.

## Next execution — H6-E-R

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Repair H6-E's reproduced App-owned Git/controller-projection integrity "
        "failures and supply the missing direct supervisor/service/IPC and exact "
        "retained evidence gates without changing the accepted authority model."
    ),
    decomposition=(
        "Correct exact host-ref and derived controller-projection normalization with adversarial near-prefix and tamper tests.",
        "Add direct v10 queue, supervisor, IPC, worker, service, recovery and package tests for the existing implementation.",
        "Run the App-native regression and App-absent SDK-headless direct-submission sentinels and retain sanitized exact evidence.",
        "Freeze the repaired candidate for controller-owned objective and architecture review successors.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "The reproduced H6-E dispatch-integrity scenario closes without normalizing any ref or controller path outside the two exact host-ref prefixes and the named derived/runtime state.",
        "Capsule, ordinary Git authority, near-prefix refs, unknown controller files, protected surfaces and the starting dirty baseline remain fail-closed and byte-preserved.",
        "Dedicated tests directly exercise v10 migration/queue, singleton supervisor and recovery, bounded authenticated IPC, leaf worker submission and exact service identity, including the plan's adversarial bounds.",
        "One exact-wheel App-absent SDK-headless sentinel completes through direct IPC with zero controller-model polling/tokens; the App-native limitation remains truthful and recoverable.",
        "Retained sanitized evidence records exact candidate, wheel and executable digests, test counts, crash/recovery facts, App absence, zero-poll counters and integrity results without secrets or raw prompts/results.",
        "make check, focused tests, Ruff, diff hygiene and self-review pass; the terminal response is one raw schema-v1 JSON object and leaves both independent reviews pending.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_ipc.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Implement only H6-E-R from the canonical plan in the existing python-sdk-controller worktree. "
        "Preserve the complete dirty H6-E candidate and fix the two reproduced integrity boundaries without broad ignore rules. "
        "Add direct tests and exact sanitized evidence for every named supervisor/service/IPC/App-absent gate; repair implementation defects those tests expose within owned surfaces. "
        "Remain a leaf worker: do not create subagents, peer tasks, reviewers or successors, and do not poll Codex tasks. "
        "Do not modify the canonical plan, routes, protected surfaces, global configuration, installed services/plugins or other worktrees. "
        "Do not run medium/large parity pilots or retire legacy code. Return exactly one raw ModelFacingResult JSON object with independent reviews pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-R is the sole executable implementation milestone. After its exact
candidate freezes, the controller may dispatch the objective review (Luna
XHigh) and architecture review (Sol Medium) in parallel on isolated read-only
review workspaces. Medium/large parity pilots remain blocked until both report
zero promotion-blocking P0/P1 findings.

### H6-E-V architecture correction — shared App-visible SDK authority

The user selected one definitive execution architecture after a live local
sentinel disproved the earlier visibility assumption. A normal persisted
`codex exec` thread created inside the saved project was immediately readable
and navigable by the Codex App as task
`01a03dce-3c69-7a10-8eda-48f465c4f3c9`; a projectless `/tmp` CLI thread was
also readable and navigable by exact id. The official Python SDK controls the
local Codex app-server and creates/resumes the same class of local Codex thread.
SDK execution is therefore not inherently invisible to the App.

The harness made its H6-E SDK thread invisible by forcing a private
`CODEX_HOME`. That same isolation also produced a real `401 Missing bearer or
basic authentication` worker failure because the private home did not own the
user's normal Codex authentication. The H6-E-R supervisor and child were stopped
before any new worktree delta. Its durable queue row remains recoverably
`running`: the current cancellation transition is itself defective because row
validation requires an immutable result for `cancelled`, although cancellation
must be terminal without an invented model result. H6-E-R is superseded and
must never be restarted or treated as implementation evidence.

#### Selected process, session and UI model

- The only production execution owner is the harness SDK worker. It uses the
  official local Codex app-server and the canonical user Codex session store;
  it does not set a private `CODEX_HOME`, copy authentication, scrape App state,
  or create a second App-native worker.
- Authentication, model access and normal Codex session persistence remain
  owned by the standard Codex runtime. Harness-specific queue, capability,
  result and successor authority remains repository-bound in SQLite. No secret,
  auth material or full Codex home is copied into the repository or ledger.
- The harness applies leaf and milestone policy through supported process/thread
  configuration overlays (`agents.enabled=false`,
  `features.multi_agent=false`, sandbox/approval/model/effort and bounded
  prompt/result settings) without rewriting the user's global config. Unsupported
  overrides fail before thread creation.
- The App is an optional UI over the same persisted SDK thread. While open it
  can display and let the user inspect that task; if closed before or during
  execution, the SDK/app-server and detached supervisor continue. Reopening the
  App must make the persisted task readable/navigable in its saved project.
  App presence, sidebar refresh and notification never own completion.
- `app_native` is retained only as explicitly labelled legacy compatibility
  during migration. It is not the production worker route, is never paired with
  an SDK worker for the same dispatch, and cannot waive SDK/ledger gates.
- The harness does not keep a controller model alive. SQLite and the supervisor
  deterministically close results, recover work and schedule already-authorized
  successors. An open App may surface the worker transcript and a best-effort
  source notification, but no model waits, polls or translates results.

This preserves what the harness adds beyond native App task management: one
durable lifecycle/result authority independent of UI; crash/restart idempotency;
FIFO queueing and bounded successor scheduling; typed capsule/result contracts;
model/effort/budget and leaf policy; worktree and mutable/protected ownership;
dependency-aware parallel lanes; first-class independent reviews; and retained
auditable evidence. It deliberately does not replace Codex inference,
authentication, transcripts, project registration or the App UI.

#### Recovery correction

The superseded H6-E-R dispatch
`model-74c74cfb35fe3e5195028c974f1af111/milestone-6fd650278d755dd490a5e9b6853c401b/executor/1`
must close exactly once as `cancelled`/superseded without a fabricated
`ModelFacingResult`, successor, notification or restart. Queue validation must
distinguish completed/failed result terminals from resultless cancellation, and
startup recovery must not claim a queue whose owning execution is already
cancelled. Migration/validation must accept only the internally consistent
cancelled shape and reject partial or conflicting terminal facts.

## Next execution — H6-E-V

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make the detached SDK route use the standard App-visible Codex thread "
        "and authentication authority while keeping all orchestration, result, "
        "recovery and ownership authority in codex-flow."
    ),
    decomposition=(
        "Replace private-CODEX_HOME execution with supported shared-session SDK/app-server configuration and immutable per-thread policy overlays.",
        "Repair resultless cancellation and recover the superseded H6-E-R queue without restart, duplicate work or invented result facts.",
        "Prove App-closed continuation plus later App readability/navigation of the same SDK thread in a saved project.",
        "Complete the missing supervisor/IPC/service adversarial tests and exact sanitized evidence before parallel independent reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "A real SDK worker uses normal Codex authentication without copying secrets, persists one standard Codex thread, and is readable/navigable in the App under the selected saved project.",
        "Closing the App before or during the run does not stop queue, SDK/app-server, direct result submission, terminal commit or authorized successor scheduling; reopening surfaces the same thread id.",
        "Leaf, model, effort, sandbox, approval and bounded result policy are applied through supported per-process/thread configuration without modifying global Codex config.",
        "No production dispatch creates both SDK and App-native workers; App-native remains explicitly non-authoritative legacy compatibility.",
        "The superseded H6-E-R queue closes resultlessly as cancelled exactly once and can never be reclaimed, resumed, notified or given a fabricated result/successor.",
        "Dedicated queue/supervisor/IPC/worker/service tests, exact-wheel temporary install, App visibility/closure sentinels, make check, Ruff, diff hygiene and sanitized retained evidence pass.",
        "The implementation worker remains leaf, modifies only owned surfaces, sends no progress callbacks and emits exactly one raw schema-v1 ModelFacingResult with both independent reviews pending.",
    ),
    mutable_surfaces=(
        "src/codex_flow/domain.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/contracts.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_app_native.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_ipc.py",
        "tests/test_h6_visible_sdk.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-V from the canonical plan in the existing python-sdk-controller worktree. "
        "Preserve the complete H6-E dirty candidate and the committed plan. Replace the private CODEX_HOME route with the official shared Codex session/auth path and supported per-thread leaf policy; never copy or expose auth. "
        "Repair the superseded queue as a resultless cancellation and prove it cannot restart. Add the direct tests, exact-wheel evidence, App-closed continuation and later App visibility sentinel specified by the plan. "
        "You are a leaf worker: create no subagents, peer tasks, reviewers or successors and do not poll Codex tasks. Do not modify protected/global state, install a real service, run parity pilots or retire legacy code. "
        "Return exactly one raw ModelFacingResult JSON object; objective and architecture reviews remain controller-owned and pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-V is the sole executable implementation milestone. Its implementation
candidate must be frozen before the controller dispatches the Luna XHigh
objective review and Sol Medium architecture review in parallel. The old H6-E-R
capsule and queue are recovery inputs only, never parallel work.

## H6-E-W architecture correction — harness-owned terminal wake-up

The one-time H6-E-V bootstrap completed outside the supervisor because the
installed predecessor could neither authenticate against the private
`CODEX_HOME` nor cancel its stale queue. Task
`01a03dd7-6f31-7e21-96d6-4dc6ddabbf66` returned one valid terminal
`ModelFacingResult`, but the result remained in the manually owned `codex exec`
process output until the planning controller reconciled that process. This was
not a worker callback failure: the leaf prompt correctly prohibited peer
messages and requested only the typed result. The failure was architectural:
the manual bootstrap bypassed the supervisor-owned SDK worker, capability and
IPC ingress, so no harness component owned terminal acquisition or source-task
wake-up.

The H6-E-V candidate is retained as an implementation donor, not promoted. It
already supplies shared standard Codex authentication/session persistence,
SDK/App-visible worker identity, leaf policy, resultless cancellation, direct
worker-to-supervisor IPC, 341 passing tests and an exact wheel. The planning
controller has also read the exact completed worker task through the App, so
later App readability of the shared SDK task is now observed. The remaining
promotion gap is the App-closed sentinel and a real harness-owned continuation
path; objective and architecture reviews remain pending until that gap closes.

### Definitive callback and continuation model

- Prompt prose never owns callback delivery, result ingress, successor
  scheduling or notification. A worker receives only its bounded work prompt
  and schema, and returns the schema-bounded result through its single-use IPC
  capability. Direct `codex exec` is not a production or recovery execution
  path unless a harness-owned wrapper binds and ingests its result before the
  process is released.
- `CODEX_THREAD_ID`, when present at `codex-flow control` enqueue, is captured
  by the controller as a host-owned source identity. It is not authored in the
  model-facing capsule, copied into a worker prompt or guessed from App state.
  A missing source identity makes UI wake-up `not_applicable`, while durable
  execution and already-authorized successors continue normally.
- Leaf topology and filesystem/process authority are independent controls. At
  enqueue the controller resolves the existing native permission profile and
  binds its effective sandbox and approval authority into immutable route,
  integrity and capability facts. The shared SDK worker must not hardcode
  `workspace-write`: it receives `danger-full-access` when that is the source
  controller's effective authority, and preserves `workspace-write` or
  `read-only` when those are effective instead. Immediately before thread
  creation the worker revalidates the shared native profile and takes the
  monotonic meet; permission drift may narrow execution but can never broaden
  it. No global profile is modified or copied into a private home.
- Terminal result commit and eligibility of already-authorized successors are
  one SQLite transaction. Dependency/DAG policy is fixed before dispatch;
  neither the worker result nor a notification may invent a successor. Reviews
  whose authorities are declared in the capsule are controller-owned successor
  dispatches and may run in parallel only after the exact implementation
  candidate is frozen.
- A separate harness notification runner uses the official shared Codex SDK to
  resume the recorded source thread by exact id with no private `CODEX_HOME`,
  no model/effort/cwd/global-config override and no App dependency. It starts
  exactly one new controller turn containing a bounded harness-authored terminal
  envelope: delivery id, dispatch id, terminal label, durable ledger location
  and result digest. Raw worker prose is never interpolated. The resumed
  controller reads durable authority and continues; this consumes controller
  tokens only after terminal delivery, never while work is running.
- Delivery has its own immutable idempotency key and state machine. Failures
  before a source turn identity may receive one bounded retry. Once the SDK
  returns a source turn id, the outbox records that identity and never starts a
  replacement turn. A crash in the ambiguous bind window is reconciled once by
  exact source-thread history and delivery id; it is never resolved by App
  polling, prompt inference or duplicate delivery. Notification remains
  non-authoritative: result/successor closure never depends on it.
- Enqueue may arm one durable controller-check deadline, 30 minutes by default.
  If the dispatch is still nonterminal when it expires, the supervisor creates
  one distinct `CHECKPOINT` wake-up for the source controller containing only
  dispatch state, exact worker/thread/process identity, start time and last
  harness-observed activity. It does not cancel, resume or duplicate the worker.
  The controller performs one evidence-backed inspection and may explicitly
  re-arm a later checkpoint; automatic recurring wake-ups are forbidden. A
  terminal result cancels an undelivered checkpoint and triggers the immediate
  terminal wake-up instead.
- Worker liveness for that checkpoint is host-owned evidence, not model prose:
  the supervisor persists the spawned PID plus process-birth identity and the
  worker wrapper renews a bounded IPC lease independently of the model output.
  Missing or stale liveness is reported to the controller but never converted
  into completion. `wait_threads`, repeated status reads and an open controller
  turn are not used; the durable timer/wake path remains correct when the App
  and source turn are closed.
- The notification runner is not a worker and does not weaken leaf enforcement.
  Worker SDK threads keep `agents.enabled=false` and
  `features.multi_agent=false`; the resumed source controller retains its own
  stored controller configuration so it can schedule the next authorized work.
- With the App closed, the SDK/app-server, supervisor, workers, result ingress,
  successor scheduling and source-controller wake-up continue. Reopening the
  App is only a projection: it must display the same worker thread and the
  single terminal wake-up turn on the original controller task.

### Persistence, failure and recovery contract

The unpromoted v10 ledger is already present in local recovery state, so the
correction uses one serialized crash-atomic v10-to-v11 migration rather than
editing schema assumptions in place. The source thread identity is an immutable
enqueue fact. A dedicated controller-wake outbox records delivery id, dispatch,
source thread, wake kind (`checkpoint` or `terminal`), payload digest, state
(`not_applicable`, `pending`, `starting`, `delivered`, `failed`, `ambiguous`),
bounded attempt count, optional source turn id and timestamps. The queue also
records the optional next controller-check deadline, worker PID/process-birth
identity, last harness liveness time and the effective native permission facts
plus their source-profile digest. Closed-schema validation rejects partial identities,
conflicting payloads, terminal mutation, duplicate active delivery and a wake
for resultless cancellation.

Supervisor restart performs one bounded recovery snapshot. Pending pre-turn
delivery may be retried within its budget; a recorded source turn is immutable;
an ambiguous post-call delivery is reconciled once through the standard SDK
session store. A source task that is currently running or cannot be resumed is
recorded truthfully without reopening the completed dispatch or suppressing
eligible successors. No `wait_threads`, `read_thread`, App action, controller
model keepalive, periodic task polling, raw Desktop socket or authentication
copy is introduced.

### Promotion proof and non-goals

The production pilot must start from a normal controller task, enqueue through
the exact installed wheel/service, close the App before or during a real Luna
leaf turn, and prove from durable facts that the worker submitted, terminalized
and released its pre-authorized successors while no controller model was
running. A delayed fixture must first produce exactly one 30-minute-equivalent
checkpoint wake-up without a second worker or recurring controller turn; the
controller explicitly re-arms or continues once. The detached notifier must
then resume the exact source task once for the terminal outcome.
After the App reopens, both the original worker thread and one source callback
turn with the same delivery id must be readable/navigable. The pilot also
injects pre-identity, post-identity and restart failures and proves no duplicate
worker, result, successor, notification or controller turn.

This milestone does not build a replacement UI, scrape the App, make App-native
the production route, expose auth, copy a Codex home, accept worker-authored
successors, or promise exactly-once external delivery where SDK evidence cannot
disambiguate process death. It also does not repair unrelated pre-commit files:
the previously reported `.agents/plugins/marketplace.json` blocker was a worker
sandbox artifact; the file is writable and already newline-terminated outside
that sandbox.

## Next execution — H6-E-W

```python
ModelFacingCapsule(
    schema_version=1,
    objective=(
        "Make terminal acquisition, authorized successor scheduling and source-controller wake-up "
        "fully harness-owned and App-independent, with no worker prompt callback responsibility."
    ),
    decomposition=(
        "Add the crash-atomic v11 source-identity, worker-liveness, one-shot controller checkpoint and terminal-wake outbox contract with exact idempotency and restart reconciliation.",
        "Capture the host-owned source thread and effective native permission authority at enqueue, preserve its monotonic sandbox/approval boundary in the shared leaf worker, and deliver one bounded terminal wake-up by resuming the exact standard SDK source thread.",
        "Atomically release only pre-authorized successor and parallel review dispatches after terminal result commit, while workers remain capability-bound leaves.",
        "Run exact-wheel/service, App-closed continuation and later App-visible callback pilots plus failure-injection gates before parallel independent reviews.",
    ),
    acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    acceptance_criteria=(
        "Worker prompts contain no callback, peer-message, result-routing or successor responsibility; the production SDK worker submits exactly one schema-bounded result through capability-bound IPC and the harness durably closes it.",
        "The controller captures CODEX_THREAD_ID as a host-owned enqueue fact and a detached shared-session SDK notifier resumes that exact source task once, with no private home, App dependency, auth copy, or model/effort/cwd/global override.",
        "The detached worker inherits the controller's effective native sandbox and approval authority instead of hardcoding workspace-write: danger-full-access remains available when natively granted, while profile drift or an explicitly narrower capsule can only reduce authority before thread creation.",
        "A configurable one-shot controller checkpoint defaults to 30 minutes: while the dispatch remains nonterminal it wakes the source controller once with harness-owned liveness facts, never polls Codex tasks or mutates the worker, and repeats only after explicit controller re-arming.",
        "Terminal commit atomically releases only successors authorized before execution, including disjoint objective and architecture review lanes; no worker or notification can invent, duplicate or suppress a successor.",
        "The v10-to-v11 migration and wake outbox reject conflicting identity, payload, replay and cancellation facts and recover pre-identity, post-identity and crash-window outcomes without duplicate controller turns.",
        "A real installed-wheel pilot completes with the App closed, uses zero controller-model tokens and zero lifecycle polling while the worker runs, then creates one source-controller wake-up turn and exposes both persisted tasks after the App reopens.",
        "Focused adversarial tests, make check, Ruff, diff hygiene, exact wheel/service checks, protected hashes and sanitized evidence pass; the retained H6-E-V candidate remains intact except for owned integration changes.",
        "Independent Luna XHigh objective and Sol Medium architecture reviews of the exact combined candidate both report P0=0/P1=0 after at most one bounded concrete repair.",
    ),
    mutable_surfaces=(
        "src/codex_flow/ledger.py",
        "src/codex_flow/controller.py",
        "src/codex_flow/cli.py",
        "src/codex_flow/supervisor.py",
        "src/codex_flow/worker.py",
        "src/codex_flow/ipc.py",
        "src/codex_flow/service.py",
        "src/codex_flow/backends/codex_sdk.py",
        "src/codex_flow/contracts.py",
        "src/codex_flow/config.py",
        "src/codex_flow/native_profile.py",
        "pyproject.toml",
        "uv.lock",
        "tests/test_h2_ledger.py",
        "tests/test_h3_controller.py",
        "tests/test_codex_sdk_adapter.py",
        "tests/test_h5_workflow_control.py",
        "tests/test_h6_model_facing_projection.py",
        "tests/test_h6_supervisor.py",
        "tests/test_h6_service.py",
        "tests/test_h6_ipc.py",
        "tests/test_h6_visible_sdk.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
    ),
    protected_surfaces=(
        "docs/reviews/peer-thread-workflow.md",
        "AGENTS.md",
        "workflow.toml",
        "src/codex_flow/domain.py",
        "src/codex_flow/projection.py",
        "src/codex_flow/worktrees.py",
        "src/codex_flow/app_native.py",
        "src/codex_flow/h6_pilot.py",
        "plugins",
        "skills",
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
    ),
    authorities=(
        ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
        ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
    ),
    prompt=(
        "Use execute-milestone and implement only H6-E-W from the canonical plan in the existing python-sdk-controller worktree. "
        "Preserve the complete H6-E-V donor and committed plan. Remove all callback responsibility from worker prompts: result ingress, successor release and controller wake-up are harness-owned. "
        "Add the v11 source identity, effective native permission binding, process/lease liveness, one-shot 30-minute controller checkpoint and terminal wake outbox; preserve danger-full-access when natively granted without ever broadening authority, resume the exact source controller through the shared standard SDK, and release only pre-authorized successors. "
        "Keep workers leaf and create no subagents, peer tasks or reviews. Do not modify protected/global state, copy auth, use App polling or introduce another transport. "
        "Run the bounded deterministic gates and prepare the exact installed-wheel App-closed/App-reopen pilot; if closing the App requires user action, return one precise pilot command/checkpoint rather than weakening or simulating the gate. "
        "Return exactly one raw schema-v1 ModelFacingResult; objective and architecture reviews remain controller-owned and pending."
    ),
    recovery_policy="completion_biased",
    prompt_budget_bytes=12_000,
)
```

H6-E-W is the sole next executable implementation milestone. H6-E-V is its
same-worktree donor, not a parallel owner. Freeze the combined candidate before
the controller dispatches the objective and architecture reviews in parallel.
