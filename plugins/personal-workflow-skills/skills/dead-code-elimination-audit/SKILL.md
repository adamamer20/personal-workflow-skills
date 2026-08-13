---
name: dead-code-elimination-audit
description: Identify and safely remove legacy dead code that has no real call path, no imports, and no runtime use evidence. Use when asked to clean up unused functions/classes/modules, shrink maintenance surface, reduce stale code risk, or perform aggressive dead-code reduction with safety checks in Python codebases.
---

# Dead Code Elimination Audit

## Goal

Remove as much truly unused legacy code as possible without changing behavior.

## Workflow

1. Define scope and safety level.
- Discover and confirm the repository's source and test roots.
- Pick mode:
  - `conservative`: remove only high-confidence dead code.
  - `aggressive`: include medium-confidence candidates after stronger checks.

2. Build a dead-code candidate set.
- Resolve the directory containing this `SKILL.md` as `SKILL_ROOT`.
- Run the bundled analyzer against the discovered roots:
  - `python3 "$SKILL_ROOT/scripts/find_unreferenced_python_symbols.py" <source-root> <test-root>`
- Add repository evidence:
  - `rg -n "<symbol_name>" <source-root> <test-root>`
- Include module-level dead files when no imports or runtime entrypoints exist.

3. Classify each candidate.
- Use `references/rubric.md` for confidence tiers and blocklist rules.
- Exclude candidates with dynamic usage indicators (`getattr`, plugin registries, reflection, framework hooks, CLI entrypoints, `__all__` exports).

4. Remove code in smallest safe slices.
- Delete one candidate group at a time (single module or tightly related symbols).
- Keep API surface stable unless user asks for breaking cleanup.

5. Validate after every slice.
- Use the focused and repository-wide checks documented by the project.
- If checks fail, restore that slice and downgrade confidence rules.

6. Report evidence-first results.
- List removed symbols with file references.
- List retained candidates with reason (`dynamic usage risk`, `public API`, `insufficient evidence`).
- If asked for a review only, present findings ordered by risk and removal confidence.

## Output Contract

Return results in this order:
1. Removed dead code (with file paths)
2. Remaining candidates and blockers
3. Validation commands run and outcomes

## Resources

- Analyzer script: `scripts/find_unreferenced_python_symbols.py`
- Decision rubric: `references/rubric.md`

Load `references/rubric.md` before making final deletion calls.
