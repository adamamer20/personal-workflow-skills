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

The SDK adapter owns one app-server connection boundary and normalizes installed
SDK objects/events into controller-owned types. No controller component parses
raw SDK or app-server envelopes outside this adapter. Direct app-server JSON-RPC
is out of scope unless a later explicit milestone replaces the SDK after a
documented capability failure.

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
promotion sentinel are complete; H4 remains blocked pending independent
objective and architecture promotion review of the exact candidate.
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

Acceptance: completed, continue-with-replan, needs-decision, repair-required,
external-blocked, context-rollover, and transport-failure scenarios have typed
deterministic integration tests. Replanning continues automatically when intent,
public/persisted contracts, security/privacy boundary, material cost,
destructive behavior, and scope are unchanged. Review is read-only; mixed
acceptance modes require every distinct authority; new reviewer scope cannot
masquerade as a surviving finding; thread and compaction limits fail closed.

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
  evidence, acceptance criterion, and whether it survives the exact prior
  repair. New scope cannot impersonate a surviving finding.
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

Validation includes hermetic scenario tests for every role/mode/result,
surviving-versus-new findings, recovery replans, budget exhaustion, stale
reviewers, notification failure, and result ordering, plus one disposable real
SDK milestone that is rejected, repaired, separately code/visual reviewed, and
accepted with durable evidence.

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

## Next execution

Milestone: H3 — controller-owned execution workspace, SDK execution, durable
resume, validation, and agent-usable CLI vertical slice.

Execution workspace:

- mode: `existing_worktree`
- repository: `/home/adam/personal-workflow-skills`
- path: `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`
- branch: `agent/python-sdk-controller`
- base SHA: `6a2ac17`
- lane: `python-sdk-controller`

Dispatch status: H3 implementation commit `ca151fe` closes H3-ARCH-006C,
H3-ARCH-007A, and H3-ARCH-007B on repair parent `3efa338`; this plan and the
compatibility contract are updated in subsequent documentation commits.
Deterministic gates and the retained authorized real sentinel are green in the
existing semantic workspace. The repaired H3 implementation candidate is ready
for fresh independent promotion review.

Next action: independently review exact repaired implementation HEAD `ca151fe`
for objective and architecture promotion with zero open P0/P1. The retained real evidence
proves the actual Python SDK route through `codex-lb`, sanitized native
permission/profile parity, the allowed workspace edit and durable resume,
unchanged Git authority, and unchanged global Codex config bytes. External-
write denial is not an H3 gate. H4 remains blocked until that review promotes
H3.
