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
  schema v8 ledger as authority. Schema v8 retains schema v7 causal turn-start
  and per-milestone workspace-baseline facts, and atomically adds each accepted
  terminal milestone's HEAD plus predecessor-owned workspace snapshot.
- Workspace modes: current checkout, an exact existing linked worktree, or a
  controller-created semantic sibling managed worktree.
- Resume: before a new SDK client or `thread_resume`, the controller re-resolves
  native configuration and atomically persists the meet of the durable and
  current permission authorities. Tightening is inherited; broadening retains
  the prior restriction. Provider, routing, model-catalog, or discovery changes
  fail closed as a distinct same-thread compatibility error.
- Workspace identity: repository and workspace spellings resolve to one
  canonical physical path before capsule serialization, execution/lease keys,
  or ledger lookup. Lexical, relative, trailing-component, and symlink aliases
  cannot acquire another owner or SDK thread. Noncanonical legacy keys fail
  schema validation closed.
- Migration: schema-v5/v6 terminal history is preserved as legacy evidence.
  Schema-v7 rows migrate forward without inventing terminal content; a
  completed v7 predecessor that lacks an accepted terminal snapshot requires
  explicit reconciliation before a successor can start. An in-flight legacy
  execution without reconstructable baseline or compatibility authority fails
  same-thread resume closed instead of guessing.

Native-profile facts v2 recursively fingerprint each configured `skills`,
`plugins`, and `memories` tree through no-follow directory descriptors. The
identity covers relative path bytes, type and ownership metadata, file-content
digests, internal symlink targets, discovery-root identity, and every absolute
ancestor identity. External/dangling/cyclic symlinks, including links to the
discovery root or any active ancestor, hard-linked regular files, special file
types, substitutions, and scan races fail closed. Each
surface is bounded to 100,000 entries, depth 64, 4,096 relative-path bytes,
512 MiB per file, and 2 GiB total file bytes; exceeding a bound is an error,
never truncation. Durable facts retain only the compatibility digest and
sanitized bounded metadata, never discovery file contents or sensitive raw
configuration. An in-flight prior-algorithm fingerprint reopens as a typed
compatibility failure before adapter construction.

Workspace mutation authority combines Git-visible paths with a bounded
no-follow directory-topology delta. Empty directory creation, deletion, and
rename are therefore visible for current, existing, and managed worktrees;
only the controller's actual repository-bound `.codex-flow` state tree is
exempt. Accepted terminal snapshots cover mutable-root absence, file and
directory type/mode/content, empty subdirectories, symlink rejection, hardlink
count, terminal HEAD, and Git authority. Every completed predecessor is
verified before a successor baseline, lease, adapter, or external call.

Native config projection is structural. Literal MCP HTTP headers become
`env_http_headers` references backed only by the SDK child's ephemeral
environment; sensitive MCP stdio environment entries become `env_vars`
references. Authorization, proxy authorization, cookies, arbitrary header
values, API keys/tokens/passwords/client secrets, case variants, and nested
header shapes cannot reach the ledger, artifacts, evidence, errors, or private
runtime files. Conflicting environment semantics fail closed. Existing native
references such as provider `env_key`, MCP `bearer_token_env_var`, and
`env_http_headers` remain references.

## Native runtime/profile/private-state matrix

| Surface | H3 authority | Persistence and proof |
| --- | --- | --- |
| Bundled runtime/app-server | Python SDK | Normal SDK child launch; no CLI/direct-RPC transport and no mandatory Bubblewrap wrapper |
| Provider/profile semantics | Native Codex config | Typed projection preserves selected provider definition, model catalog, permissions, MCP, skills, plugins, memories, hooks, projects, and shell environment; sanitized facts plus digest are durable |
| Model, effort, cwd/workspace | Controller capsule | Explicit SDK thread/turn inputs; they do not alter inherited permission authority |
| Approval and sandbox | Native Codex by default | Schema-v6 effective authority is rebound before resume; optional `read_only` and previously inherited restrictions can only be retained or tightened |
| Session/database/log state | Controller-private `CODEX_HOME` | New runtime home per run/milestone; projected config contains references only; global config remains source-only |
| Provider/MCP authentication | Environment references | Provider `env_key`, MCP bearer/header references, and structurally converted literal header or sensitive stdio values reach only the SDK child environment; raw values are never persisted |
| Worktree/Git controls | Controller ownership/evidence | Canonical physical leases, schema-v8 baselines/terminal snapshots, bounded topology facts, and Git authority remain ownership/evidence controls, not an OS containment claim |

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
digests, and unchanged global native config bytes. Schema-v8 terminal authority,
canonical workspace identity, directory topology, discovery-cycle rejection,
and secret projection retain the proven SDK/provider/model/permission route.
The active `codex-lb` profile still projects with zero ephemeral secret
references and the pinned runtime's config diagnostic loads it successfully;
no additional real provider run was consumed. External-write denial is not an H3
requirement when the inherited native profile permits repository-external writes.

Desktop visibility, idle wake, remote hosts, permission-profile survival, and
native review remain outside H3. Later milestones must label each as proven,
unsupported, not exposed, or not run; none is inferred from local SDK success.
