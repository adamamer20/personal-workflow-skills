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
Luna XHigh
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

Recommended conceptual routing:

| Situation | Task context | Model |
| --- | --- | --- |
| Small/local change | direct execution | Luna High |
| Decision-ready substantial milestone | fresh execution | Luna XHigh |
| First-time large or uncertain program | planning | Sol High |
| Material architecture escalation | existing planning thread | Sol High |
| Mechanical repair after a precise finding | fresh/current execution | Luna High |
| Difficult unresolved implementation after XHigh | fresh execution | Luna Max |
| Independent normal code review | fresh peer | Luna XHigh |
| Critical architecture or security review | fresh peer or planning | Sol High |

These are task-level routing defaults, not a parent/subagent configuration.
Subagents remain optional tactical helpers rather than the workflow foundation.
The skills cannot claim to change a current task's model if the runtime does not
expose that operation.

## Validate

Run `python3 scripts/validate.py` before installing or publishing an update.

## License

MIT. See [`LICENSE`](LICENSE).
