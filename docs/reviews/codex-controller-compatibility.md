# Codex controller compatibility

## Supported H3 contract

- Python baseline: 3.12.
- SDK transport: `openai-codex==0.147.0`, high-level `Codex` API only.
- Permissions: H1 compatibility checks may explicitly request `read_only`.
  H3 defaults to `inherit_native`, supplies no SDK sandbox/approval override,
  and therefore follows the active native Codex config on each new execution.
  A capsule may request `read_only` as a typed monotonic restriction; no H3
  value can broaden native authority.
- Runtime: local Linux, with Git worktrees and the repository-local SQLite
  schema v7 ledger as authority. Schema v7 retains the v6 split between immutable
  native compatibility identity and mutable effective permission authority, and
  adds causal turn-start and per-milestone workspace-baseline facts.
- Workspace modes: current checkout, an exact existing linked worktree, or a
  controller-created semantic sibling managed worktree.
- Resume: before a new SDK client or `thread_resume`, the controller re-resolves
  native configuration and atomically persists the meet of the durable and
  current permission authorities. Tightening is inherited; broadening retains
  the prior restriction. Provider, routing, model-catalog, or discovery changes
  fail closed as a distinct same-thread compatibility error.
- Migration: schema-v5/v6 terminal history is preserved as legacy evidence. An
  in-flight legacy execution without a reconstructable workspace baseline or
  split compatibility/permission authority fails same-thread resume closed
  instead of guessing.

## Native runtime/profile/private-state matrix

| Surface | H3 authority | Persistence and proof |
| --- | --- | --- |
| Bundled runtime/app-server | Python SDK | Normal SDK child launch; no CLI/direct-RPC transport and no mandatory Bubblewrap wrapper |
| Provider/profile semantics | Native Codex config | Typed projection preserves selected provider definition, model catalog, permissions, MCP, skills, plugins, memories, hooks, projects, and shell environment; sanitized facts plus digest are durable |
| Model, effort, cwd/workspace | Controller capsule | Explicit SDK thread/turn inputs; they do not alter inherited permission authority |
| Approval and sandbox | Native Codex by default | Schema-v6 effective authority is rebound before resume; optional `read_only` and previously inherited restrictions can only be retained or tightened |
| Session/database/log state | Controller-private `CODEX_HOME` | New runtime home per run/milestone; global config is source-only and byte-hashed before/after the real sentinel |
| Provider authentication | Environment key reference | `env_key` name is projected and recorded; secret values are neither copied into config nor persisted |
| Worktree/Git controls | Controller ownership/evidence | Leases, mutation contract, and bounded Git-authority snapshots remain; they are not an OS containment claim |

## Persisted-empty-thread boundary

The pinned SDK runtime does not materialize a resumable rollout at
`thread_start` alone. A new SDK client returns `no rollout found` if the first
client stops after identity but before the first turn creates the rollout. The
controller records that identity and never starts a replacement thread; the
resume error is therefore fail-closed rather than a duplicate-owner fallback.

The H3 real promotion sentinel injects its process boundary at the first
resumable checkpoint: thread identity and the first turn observation are both
durable, while validation, terminal result, state transition, and projection
remain incomplete. A fresh controller then resumes the same SDK thread, runs
validation, and returns with both the terminal result and projection durable.
That real observation proves survival across the process boundary, not their
relative write order. Result-before-projection ordering is proved separately by
the deterministic `before_projection` fault-injection regression, which reads
the committed terminal status at the exact projection boundary.

## Evidence

`docs/reviews/evidence/h3-controller-sentinel.json` retains the passing corrected
run for implementation commit `04e0f889678770bfecd7da3c0a8cbd03344f123f`.
It proves one Python-SDK/bundled-app-server dispatch through `codex-lb`, inherited
native `danger-full-access`/`never`, an injected post-turn process boundary,
fresh-process same-thread resume, the allowed workspace edit, equal Git-authority
digests, and unchanged global native config bytes. The schema-v7 causal-workspace
repair retains schema-v6 monotonic resume and does not invalidate those
production-route facts;
no additional real run was consumed. External-write denial is not an H3
requirement when the inherited native profile permits repository-external writes.

Desktop visibility, idle wake, remote hosts, permission-profile survival, and
native review remain outside H3. Later milestones must label each as proven,
unsupported, not exposed, or not run; none is inferred from local SDK success.
