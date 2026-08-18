# Plan: Peer-thread planning and milestone execution

## Goal

Replace parent/subagent orchestration with a planning task that owns one
canonical program plan and fresh peer execution tasks that each own one
decision-ready milestone.

## Decisions

- `plan-work` owns repository inspection, material questions, program
  decomposition, the canonical plan, and initial or successor milestone
  dispatch.
- `execute-milestone` owns one milestone's implementation, tests, ordinary
  repair, self-review, safe local commits, and terminal callback.
- `codex-thread-handoff` is the only transport primitive: START creates one
  native peer task and MESSAGE sends one bounded packet to an existing peer.
- Peer creation uses native `list_projects` and `create_thread`; messaging uses
  `list_threads` and `send_message_to_thread`. No operation silently falls back
  to `fork_thread` or a subagent.
- Planning never polls execution. Material escalation and terminal completion
  are event-based messages to exact thread ids.
- One milestone normally equals one fresh context. Hard rollover occurs after
  two major compactions or, when observable, 75M tokens or 500 model calls.
- The milestone owner may make safe local commits. Independent review is a
  promotion gate only when risk or subjective quality warrants it.

## Scope and non-goals

The workflow skills, UI metadata, plugin metadata, validator, README, and
global `AGENTS.md` template are in scope. The seven audit skills are unchanged.
Persistent orchestration, progress polling, automatic retries, and silent
subagent fallback are out of scope.

## Acceptance and validation

- Plugin inventory contains exactly the three workflow skills and seven audits.
- START encodes the real native project/worktree and queued-creation contract.
- Planning and execution ownership, escalation, completion, rollover, Git
  safety, subjective-quality, production-reachability, and parity contracts are
  deterministically validated.
- `python3 -B scripts/validate.py`, all three skill quick validators, JSON
  parsing, deprecated-term search, audit-skill unchanged checks, and
  `git diff --check` pass.

## Current review log

- 2026-08-18 — Implemented the peer-thread workflow and removed the legacy
  Direct/Lite/Full routing architecture.
- 2026-08-18 — Integrated the previously merged `codex-notify-thread` work by
  superseding it with the START/MESSAGE `codex-thread-handoff` contract.
