# Codex controller compatibility

## Supported H3 contract

- Python baseline: 3.12.
- SDK transport: `openai-codex==0.147.0`, high-level `Codex` API only.
- Sandboxes: explicit `read_only` for compatibility checks and explicit
  `workspace_write` for controller execution; `full_access` is rejected.
- Runtime: local Linux, with Git worktrees and the repository-local SQLite v3
  ledger as authority.
- Workspace modes: current checkout, an exact existing linked worktree, or a
  controller-created semantic sibling managed worktree.
- Resume: a new controller and SDK client use `thread_resume` with the durable
  identity and reject any identity change.

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

`docs/reviews/evidence/h3-controller-sentinel.json` retains the real disposable
editing proof: one dispatch, one semantic managed-worktree lease, one stable SDK
thread id across fresh clients, ordered lifecycle events, unchanged protected
paths, successful explicit validation, and result durability before projection.

Desktop visibility, idle wake, remote hosts, permission-profile survival, and
native review remain outside H3. Later milestones must label each as proven,
unsupported, not exposed, or not run; none is inferred from local SDK success.
