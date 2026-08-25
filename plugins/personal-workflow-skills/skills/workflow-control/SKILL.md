---
name: workflow-control
description: Invoke the packaged codex-flow controller for one typed capsule and report its durable status.
---

# Workflow Control

Use this skill as the runtime boundary for one decision-ready milestone. It
accepts the typed `codex_flow.contracts.ModelFacingCapsule` authoring contract
and its repository-bound controller capsule projection. The controller owns
workspace identity, runtime configuration, durable lifecycle facts, and the
serialized result.

Run the packaged entrypoint from the selected checkout:

```text
codex-flow control --capsule /absolute/path/capsule.json \
  --state-root /absolute/path/checkout --json
```

The command plans once, starts once, and prints the durable status. Inspect the
JSON result and the repository outcome together. Use the read-only command
below for a later status check:

```text
codex-flow status --run-id RUN --milestone-id MILESTONE \
  --state-root /absolute/path/checkout --json
```

If status explicitly reports a durable SDK identity without a terminal result,
resume only as an intentional follow-up:

```text
codex-flow control --resume --capsule /absolute/path/capsule.json \
  --state-root /absolute/path/checkout --json
```

Do not invent a fallback command, duplicate durable state, or relabel an
uncertain status as success. The returned `result`, checkpoint, validation
facts, protected-surface digests, and observable repository change are the
evidence. Keep the typed Python model as the authoring surface; JSON/JSONL is
serialization only.

The explicit legacy route remains available only when separately selected:
`$codex-thread-handoff`. It is not part of this controller path and must not be
silently mixed with it.
