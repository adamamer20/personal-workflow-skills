---
name: overabstraction-audit
description: Detect and prioritize harmful over-abstraction in service and pipeline codebases. Use when architecture reviews, PR feedback, or refactors need evidence on pass-through layers, speculative protocols, unclear ownership boundaries, debug-hostile call chains, or change amplification from wiring-heavy designs.
---

# Over-Abstraction Audit

## Goal

Evaluate whether current abstractions pay rent, then recommend targeted merges,
boundary clarifications, and policy-centered contracts that reduce cognitive
load without collapsing useful architecture.

## Workflow

1. Scope the decision path.
- Trace one concrete domain action end-to-end (for example: "accept one request",
  "persist one record", "mark one job complete").
- Record hop count: number of files/classes crossed before domain policy is
  executed.

2. Gather over-abstraction signals.
- Find pass-through methods and classes that mostly forward calls.
- Find protocol/port interfaces that have only one implementation.
- Find policy decisions split across layers with ambiguous ownership.
- Find logging/error emission far from skip/build/retry policy boundaries.
- Find small features that require many wiring edits across layers.

3. Classify each finding.
- `pass-through-indirection`: forwarding without policy/capability value.
- `speculative-interface`: interface added before competing implementations.
- `ownership-fracture`: unclear location for domain rule ownership.
- `debug-archaeology`: traceability blocked by glue-heavy stack paths.
- `change-amplification`: trivial behavior change requiring broad edits.

4. Test whether each layer pays rent.
- Keep a layer when it enforces invariants, owns policy, or delivers a clear
  capability (batching, caching, resumability, logging semantics, error
  semantics).
- Flag a layer when it adds navigation cost but no unique behavior.

5. Produce simplification plan.
- Merge or inline non-rent-paying layers first.
- Keep boundaries around policy decisions explicit (`planner`, `resolver`,
  `deriver`, `assembler`, `store`) and prevent mixed ownership.
- Delay protocol extraction until a second real implementation appears, unless
  test seams or dependency inversion are immediately required.

Load `references/rubric.md` when scoring findings and writing review text.

## Output Requirements

Return results in this order:
1. Findings ordered by severity
2. Open questions and assumptions
3. Simplification plan

For each finding, include:
- Symptom and observed evidence
- Why the abstraction is or is not paying rent
- Recommended structural change
- Expected impact on debuggability and change surface
- File references with line numbers
- Validation/tests needed after the change

## Review Language

Use direct, non-dogmatic language:
- "Abstractions should pay rent: enforce an invariant, own a policy decision,
  or deliver a capability."
- "This layer appears to be pass-through and increases navigation cost."
- "Merge this boundary unless a second implementation is planned now."

## Repository defaults

- Use `rg` for evidence collection and line-anchored references for claims.
- Use the repository's documented checks and tests when verification is required.
- Prefer forward-only simplification with updated call sites in the same
  change.
