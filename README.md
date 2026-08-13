# Personal Workflow Skills

Versioned, cross-project Codex workflows owned by Adam Amer. The plugin keeps
reusable planning, orchestration, delegation, and code-audit instructions out
of individual application repositories.

Project-specific composition remains in each project's `AGENTS.md`. In
particular, `grill-me-light` and `sol-luna-route` are independent skills:
repository instructions decide when planning hands off to execution and name
the project-owned active plan.

## Included skills

- `grill-me-light`
- `sol-luna-route`
- `reasonix-go`
- `implement-and-adversarial-review`
- `abstraction-opportunity-audit`
- `dead-code-elimination-audit`
- `dedup-naming-audit`
- `fallback-upstream-audit`
- `indirect-attribute-access-audit`
- `overabstraction-audit`
- `strong-typing-audit`

Run `python3 scripts/validate.py` before installing or updating the plugin.
Install from the personal marketplace with:

```bash
codex plugin add personal-workflow-skills@personal
```

Start a new Codex task after installation so the bundled skills are discovered.
