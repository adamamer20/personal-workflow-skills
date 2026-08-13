---
name: fallback-upstream-audit
description: Audit fallback logic across a repository, especially guessed defaults, inferred identifiers, silent coercions, downstream data cleanup, and consumer-side repair code. Use when a system is compensating for missing or dirty upstream data, when fallback-heavy code obscures the canonical source of truth, or when Codex should remove unnecessary fallbacks and push the fix upstream into parsing, ETL, normalization, or canonical storage.
---

# Fallback Upstream Audit

Audit fallbacks as architecture, not just syntax. Prefer fixing canonical data production over adding defensive repair logic at consumer boundaries.

## Workflow

1. Find fallback sites.
   Search for patterns such as `or`, `if x is None`, default literals, guessed identifiers, parsing from display strings, or multi-field coalescing.

2. Classify each fallback.
   Keep only:
   - safety fallbacks required by external input boundaries
   - explicit presentation defaults that do not change domain meaning
   - output guardrails that enforce API invariants

   Flag for removal:
   - domain inference from opaque IDs or filenames
   - consumer-side repair of canonical data
   - silent coercions that hide upstream defects
   - duplicated normalization spread across layers

3. Identify the owning upstream layer.
   Ask where the field should become canonical:
   - parser or source adapter
   - ETL or delta builder
   - canonical storage model
   - projection/read model
   - API serializer

   Prefer the earliest honest layer that has enough information to normalize the field once.

4. Fix upstream first when possible.
   Normalize the canonical field in the owning layer, then simplify or delete the downstream fallback.

5. Preserve only minimal downstream guardrails.
   If a downstream fallback remains, keep it explicit, narrow, and easy to detect in tests.

6. Validate with real data.
   Rebuild or reproject representative artifacts, then verify the consumer no longer depends on the removed fallback.

## Heuristics

- If a consumer reconstructs domain identity from an opaque ID, that is usually a bug.
- If multiple layers clean the same field differently, move the cleanup upstream and keep one canonical representation.
- If a fallback changes domain semantics, it does not belong in a read model.
- If a fallback only prevents invalid public output shape, it may stay as a guardrail, but do not let it justify bad canonical data.

## Output Expectations

When using this skill, produce:
- the fallback sites found
- which ones are legitimate guardrails versus architectural debt
- the upstream layer that should own the fix
- the concrete code changes needed
- the validation path proving the fallback is no longer required
