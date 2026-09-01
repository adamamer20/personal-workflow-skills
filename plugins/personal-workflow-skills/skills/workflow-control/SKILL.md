---
name: workflow-control
description: Invoke the packaged codex-flow controller for one typed capsule and report its durable status.
---

# Workflow Control

Preflight without mutation: `uv tool list`, `codex-flow --help`, and
`codex-flow tui --help`. If `codex-flow 0.2.0` or its TUI is unavailable, stop
and direct the user to `make install-personal-workflow-skills` in the matching
checkout. This skill never installs or updates state.

Use this skill as the runtime boundary for one decision-ready milestone. The
normal path accepts only the canonical plan path and exact active milestone id;
the controller compiles the typed `codex_flow.contracts.ModelFacingCapsule`
without executing plan text. The explicit `--capsule` form remains a
low-level/testing input. The controller owns workspace identity, repository
routing, the SQLite dispatch authority, lifecycle facts, and serialized
results. Select exactly one hosting mode; never silently substitute the other.

A single planning/controller turn may issue multiple START operations for ready
disjoint milestones. Handoff singularity is per peer and milestone: each gets
one owner and one START, while dependencies and shared mutable surfaces remain
serial.

The controller records one long-lived program integration worktree and a frozen
verified commit SHA before mutable fan-out. Parallel mutable work uses semantic
child lanes in physical sibling worktrees from that exact SHA or an exact
integrated predecessor. Workers commit only their lane; they never integrate
the trunk. After exact-commit review reaches zero promotion-blocking P0/P1, only
the controller/integration owner may integrate the promoted commit, update DAG
readiness and derive successors from the new trunk tip. This cognitive skill
does not run Git operations itself.

## SDK-headless mode

```text
codex-flow control --plan-path /absolute/path/docs/reviews/peer-thread-workflow.md \
  --milestone-id H6-E-W \
  --state-root /absolute/path/checkout --json
```

It plans and starts once. Use `status` later; resume only with a durable SDK
identity and no terminal result:

```text
codex-flow status --run-id RUN --milestone-id MILESTONE \
  --state-root /absolute/path/checkout --json
```

```text
codex-flow control --resume --capsule /absolute/path/capsule.json \
  --state-root /absolute/path/checkout --json
```

## App-native mode

Prepare without starting the SDK:

```text
codex-flow control --hosting app-native \
  --capsule /absolute/path/capsule.json \
  --state-root /absolute/path/checkout --json
```

If state is `prepared`, perform exactly one native, non-blocking Codex app task
creation from the returned action's exact fields. Do not create another
worktree. Build one `HostReceipt` only from that native result and its exact
dispatch id, digest, bind challenge, claim capability, host id, and thread id;
its HMAC proves capability possession, not App attestation. Never guess ids.
Write `receipt.to_json()` unchanged as closed JSON, then bind:

```text
codex-flow app-bind --dispatch-id DISPATCH --claim-token TOKEN \
  --receipt /absolute/path/host-receipt.json \
  --state-root /absolute/path/checkout --json
```

The later host callback ingests one closed terminal JSON result:

```text
codex-flow app-result --dispatch-id DISPATCH --claim-token TOKEN \
  --thread-id THREAD --host-id HOST --result /absolute/path/result.json \
  --state-root /absolute/path/checkout --json
```

Use `codex-flow app-status --dispatch-id DISPATCH --state-root
/absolute/path/checkout --json` for recovery. A `prepared` record means bind
the identity from the already issued action; never create another task merely
because binding was interrupted. A `bound` record means await or ingest its
result. `cancelled` is terminal: late bind/result callbacks are rejected and
cannot reopen it. Other terminal records are idempotent.

Never invent a fallback, attach to a Desktop socket, duplicate state, weaken
protected-surface integrity, or relabel uncertainty. Typed Python is the
authoring surface; JSON/JSONL are serialization only.

The explicit legacy route remains available only when separately selected:
`$codex-thread-handoff`. It is not part of this controller path and must not be
silently mixed with it.
