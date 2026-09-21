# Personal Workflow Skills

Versioned, cross-project Codex workflows owned by Adam Amer. This repository is
both the source of the plugin and an installable Codex marketplace. It packages
typed planning, controller-backed milestone execution, an explicit legacy
peer-thread route, and bounded code-audit instructions without embedding
project-specific decisions.

## Python SDK controller

The installable `codex-flow` harness is directly callable by a Codex agent. Its
canonical H3 vertical slice is:

```bash
codex-flow plan --capsule /absolute/path/to/capsule.json --state-root /absolute/checkout --json
codex-flow start --run-id RUN --milestone-id MILESTONE --state-root /absolute/checkout --json
codex-flow resume --run-id RUN --milestone-id MILESTONE --state-root /absolute/checkout --json
codex-flow status --run-id RUN --milestone-id MILESTONE --state-root /absolute/checkout --json
codex-flow cancel --run-id RUN --milestone-id MILESTONE --state-root /absolute/checkout --json
codex-flow control --capsule /absolute/path/to/capsule.json --state-root /absolute/checkout --json
codex-flow schema --kind all
codex-flow diagnostics --help
codex-flow live conversation --subject-kind worker --subject-id DISPATCH \
  --thread-id THREAD --generation 1 --attempt 1 --json
CODEX_FLOW_REAL_SDK=1 codex-flow workflow-control-pilot --real --model gpt-5.6-luna --effort medium
```

The root help keeps the day-to-day surface to `tui`, `control`, `status`,
`cancel`, `harness`, `live`, `schema`, and `diagnostics`. Compatibility
root spellings for low-level, sentinel, and pilot commands remain callable but
are hidden from help; new diagnostics use the `codex-flow diagnostics` group.
The native conversation command reads one bounded redacted page through the
existing harness/SDK worker path and never persists raw provider output.

Diagnostic pilot and sentinel commands are `review-pilot`,
`multi-authority-review-pilot`, `workflow-control-pilot`, `production-pilots`,
`sdk-compatibility-sentinel`, `controller-recovery-sentinel`, and
`worker-sentinel`. Run them as, for example,
`codex-flow diagnostics sdk-compatibility-sentinel --help`.

The capsule selects `current_checkout`, `existing_worktree`, or
`managed_worktree`. Managed paths are always semantic siblings at
`<repo>.worktrees/<lane>` with branch `agent/<lane>`; the same program/lane
lease is reused across sequential milestones and fresh-process recovery.
This naming rule applies to controller-created managed worktrees. A
Codex-managed checkout already bound to a task may be represented as
`existing_worktree` at its exact `$CODEX_HOME/worktrees/...` path. The capsule
keeps its integration `repository_root` distinct from its mutable
`workspace_path`; saved-project registration is only native START addressing.
SQLite owns capsule digests, workspace leases, dispatch/thread/turn identity,
ordered SDK lifecycle events, immutable native compatibility identity,
monotonic effective permission authority, validation, and terminal results.
On resume, a newly stricter native policy is durably rebound before the SDK
call, while a newly broader policy cannot broaden the execution. The controller
uses the published `openai-codex` Python SDK as its only Codex transport and
never falls back to the CLI or direct app-server RPC.

`$workflow-control` is the packaged agent entrypoint: it invokes
`codex-flow control` for exactly one typed capsule and reports the durable
checkpoint, result, validation facts, and protected-surface evidence. Planning
authors use `codex_flow.contracts.ModelFacingCapsule`; executors return one
`ModelFacingResult`. JSON/JSONL is only controller serialization. The default
planning and execution skills describe cognitive roles and acceptance modes;
runtime lifecycle policy lives in the controller and `workflow-control` skill.

The explicit legacy command `$codex-thread-handoff` remains reachable through
H6 for compatibility evidence. It is selected deliberately and is never mixed
with the controller path for one milestone.

One planning/controller turn may start multiple ready milestones when their
mutable surfaces are disjoint. The one-start/one-owner idempotency rule is per
peer and milestone; serial dependencies and shared mutable ownership remain
ordered.

Run the opt-in real crash/resume sentinel only in a disposable repository:

```bash
CODEX_FLOW_REAL_SDK=1 uv run codex-flow controller-recovery-sentinel \
  --real --model gpt-5.6-luna --effort medium \
  --output docs/reviews/evidence/h3-controller-sentinel.json
```

## Lifecycle

```text
intent and bounded outcome
        ↓
$plan-work: material risks → smallest discovery proof
        ↓
first feasible production vertical → observe → justified architecture
        ↓
one ready milestone → $workflow-control → $execute-milestone
        ↓
compact result → risk-triggered promotion review → integration
        ↓
controller decision only when needed

explicit compatibility comparison only → $codex-thread-handoff
```

Resolve material uncertainty before designing the whole system. A prerequisite
foundation must name the concrete obstacle to a complete first vertical. Freeze
one deliverable and non-goals; a valid alternative output must not fail merely
because the benchmark expects its favorite fixture. Required safety remains
binding; challenge an expensive new guarantee against its threat and boundary.

After two attempts without new causal evidence or observable improvement, diagnose
oracle, scope, implementation or environment before another repair. The controller
owns any rollover/model decision, reusing the workspace; work/context changes
matter before numeric thresholds. Keep results to outcome, candidate, important
checks, changed contracts, blocker and remainder, with required evidence linked.
Review is proportional to concrete risk and re-review targets changed findings.
Focused checks guide implementation; affected gates close a milestone and the
appropriate full gate validates the integrated candidate before merge. Reuse
successful unchanged checks. Track first vertical, mergeability, merge, waits,
known cost and reopenings in history without another measurement platform.

The planner owns intent, decomposition, acceptance, and protected surfaces. The
executor owns implementation inside one capsule and writes one typed result.
The controller owns runtime lifecycle, workspace identity, durable evidence,
and status. The legacy handoff remains available for explicit compatibility
work only; it is never silently combined with the controller path.

A fresh execution thread is a model-context boundary, not a Git-workspace
boundary. The plan selects one execution workspace for the program or mutable
lane and reuses it across sequential milestones, context rollover, repair,
recovery, model changes, and read-only review. A new worktree is reserved for
concurrent mutable ownership, protection of pre-existing user changes, or an
explicitly isolated experiment.

Managed worktrees for `<parent>/<repo>` live at
`<parent>/<repo>.worktrees/<program-slug>` or, for a parallel lane,
`<parent>/<repo>.worktrees/<program-slug>-<lane-slug>`, with a matching
`agent/<slug>` branch. Thread ids, client ids, model names, and bare milestone
numbers are not workspace identities. `plan-work` chooses the topology;
`codex-thread-handoff` only launches into the exact selected workspace and fails
closed when the native API cannot address it.

An already-running task's Codex-managed worktree remains authoritative for its
retained dirty state even when that path is not a saved project. Fresh
`create_thread` addressability, current-task workspace ownership, and the later
integration checkout are separate facts.

Keep a lightweight current plan and a separate history document. The current
plan selects outcome, risks, readiness, ownership and next gate. History retains
closed decisions and evidence; it never authorizes dispatch. Link any retained
design that still governs current work, preserving capsule identities.

Project-specific composition remains in each project's `AGENTS.md`. Repository
instructions name the canonical plan path, gates, protected surfaces, and any
exceptions.

## Included skills

Workflow:

- `plan-work`
- `execute-milestone`
- `workflow-control`
- `codex-thread-handoff`

Audits:

- `abstraction-opportunity-audit`
- `dead-code-elimination-audit`
- `dedup-naming-audit`
- `fallback-upstream-audit`
- `indirect-attribute-access-audit`
- `overabstraction-audit`
- `strong-typing-audit`

## Install from GitHub

To install or update both the current checkout's `codex-flow` tool and matching
plugin in the standard shared user environment, run the explicit bootstrap:

```bash
make install-personal-workflow-skills
```

It installs `codex-flow` with CPython 3.12 through `uv tool`, registers this
checkout as the local `adam-workflows` marketplace, installs and enables
`personal-workflow-skills`, then verifies version, CLI/TUI help, regular-file
topology, and plugin byte parity. Repeating the command is safe. It rejects a
private `CODEX_HOME`, custom uv tool paths, mismatched versions, and substituted
marketplace/plugin paths. It does not copy credentials, edit `AGENTS.md`, start
a provider or service, or install automatically from a hook. Start a new Codex
task after success so the updated plugin is discovered.

For a source marketplace without installing the Python controller, use the
manual plugin-only commands below.

Register the public marketplace and install the plugin:

```bash
codex plugin marketplace add adamamer20/personal-workflow-skills --ref main
codex plugin add personal-workflow-skills@adam-workflows
```

The first command teaches Codex where to refresh this marketplace. The second
installs and enables the workflows. Start a new Codex task afterwards so the
new skills are discovered.

To inspect the source before registering it, clone the repository and use the
local checkout as the marketplace:

```bash
git clone https://github.com/adamamer20/personal-workflow-skills.git
codex plugin marketplace add ./personal-workflow-skills
codex plugin add personal-workflow-skills@adam-workflows
```

Refresh a previously registered Git marketplace with:

```bash
codex plugin marketplace upgrade adam-workflows
codex plugin add personal-workflow-skills@adam-workflows
```

Codex intentionally does not auto-install plugins merely because a repository
was cloned. Marketplace registration and plugin installation are explicit trust
decisions. Do not hand-edit `~/.codex/config.toml`; use the commands above.

## Optional global routing instructions

[`templates/AGENTS.md`](templates/AGENTS.md) contains generic routing rules for
the three workflow skills. Codex reads global instructions from
`$CODEX_HOME/AGENTS.md`, or `~/.codex/AGENTS.md` when `CODEX_HOME` is unset.
Review and merge the template with an existing global file; do not overwrite
personal instructions blindly.

The plugin installer intentionally does not modify global instructions. This
keeps skill installation and routing as separate trust decisions. See the
official [Codex AGENTS.md documentation](https://developers.openai.com/codex/guides/agents-md/)
for instruction precedence.

Authorized native routing defaults:

The Sol-first policy keeps routine planning and specialist judgment on Sol Medium
and uses Sol High for escalation and recovery. Astra is a break-glass override,
not a configured default: it requires either an explicit user request or two Sol
High attempts on the same failure without new causal evidence or observable
improvement. Runtime capability proof remains separate from changing a configured
model name. Existing tasks retain their pinned routes; these defaults apply to
newly dispatched work.

| Situation | Task context | Native `model` | Native `thinking` |
| --- | --- | --- | --- |
| Small/local change | direct execution | `gpt-5.6-luna` | `high` |
| Decision-ready substantial milestone | fresh execution | `gpt-5.6-luna` | `xhigh` |
| Visual-judgment implementation (slides, landing pages, frontend/UI, rendered documents) | fresh execution | `gpt-5.6-sol` | `medium` |
| Ordinary recovery implementation | fresh execution | `gpt-5.6-sol` | `high` |
| Independent visual-quality promotion review, when required | fresh peer | `gpt-5.6-sol` | `medium` |
| First-time large or uncertain program | planning | `gpt-5.6-sol` | `medium` |
| Architecture conformance, when risk-triggered | fresh peer or planning | `gpt-5.6-sol` | `medium` |
| Recovery diagnosis | fresh peer or planning | `gpt-5.6-sol` | `high` |
| Controller decisions | ephemeral controller turn | `gpt-5.6-sol` | `medium` |
| Significant/ambiguous architecture or security escalation | fresh peer or planning | `gpt-5.6-sol` | `high` |
| Mechanical repair after a precise finding | fresh/current execution | `gpt-5.6-luna` | `high` |
| Independent objective/code review, when risk-triggered | fresh peer | `gpt-5.6-luna` | `xhigh` |

Classify every milestone by one or more acceptance modes: `objective`, `visual`,
and `architecture`. Controller capsules require every declared review authority.
Select the smallest sufficient modes/lenses from concrete contract, ownership,
security/privacy, migration, integration, prior-finding or visual risk before
dispatch; do not skip declared authorities or combine them silently afterwards.
Only small reversible work on the direct path outside workflow-control can close
by validation/self-review without a reviewer when no risk trigger applies. A changed
file or configured role is not by itself a review trigger for that direct path.

Declared reviews are sequenced by candidate maturity. Objective and visual
authorities run first and may trigger the single bounded repair. Architecture is
a late promotion gate: it is dispatched only after every declared
non-architecture authority is green on the exact candidate. A repair after an
architecture finding creates a successor that must regain those earlier
approvals before architecture re-review. Architecture is omitted entirely when
the milestone does not materially exercise a boundary, public/persisted
contract, ownership, storage/concurrency model, security/privacy boundary,
execution topology, or structural dependency.

When selected, objective code review uses Luna XHigh. When success
depends on composition, hierarchy, responsive behavior, rendered inspection, or
other subjective visual judgment, visual implementation and independent
visual-quality review default to Sol Medium. Ordinary recovery defaults to Sol High.
Normal architecture conformance and semantic orchestration use Sol Medium. A
combined architecture/security review starts with Sol Medium and escalates to
Sol High only when the first pass cannot close the question and records the
unresolved risk, evidence checked and reason. Astra is used only when explicitly
requested by the user or after two Sol High attempts on the same failure without
new causal evidence or observable improvement.
Domain labels, severity, finding count and previous reviewer identity alone do
not justify escalation. Retaining a higher-cost reviewer for continuity is optional;
re-review is about the repaired findings. Record authorized routing changes before
dispatch and preserve declared authorities. This is conditional escalation, not
three mandatory reviews. High is an explicit exceptional escalation only. Do not duplicate objective and architecture reviews unnecessarily when
selecting modes; every declared capsule authority remains required. Use one focused lens
when it resolves the recorded risk. Use distinct authorities only for materially
independent evidence. A mixed objective/visual milestone must preserve both kinds
of evidence. Luna remains appropriate when the visual target is frozen
and the remaining work is mechanical and objectively verifiable, such as bounded
wiring, copy replacement, asset processing, export, or a precisely identified
CSS repair.

Non-convergence triggers diagnosis and a change of authority or approach, not a
terminal condition. A Sol High recovery owner receives the current diff,
validation evidence, findings, accepted intent, and remaining gap. It finishes a
bounded repair, replaces a failed implementation strategy, or replans and
continues when outcome, public/persisted contracts, security/privacy boundary,
material cost, destructive behavior, and scope remain within accepted intent.
`CONTINUE_WITH_REPLAN` is internal and nonterminal. Escalate as
`NEEDS_DECISION` only when user intent is genuinely underdetermined or new user
authority is required; use `EXTERNAL_BLOCKED` only for a missing external
prerequisite and `FAILED` only when the goal is not reasonably achievable under
accepted constraints. Difficulty or a disproven plan alone never summons the
user.

These pairs are applied to native `create_thread` only when an explicit user
request or applicable user-owned `AGENTS.md` policy authorizes them. A plugin
installation alone is insufficient authorization. The handoff checks that
the current native schema advertises both fields and the exact values, passes
both in one logical START, and fails closed on unsupported or rejected routes
without retrying, substituting another model, or silently using the configured
default. Error text alone never proves non-creation: after any failed-looking
result, the handoff takes one non-waiting `list_threads` reconciliation snapshot
and never calls `create_thread` again. No authorization means no overrides and
an explicit “routing not enforced” status. A returned thread id or queued
`clientThreadId` confirms dispatch; model enforcement is confirmed only when the
native response or tool contract acknowledges the exact pair.
Luna Max escalation is outside these template defaults and requires a separate
explicit user authorization plus schema support.
Subagents remain optional tactical helpers rather than the workflow foundation.
The skills cannot change the model of a task that is already running.

The handoff capsule also carries `current_checkout`, `existing_worktree`, or
`managed_worktree` plus the exact repository/path and any branch/base/lane
identity. Native project lookup must resolve that exact path only for a fresh
START. It is not consulted to decide whether an existing task owns its bound
worktree. The handoff never defaults every Git task to a new worktree and never
substitutes another directory when the selected existing workspace is
unavailable.

Execution capsules carry the exact planning callback `threadId` and a `hostId`
only when native task tools returned it; environment ids are never substituted
for host ids. The planning task remains unarchived while peers are active. Every
terminal execution outcome attempts one callback: `COMPLETION` when green, or
an accurately labelled `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, or `FAILED`
escalation otherwise. Replanning within accepted intent continues automatically.
If delivery is impossible, the execution task ends with
`callback_status: unsent` and the complete recoverable packet instead of
silently disappearing.

Runtime recovery is deliberately retry-free. A failed or uncertain START can be
reconciled later with one read-only snapshot; it is never silently recreated. If
an existing task's last turn is `interrupted`, one explicit recovery operation
resumes that same thread and worktree after inspecting durable state. If work is
complete but its callback is missing, the same thread republishes the existing
terminal packet without rerunning or changing it. Active work is left alone, and
a replacement task requires separate user authorization after the old owner is
proven unavailable or terminal. This handles recoverable client/server and
interruption cases without creating duplicate owners; it cannot guarantee an
automatic callback when the runtime dies before the task can send one.

When the task is inactive and the context itself is degraded, one explicit
same-directory rollover may fork the task and seed the child with the frozen
workspace/capsule facts. The source relinquishes mutable ownership only after
that seed is delivered. A latest-turn 503 is neither necessary nor sufficient;
active work is never forked and project registration is irrelevant to this
same-directory continuation.

## Native handoff lifecycle hooks

The plugin bundles synchronous `PreToolUse`, `PostToolUse`, `Stop`, and
`SessionStart` hooks in the default `hooks/hooks.json` path. They guard native
`create_thread` and `list_threads` calls with a small retry-free state machine:

- before create, only an unambiguous top-level `projectId` duplicate may be
  moved into a complete project `target`; project environments are strictly
  `local` or `worktree`, and top-level-only ids are denied;
- an atomic `PLUGIN_DATA` ledger stores a bounded title/target/model/thinking
  fingerprint and prompt digest, never the prompt body or raw tool response;
- same-turn, unresolved, and terminally unsafe duplicate creates are blocked,
  including changed payloads in the same session; ledger saturation never
  evicts recovery-blocking state;
- `threadId` is confirmed, `clientThreadId` is queued, and every other result is
  uncertain/error and permits at most one exact read-only `list_threads`
  reconciliation; success requires exactly one native identity shape and ids
  cannot contain Unicode whitespace or control/format/surrogate characters;
- reconciliation is classified as found, not-found, or ambiguous using exact
  title plus explicit complete target identity; only a supported, bounded list
  snapshot can reconcile and only its valid empty form can prove not-found;
  supported envelopes contain only `threads`, canonical `pinnedThreads` plus
  `threads`, or one retained legacy `items`/`results` collection; additional
  status/flag/error/metadata keys, mixed shapes, oversized snapshots, invalid
  `threadId` values, missing/lossy identity, title-prefix collisions, and
  normalization-only matches remain terminally ambiguous; hooks never call
  tools, poll, retry, unarchive, replace, switch models, or send messages;
- unresolved recovery is surfaced once at `Stop` and once on same-session
  `resume`, with `stop_hook_active` honored to prevent continuation loops.

These are non-managed command hooks. Codex requires explicit review and trust
of the exact current hook definitions (for example, through `/hooks`) before
they run; installing or enabling the plugin alone does not grant that trust.
Internal hook failures fail open with a warning. Review the hook source and its
privacy boundary before trusting it, and keep `PLUGIN_DATA` private.

## Validate

Run `python3 scripts/validate.py` before installing or publishing an update.

## License

MIT. See [`LICENSE`](LICENSE).
