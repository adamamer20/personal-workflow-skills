---
name: abstraction-opportunity-audit
description: Find and prioritize safe abstraction opportunities in codebases, including abstract base classes, protocols, strategy objects, and reusable mini-framework seams. Use when asked to reduce duplication, standardize behavior across concrete implementations, introduce extension points, or design a shared architecture without breaking behavior.
---

# Abstraction Opportunity Audit

## Goal

Identify where shared abstractions improve maintainability and consistency, then
propose incremental refactors with concrete evidence and low regression risk.

## Workflow

1. Scope the target surface.
- Read user constraints and language/framework boundaries.
- Focus on files in scope plus direct call-path dependencies.

2. Collect evidence.
- Use `rg` to find repeated branches, lifecycle methods, and repeated adapters.
- Capture file/line anchors for each candidate.
- Confirm at least two real call sites before proposing extraction.

3. Classify the opportunity.
- `template-method`: shared algorithm skeleton with overridable steps.
- `strategy`: same orchestration, different interchangeable behavior.
- `protocol-interface`: callers depend on capability, not concrete class.
- `module-framework`: repeated pipeline/stage lifecycle across modules.

4. Test abstraction safety.
- Verify shared invariants, error handling, and return contracts match.
- Define extension points explicitly (hooks, policies, typed callbacks).
- Reject abstraction when only superficial similarity exists.

5. Produce an incremental plan.
- Start with narrow extraction used by existing code paths.
- Keep behavior and public API stable until final cleanup step.
- List migration checkpoints and tests for each step.

## Decision Heuristics

- Extract only when at least two active use cases benefit now.
- Prefer `Protocol` for consumer-facing contracts and test doubles.
- Prefer abstract classes when lifecycle ordering and state rules matter.
- Keep domain-specific branching at leaf nodes, not in shared base classes.
- Avoid deep inheritance when composition can isolate variability.

Load `references/patterns.md` when deciding between abstract classes,
protocols, strategies, or pipeline framework extraction.

## Output Format

Return results in this order:
1. Findings (highest impact first)
2. Open questions and assumptions
3. Incremental refactor plan

For each finding, include:
- Current repeated or unstable behavior
- Recommended abstraction shape and why
- Risks and guardrails
- File references with line numbers
- Tests needed before and after extraction

## Repository defaults

- Use the repository's documented command interface when checks or tests are needed.
- Use `rg` and line-anchored evidence for all claims.
- Prefer typed, explicit APIs and fail-fast validation semantics.
