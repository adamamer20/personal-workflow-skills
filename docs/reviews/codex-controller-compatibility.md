# Codex controller compatibility

## Supported H3 contract

- Python baseline: 3.12.
- SDK transport: `openai-codex==0.147.0`, high-level `Codex` API only.
- Permissions: H1 compatibility checks may explicitly request `read_only`.
  H3 defaults to `inherit_native`, supplies no SDK sandbox/approval override,
  and therefore follows the active native Codex config on each new execution.
  A capsule may request `read_only` as a typed monotonic restriction; no H3
  value can broaden native authority.
- Runtime: local Linux, with Git worktrees and the repository-local SQLite v3
  ledger as authority.
- Workspace modes: current checkout, an exact existing linked worktree, or a
  controller-created semantic sibling managed worktree.
- Resume: a new controller and SDK client use `thread_resume` with the durable
  identity and reject any identity change.

## Native runtime/profile/private-state matrix

| Surface | H3 authority | Persistence and proof |
| --- | --- | --- |
| Bundled runtime/app-server | Python SDK | Normal SDK child launch; no CLI/direct-RPC transport and no mandatory Bubblewrap wrapper |
| Provider/profile semantics | Native Codex config | Typed projection preserves selected provider definition, model catalog, permissions, MCP, skills, plugins, memories, hooks, projects, and shell environment; sanitized facts plus digest are durable |
| Model, effort, cwd/workspace | Controller capsule | Explicit SDK thread/turn inputs; they do not alter inherited permission authority |
| Approval and sandbox | Native Codex by default | No SDK override for `inherit_native`; optional `read_only` supplies only a monotonic sandbox restriction |
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
validation, commits the terminal result, and only afterwards writes the
projection.

## Evidence

`docs/reviews/evidence/h3-controller-sentinel.json` retains the earlier editing
baseline plus the failed corrected native-profile run. The corrected run reached
the SDK turn and injected post-turn crash boundary, but fresh resume stopped on
a private-config reprojection conflict before terminal evidence. That defect is
deterministically repaired, but no second real run was performed. Sanitized
`codex-lb` terminal parity, unchanged Git authority, and unchanged global native
config therefore remain unproven for the corrected exact route. External-write
denial is not an H3 requirement when the inherited native profile permits
repository-external writes.

Desktop visibility, idle wake, remote hosts, permission-profile survival, and
native review remain outside H3. Later milestones must label each as proven,
unsupported, not exposed, or not run; none is inferred from local SDK success.
