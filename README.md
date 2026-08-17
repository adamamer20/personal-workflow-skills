# Personal Workflow Skills

Versioned, cross-project Codex workflows owned by Adam Amer. This repository is
both the source of the plugin and an installable Codex marketplace. It keeps
reusable planning, orchestration, delegation, and code-audit instructions out
of individual application repositories.

Project-specific composition remains in each project's `AGENTS.md`. In
particular, `grill-me-light` and `sol-luna-route` are independent skills:
repository instructions decide when planning hands off to execution and name
the project-owned active plan.

## Included skills

- `grill-me-light`
- `codex-notify-thread`
- `sol-luna-route`
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

[`templates/AGENTS.md`](templates/AGENTS.md) contains the generic routing rules
used to compose `grill-me-light` with `sol-luna-route`. Codex reads global
instructions from `$CODEX_HOME/AGENTS.md`, or `~/.codex/AGENTS.md` when
`CODEX_HOME` is unset. Review and merge the template with an existing global
file; do not overwrite personal instructions blindly.

The plugin installer intentionally does not modify global instructions. This
keeps skill installation and global routing as separate trust decisions, while
repository `AGENTS.md` files remain responsible for project-specific plan
paths, gates, and exceptions. See the official [Codex AGENTS.md
documentation](https://developers.openai.com/codex/guides/agents-md/) for the
instruction precedence rules.

`sol-luna-route` controls spawned roles and checkpoints; it cannot change the
current main-thread model or reasoning effort mid-task and must not pretend it
did. Users who want Sol to default to Medium may set that preference in their
own Codex configuration or profile; the plugin never edits configuration
automatically. The route selects the smallest safe mode, so a Git Committer,
Explorer, verifier, and final reviewer are conditional rather than mandatory.

An optional user-level baseline for this workflow is:

```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "medium"

[agents]
enabled = true
max_concurrent_threads_per_session = 20
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "high"
```

Set `agents.max_concurrent_threads_per_session` only as a capacity ceiling;
the skill parallelizes genuinely independent packages and does not treat free
slots as a reason to create roles. See the official [Codex configuration
reference](https://developers.openai.com/codex/config-reference/) and
[subagent guide](https://developers.openai.com/codex/subagents/).

## Validate

Run `python3 scripts/validate.py` before installing or publishing an update.

## License

MIT. See [`LICENSE`](LICENSE).
