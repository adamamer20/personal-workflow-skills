---
name: workflow-control
description: Invoke the packaged codex-flow controller for one typed capsule and report its durable status.
---

# Workflow Control

Use this skill as the runtime boundary for one decision-ready milestone. It
accepts the typed `codex_flow.contracts.ModelFacingCapsule` authoring contract.
The controller owns workspace identity, repository routing, the SQLite
dispatch authority, lifecycle facts, and the serialized result. Select exactly
one hosting mode; never silently substitute the other.

## SDK-headless mode

```text
codex-flow control --capsule /absolute/path/capsule.json \
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

If state is `prepared`, perform exactly one
native, non-blocking Codex app task creation using the returned action's exact
`model`, `reasoning_effort`, `workspace_path`, `prompt`, and `output_schema`.
Do not create another worktree. The App host is the trusted identity authority:
construct one `HostReceipt` only from the
actual native action result and the action's dispatch id, digest, bind
challenge, and claim capability. Its HMAC proves capability possession, not
Codex App attestation. Never use guessed, user-supplied, or prior-task ids:

```python
from codex_flow.app_native import AppNativeTaskAction, HostIdentity, HostReceipt
from codex_flow.domain import ThreadIdentity

action = AppNativeTaskAction.from_json(prepared["action"])
receipt = HostReceipt.from_native_result(
    action,
    HostIdentity(actual_native_host_id, ThreadIdentity(actual_native_thread_id)),
)
```

Both `actual_native_*` values must come directly from that native tool result.
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
