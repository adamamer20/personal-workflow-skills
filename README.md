# Personal Workflow Skills

Versioned, cross-project Codex workflows owned by Adam Amer. This repository is
both the source of the plugin and an installable Codex marketplace. It packages
planning, milestone execution, event-based peer-thread handoff, and bounded
code-audit instructions without embedding project-specific decisions.

## Lifecycle

```text
new substantial program
        ↓
$plan-work
Sol High planning thread
        ↓
decision-ready canonical plan
        ↓
fresh peer execution thread
        ↓
$execute-milestone
authorized native route selected by task class
        ↓
completion or material escalation
        ↓
$codex-thread-handoff
        ↓
planning thread
```

Peer execution threads are not subagents. The planning thread does not wait for
or poll them. It owns the canonical plan, architecture decisions, milestone
ordering, and program completion. Each fresh execution thread owns exactly one
milestone under normal conditions and sends one material escalation or terminal
outcome when needed.

Project-specific composition remains in each project's `AGENTS.md`. Repository
instructions name the canonical plan path, gates, protected surfaces, and any
exceptions.

## Included skills

Workflow:

- `plan-work`
- `execute-milestone`
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

| Situation | Task context | Native `model` | Native `thinking` |
| --- | --- | --- | --- |
| Small/local change | direct execution | `gpt-5.6-luna` | `high` |
| Decision-ready substantial milestone | fresh execution | `gpt-5.6-luna` | `xhigh` |
| Visual-judgment implementation (slides, landing pages, frontend/UI, rendered documents) | fresh execution | `gpt-5.6-sol` | `medium` |
| New visual direction, ambiguous references, repeated visual failure, or final qualitative promotion review | fresh peer or planning | `gpt-5.6-sol` | `high` |
| Luna implementation/review loop after two unsuccessful repair cycles | fresh execution | `gpt-5.6-sol` | `medium` |
| Sol Medium implementation/review loop after two further unsuccessful repair cycles | fresh execution | `gpt-5.6-sol` | `high` |
| First-time large or uncertain program | planning | `gpt-5.6-sol` | `high` |
| Material architecture escalation | existing planning thread | `gpt-5.6-sol` | `high` |
| Mechanical repair after a precise finding | fresh/current execution | `gpt-5.6-luna` | `high` |
| Difficult unresolved implementation after XHigh | fresh execution | `gpt-5.6-luna` | `max` |
| Independent normal code review | fresh peer | `gpt-5.6-luna` | `xhigh` |
| Critical architecture or security review | fresh peer or planning | `gpt-5.6-sol` | `high` |

Classify by the judgment needed for acceptance, not by file type alone. When
success depends on composition, hierarchy, responsive behavior, rendered
inspection, or other subjective visual judgment, Sol Medium is the default.
Use Sol High for novel or system-wide visual direction, weak or conflicting
references, remediation after repeated visual misses, and the independent final
qualitative promotion review. Luna remains appropriate only when the visual
target and acceptance criteria are already frozen and the remaining work is
mechanical and objectively verifiable, such as bounded wiring, copy replacement,
asset processing, export, or a precisely identified CSS repair.

Luna also has a bounded implementation/review circuit breaker. If two complete
repair and re-review cycles fail to close the same material blocker, or the same
class of finding is reopened, stop iterating in that Luna context. Hand the
current diff, validation evidence, review findings, and remaining acceptance gap
to a fresh Sol Medium execution context. If that Sol Medium continuation also
completes two repair and re-review cycles without closing the blocker, hand the
same bounded evidence forward once to fresh Sol High. If Sol High cannot close
the gate through ordinary repair, return an accurately labelled terminal
`BLOCKED` or `FAILED` outcome to the planning owner; do not create an indefinite
review loop. Each step is a model escalation, not permission to weaken the gate,
silently expand scope, or continue using the exhausted task. A more specific
Sol High route still wins immediately for critical visual, architecture, or
security work.

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

Execution capsules carry the exact planning callback `threadId` and a `hostId`
only when native task tools returned it; environment ids are never substituted
for host ids. The planning task remains unarchived while peers are active. Every
terminal execution outcome attempts one callback: `COMPLETION` when green, or
an accurately labelled `BLOCKED`/`FAILED` escalation otherwise. If delivery is
impossible, the execution task ends with `callback_status: unsent` and the complete
recoverable packet instead of silently disappearing.

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

## Validate

Run `python3 scripts/validate.py` before installing or publishing an update.

## License

MIT. See [`LICENSE`](LICENSE).
