---
name: reasonix-go
description: Delegate bounded repository exploration, review, debugging, testing, or implementation to DeepSeek V4 Flash through Reasonix and the user's OpenCode Go subscription, including persistent ACP workers with progress, mid-turn steering, cancellation, and follow-up turns. Use when Codex can offload a well-scoped coding workstream, needs an independent external review, wants an interactive external implementer, or should balance usage between Reasonix and native Luna as equivalent coding workers.
---

# Reasonix Go

Resolve the directory containing this `SKILL.md` as `SKILL_ROOT`, then run
`$SKILL_ROOT/scripts/reasonix-go-worker` from the repository root. Treat
Reasonix as an external worker, not a native Codex subagent.

## Choose a mode

- Use `explore` for broad read-only codebase investigation.
- Use `review` for an independent read-only diff review.
- Use `implement` for one narrow implementation workstream.
- Use `interactive` for an implementation workstream that may need mid-turn
  steering, permission choices, or cancellation from the Codex orchestrator.

Use Reasonix's automatic step budget by default (`REASONIX_MAX_STEPS=0`). The
wrapper still enforces a hard wall time of 15 minutes for `review`/`explore`
and 30 minutes for `implement`. Set a positive `REASONIX_MAX_STEPS` only when a
task deliberately needs a stricter round cap.

Keep implementation slices narrow. DeepSeek V4 Flash can spend many tool calls
on broad repository prompts, repeated inspection, and iterative test diagnosis.
Prefer one owned behavior plus focused tests per turn; steer early when tool
events show repeated reading or equivalent test commands. Do not solve tool
inefficiency merely by raising the step cap.

Treat DeepSeek V4 Flash through Reasonix and native Luna as equivalent
general-purpose coding workers. Prefer Reasonix first while OpenCode Go capacity
is available to distribute usage across both providers. For medium or large
tasks, use both when they can own genuinely independent workstreams. Choose
between them based on available capacity and file ownership, not on a presumed
quality or risk hierarchy. Do not duplicate the same implementation merely to
use both systems.

Use multiple Reasonix workers when decomposition justifies it. Codex may launch
several wrapper processes for independent workstreams, and a Reasonix worker
may use Reasonix's own isolated subagent profiles. Count Reasonix and Luna
workers together against the repository's normal concurrency limit; do not
reserve slots for either provider merely to force a mix.

If the wrapper reports `REASONIX_CAPACITY_UNAVAILABLE`, do not retry in a loop.
Route the pending work to a native Luna subagent and report the fallback.

Treat wrapper outcomes distinctly:

- Exit `75` with `REASONIX_CAPACITY_UNAVAILABLE` means an explicit provider
  quota, rate, balance, or subscription failure.
- Exit `76` with `REASONIX_BUDGET_EXHAUSTED` means the agent exhausted its
  tool/step budget. Inspect its partial diff, then continue with a smaller
  owned slice in Reasonix or Luna; do not call it provider capacity.
- Exit `70` with `REASONIX_PROVIDER_STREAM_FAILED` means the provider stream
  ended unexpectedly. Do not follow up on that ACP session; start one fresh
  session at most once, then use Luna if the transport failure repeats.

## Delegate safely

Include in every task:

1. The precise objective and expected response.
2. Relevant files or directories.
3. Whether edits are allowed.
4. Paths that must not change.
5. Required tests or validation.
6. A prohibition on commit and push.

Use this shape:

```bash
"$SKILL_ROOT/scripts/reasonix-go-worker" review \
  "Review the current diff. Report only concrete findings with file and line references. Do not modify files."
```

For an interactive implementation, start:

```bash
"$SKILL_ROOT/scripts/reasonix-go-worker" interactive \
  "Implement the bounded task. Do not commit or push."
```

Launch the shell execution with a PTY (`tty=true`). Codex closes stdin for
non-PTY executions, which makes mid-turn `/steer`, `/respond`, and `/cancel`
messages impossible even though the ACP session remains active.

When the shell tool returns a running process handle, send commands to that
same handle through stdin:

- `/steer <guidance>` queues guidance at Reasonix's next safe model boundary.
- Any non-command line is also treated as steering guidance.
- `/cancel` cancels the active turn but keeps the worker session alive.
- `/respond <request-id> <option-id>` answers a surfaced permission or
  structured-choice request.
- After a `reasonix_result`, send a plain message or `/followup <message>` to
  start another turn with the same Reasonix session and accumulated context.
- `/close` ends the persistent worker and closes its ACP controller.

Keep steering concise and task-local. Use it to clarify or correct the active
workstream, not to add unrelated scope. A successful steer emits
`reasonix_steer_accepted`; do not assume guidance was accepted before seeing
that event.

## Wait for completion

- If the shell execution tool returns a `session_id` without an `exit_code`,
  treat it as the handle of a still-running Codex shell process, not as
  Reasonix output or a Reasonix session ID.
- Poll that same shell-process handle with the execution tool until it returns
  output and an `exit_code`. Do not look for it with `reasonix session list` or
  `reasonix task list`.
- Report bounded progress from each poll. In `implement` mode the wrapper emits
  filtered tool activity, completion events, and the final response while
  dropping streamed reasoning and full tool results. In `review` and `explore`
  modes it forwards native output plus periodic `REASONIX_RUNNING` heartbeats.
- A heartbeat proves only that the local bridge process is alive and waiting.
  It is not evidence of ongoing model progress. Use tool, plan, message, or
  completion events as progress; inspect `activity_idle_seconds` in interactive
  mode before deciding a worker is productive or stalled.
- In `interactive` mode keep the process handle open and poll its ACP updates.
  Treat `reasonix_result` as the end of one turn, not the worker: after the
  following `reasonix_idle`, send a follow-up to reuse the same context or
  `/close` to release the process. Use stdin during a turn for steering,
  permission responses, or cancellation.
- Treat an initial yield or temporarily empty output as still running; never
  infer fallback from silence or a heartbeat. Act only on a terminal marker:
  code 75 routes to Luna, code 70 permits one fresh ACP session before Luna,
  and code 76 requires inspection of the partial diff and a smaller slice.

## Integrate results

- Treat the final answer as evidence, not as automatically correct.
- After `implement`, inspect the complete diff and run the relevant checks
  independently before accepting it.
- Multiple Reasonix, Luna, and orchestrator writers may share one worktree only
  with explicit, disjoint file ownership assigned before they start. Never let
  two writers edit the same file or an undeclared path.
- Assign every shared or generated file to exactly one integration owner. That
  owner may be the orchestrator or one designated worker; all other writers
  must treat those files as read-only.
- Serialize repository-wide mutations such as dependency installation,
  lockfile or code generation, global formatting, migrations, staging, and
  commits. Workers must not commit, push, stage, reset, or clean the worktree.
- Multiple `review`/`explore` workers may share a worktree without ownership
  partitions because they are read-only. Use separate worktrees or serialize
  implementation when ownership cannot be made reliably disjoint.
- After concurrent work, have the orchestrator inspect the combined diff and
  run integration checks before accepting it.
