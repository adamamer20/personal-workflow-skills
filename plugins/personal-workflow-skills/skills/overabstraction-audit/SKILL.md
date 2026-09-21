---
name: overabstraction-audit
description: Detect and prioritize harmful implementation machinery in service and pipeline codebases. Use when architecture reviews, PR feedback, or refactors need evidence on pass-through layers, speculative protocols, reinvented capabilities, dependency overreach, unclear ownership, debug-hostile call chains, or change amplification.
---

# Over-Abstraction Audit

## Goal

Evaluate whether current abstractions pay rent, then recommend targeted merges,
reuse/native replacements, boundary clarifications, and policy-centered contracts
that reduce cognitive load without collapsing useful architecture.

## Workflow

1. Scope the decision path.
- Trace one concrete domain action end-to-end (for example: "accept one request",
  "persist one record", "mark one job complete").
- Record hop count: number of files/classes crossed before domain policy is
  executed.
- Read callers and the production path before judging solution size.

2. Gather over-abstraction signals.
- Find pass-through methods and classes that mostly forward calls.
- Find protocol/port interfaces that have only one implementation.
- Find policy decisions split across layers with ambiguous ownership.
- Find logging/error emission far from skip/build/retry policy boundaries.
- Find small features that require many wiring edits across layers.
- Find local code that reinvents an existing repository, standard-library, or
  native platform capability.
- Find new dependencies whose used capability is already available or is small
  enough to own directly without sacrificing guarantees.

3. Classify each finding.
- `pass-through-indirection`: forwarding without policy/capability value.
- `speculative-interface`: interface added before competing implementations.
- `ownership-fracture`: unclear location for domain rule ownership.
- `debug-archaeology`: traceability blocked by glue-heavy stack paths.
- `change-amplification`: trivial behavior change requiring broad edits.
- `reinvented-capability`: local machinery duplicates an existing, stdlib, or
  native capability.
- `dependency-overreach`: a dependency/framework adds more ownership and surface
  than the accepted outcome requires.

4. Test whether each layer pays rent.
- Keep a layer when it enforces invariants, owns policy, or delivers a clear
  capability (batching, caching, resumability, logging semantics, error
  semantics).
- Flag a layer when it adds navigation cost but no unique behavior.

5. Produce simplification plan.
- Apply the first-sufficient ladder: delete, repository reuse, standard library,
  native capability, already-owned dependency, local functions/composition, then
  a new abstraction/dependency only when earlier rungs cannot preserve the
  accepted guarantees.
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
