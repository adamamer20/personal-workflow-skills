# Dead Code Removal Rubric

Use this rubric to decide what to delete now versus defer.

## Confidence Tiers

### High (safe to remove now)

- Symbol has zero static usages in scoped code.
- Symbol is private (`_name`) or test-only.
- Symbol is not exported via `__all__`.
- Symbol is not in an `__init__.py` package API module.

Action: remove in small slices, run checks immediately.

### Medium (remove with extra checks)

- Symbol has zero static usages, but is public (`name`) in non-test modules.
- No direct imports found, but external callers are possible.

Action: search wider for string/import references and integration points before removal.

### Low (defer unless explicitly approved)

- Symbol is in `__init__.py`.
- Symbol appears in `__all__`.
- Symbol may be reached by reflection, plugin loading, dependency injection,
  framework routing, or CLI entrypoint mechanisms.

Action: keep unless user requests breaking cleanup and accepts risk.

## Hard Blocklist (do not auto-delete)

- Framework hook names (`startup`, `shutdown`, `handle`, `on_*`, etc.)
- Entry-point functions (`main`, console-script targets)
- Registration-driven components (plugin maps, registries, factories)
- Objects loaded by string path (`"package.module:obj"` patterns)

## Validation Gates

Run after each deletion slice:

1. The narrowest test or static check covering the removed code.
2. The repository's documented full check before closure.

Stop and revert the last slice on failures.
