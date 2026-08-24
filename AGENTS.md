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

## Safety and review

- Do not use `eval` or `exec`.
- Make side effects explicit and bounded. Any real SDK sentinel must use a
  disposable Git repository and `Sandbox.read_only`, then compare before/after
  bytes. Archive only the sentinel thread it created after proof.
- Tests, manifests, counts, and status metadata support evidence; they do not
  replace a real observable outcome. A missing SDK capability is a truthful
  `BLOCKED` result with the smallest decision-ready gap.
- Before committing, inspect the exact staged paths, run `git diff --check`,
  and self-review the complete milestone diff. Do not push, merge, rebase, stash, or
  discard changes without separate authorization.
