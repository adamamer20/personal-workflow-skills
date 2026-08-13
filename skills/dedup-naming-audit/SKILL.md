---
name: dedup-naming-audit
description: Detect repeated code paths and confusingly similar names, then propose safe extraction and renaming plans with concrete file/line evidence. Use when asked to deduplicate code, reduce copy-paste logic, identify reusable components, or clarify naming in Python services and pipelines.
---

# Dedup Naming Audit

## Goal

Produce a code-health audit focused on:
- Deduplication opportunities with high confidence
- Naming collisions that increase ambiguity
- Safe, incremental refactor plans

Prioritize behavioral safety over aggressive DRY changes.

## Workflow

1. Scope the review target.
- Read the user request and extract explicit constraints.
- Prefer the files currently open in the IDE plus directly related modules.

2. Gather evidence quickly.
- Use `rg` for repeated patterns (method bodies, SQL blocks, helper names).
- Read candidate files with line numbers.
- Confirm at least one concrete duplicate before proposing extraction.

3. Classify duplicates.
- Mark as `exact`, `structural`, or `semantic`.
- `exact`: near copy-paste with trivial literal differences.
- `structural`: same control flow with parameterizable differences.
- `semantic`: same responsibility expressed via different code.

4. Evaluate extraction safety.
- Confirm common preconditions/postconditions are identical.
- Identify required extension points (callbacks, strategy objects, typed params).
- Reject extraction when it would obscure domain intent.

5. Audit naming clarity.
- Detect near-colliding names with different semantics.
- Detect overloaded names used for different layers (API vs DB vs file paths).
- Propose replacements that encode role and scope.

6. Report findings with actionability.
- List findings first, ordered by severity/impact.
- Include file references with line numbers.
- Provide a minimal refactor sequence that can be applied incrementally.

## Dedup Heuristics

- Start with high-payoff duplicates:
  - Artifact-processing pipelines repeated across modules
  - Repeated SQL fragments or migration helpers
  - Repeated parsing and normalization helpers
- Avoid premature abstraction:
  - Keep one-off domain logic local
  - Keep extraction only when at least two call sites benefit now

## Naming Heuristics

- Prefer names that encode intent, not implementation detail.
- Keep one term per concept across the module set.
- Separate similar but distinct terms explicitly:
  - Example pattern: `source_db_path` vs `cache_db_path` when they have different ownership.
- Rename private helpers if they shadow public shared helpers with only underscore differences.

## Output Format

Return results in this order:
1. Findings (highest impact first)
2. Open questions/assumptions
3. Refactor plan (smallest safe sequence)

For each finding, include:
- What is duplicated or ambiguous
- Why it matters (risk, maintenance cost, drift)
- Recommended extraction/rename target
- Affected file reference(s)

## Repository defaults

- Use the repository's documented command interface.
- Use `rg` for search and `nl -ba` for line-anchored evidence.
- Keep changes typed and modular; fail fast on invalid data.

Load `references/rubric.md` when deciding whether to merge code paths or keep them separate.
