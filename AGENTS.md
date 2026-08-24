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
CODEX_FLOW_REAL_SDK=1 uv run codex-flow sdk-sentinel \
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
  PEP 484/695 annotations, typed boundary models, and explicit protocols.
- Convert raw SDK payloads at the adapter boundary. Do not use
  `getattr`/`setattr`/`hasattr` for expected interfaces; an interface drift
  must fail visibly in typed code or a focused contract test.
- Use parameterized logging/SQL when those surfaces are introduced. SQLite is
  the intentional later controller ledger; do not add PostgreSQL-only rules.
- Read secrets from environment variables only. Never hard-code credentials,
  paths, hosts, or URLs, and never mutate global Codex configuration,
  authentication, hooks, remotes, or unrelated tasks.

## Program ownership and boundaries

The single active program plan is
`docs/reviews/peer-thread-workflow.md`. Read its current milestone, mutable
ownership, protected surfaces, acceptance, and promotion gate before changing
code. The planning task owns architecture, scope changes, milestone ordering,
the plan, and this instruction file. Each execution task owns exactly one
decision-ready milestone and may not expand into a later milestone.

The `openai-codex` adapter is the only production Codex transport. There is no
CLI backend and no direct app-server fallback. SQLite is the single durable
workflow ledger. Give the SDK adapter, ledger/schema, worktree manager,
controller state machine, routing configuration, and model-facing contracts one
implementation owner at a time; do not add alternate scaffolding or duplicate
production paths.

Existing plugin hooks, workflow/audit skills, manifests, validators, the
protected primary checkout, remotes, global Codex state, downstream
repositories, and later milestones remain protected unless the canonical plan
explicitly assigns them to the current owner. Do not silently substitute
models, reasoning effort, transports, permissions, or acceptance gates.

## Execution workspace topology

- A fresh Codex thread or model context does not imply a fresh Git worktree. A
  worktree is an isolated mutable workspace owned by a program or execution
  lane, not by Luna, Sol, a reviewer, or a thread id.
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
- The canonical plan selects `current_checkout`, `existing_worktree`, or
  `managed_worktree` before task creation and records the exact repository,
  path, branch, base SHA, and lane when applicable. Handoff transports launch
  threads; they do not invent Git topology or silently create another worktree.
- When the native task API cannot address the selected existing workspace,
  fail the handoff closed and preserve the workspace capsule. Never substitute
  a runtime-generated worktree merely because a fresh thread was requested.

## Controller routing and recovery semantics

- Model milestones with explicit `objective`, `visual`, and `architecture`
  acceptance modes. Derive implementation and independent review authorities
  per mode; never infer them from file extension or repository area.
- Objective/code review uses Luna. Visual-judgment implementation and rendered-
  quality review use Sol, with a separate objective reviewer when both modes
  apply. Architecture and recovery diagnosis use Sol High.
- Non-convergence changes authority or approach; it does not terminate work.
  The recovery authority may finish locally, replace the implementation
  strategy, or update an implementation-level architecture assumption and
  continue when accepted outcome, public/persisted contracts, security/privacy
  boundary, material cost, destructive behavior, and scope remain unchanged.
- `CONTINUE_WITH_REPLAN` is internal and nonterminal. `NEEDS_DECISION` is only
  for genuinely underdetermined user intent or missing user authority.
  `EXTERNAL_BLOCKED` is only for missing credentials, permission, service,
  hardware, or another external prerequisite. `FAILED` means evidence shows the
  goal is not reasonably achievable under accepted constraints.
- Never escalate to the user merely because implementation is difficult, a
  previous model did not converge, or an implementation plan was disproven.

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
