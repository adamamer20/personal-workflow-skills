---
name: grill-me-light
description: Lightweight implementation planning interview. Inspect the repository first, form a strong recommended design, and ask only high-impact questions whose answers would materially change product behavior, architecture, interfaces, data ownership, safety, or acceptance criteria. Stop as soon as an implementation agent can execute without inventing important decisions. Update the single active plan at the path defined by repository instructions or the user. Not for trivial changes that can be implemented directly, and not for exhaustive requirements discovery.
---

# Grill Me Light — Close Important Decisions, Not Every Decision

The goal is not to eliminate all uncertainty. The goal is to leave an implementation agent with no need to invent product intent, architecture, important contracts, or success criteria.

## Core behavior

1. **Inspect before asking.**
   - Read the relevant repository files, existing architecture, tests, docs, and repo instructions first.
   - Treat established project conventions as defaults. Do not ask the user to reconfirm decisions already encoded in the repo.

2. **Delegate reconnaissance when it saves context or time.**
   - The planner may use read-only sub-agents to investigate bounded parts of the codebase instead of loading large or weakly related areas into its own context.
   - Delegate **evidence gathering, tracing, and architecture discovery**; do not delegate the final product or architecture decision. The main planner remains responsible for synthesizing one coherent design.
   - Give each sub-agent a narrow question and scope, for example: trace an existing request flow, identify the owner of a persisted model, find analogous implementations, enumerate current API contracts, or locate the tests that define behavior.
   - Prefer the configured economical sub-agent for bounded reconnaissance. Escalate reasoning only when the task genuinely requires deeper cross-cutting analysis or the first pass is inconclusive.
   - Parallelize independent reconnaissance when useful, but do not spawn multiple agents to answer the same question unless disagreement or verification is valuable.
   - Require concise findings, not a second plan: conclusion, relevant paths/symbols, important contracts/invariants, relevant tests, and any material uncertainty.
   - Do not use sub-agents merely because they are available. For a small/local change, inspect directly.

3. **Think first, then ask.**
   - Form a concrete recommended approach before interviewing the user.
   - Prefer a good default over presenting every theoretically possible design.
   - When asking a question, state the recommended answer briefly and why it matters.

4. **Ask only material questions.**
   Ask the user only when their answer could materially change one or more of:
   - user-visible behavior or expected result;
   - architecture or component ownership;
   - public/API/interface contracts;
   - persisted data model, migration strategy, or compatibility behavior;
   - security, privacy, permissions, destructive operations, or meaningful operational risk;
   - an important performance/cost tradeoff;
   - acceptance criteria or what counts as done.

   Do **not** ask about:
   - local implementation details an engineer can safely choose;
   - naming, minor abstractions, helper structure, or style already covered by repo conventions;
   - reversible choices with low blast radius;
   - edge cases that have an obvious conventional treatment and do not alter product intent.

5. **One question at a time.**
   - Ask the highest-information unresolved question first.
   - Prefer a concrete forced choice or a small set of realistic alternatives over an open-ended brainstorm.
   - Include: **Recommendation:** <default>.
   - If the user's answer exposes a new material branch, follow it. Otherwise move on.

6. **Use a question budget.**
   - Typical task: **0–4 questions**.
   - Complex/high-risk task: **up to 6 questions**.
   - Exceed 6 only when the user explicitly asks for exhaustive planning.
   - A question budget is not a target. If the plan is already implementation-ready, ask zero questions.

7. **Resolve low-risk ambiguity yourself.**
   - Record reasonable assumptions in the plan instead of asking about each one.
   - Prefer the simplest design consistent with the repo and task.
   - Avoid speculative extensibility and architecture for hypothetical future requirements.

## Stop condition

Stop interviewing when another capable implementation agent can answer all of these from the plan and repo without asking the user:

- What exactly are we trying to achieve?
- What behavior should change, and what should remain unchanged?
- What is the intended architecture / ownership boundary?
- Which components or surfaces are expected to change?
- What important contracts or invariants must hold?
- What failure modes and meaningful edge cases must be handled?
- What is explicitly out of scope?
- How do we prove the implementation is correct?

The implementer may still choose ordinary code structure, helper functions, exact names, and other local details.

## Active plan contract

After the stop condition is met, update exactly one active plan at the path named by repository instructions or the user. If no durable plan path is defined, return the plan in the thread unless the user explicitly asks to create a file. Never create a second mutable roadmap alongside an existing active plan. Treat the selected plan as the task's source of truth for the goal, accepted decisions, implementation evidence, review findings, and closure gates.

```markdown
# Plan: <task>

## Goal
<Concise description of the desired outcome and user-visible behavior.>

## Current context
<Only the existing architecture/constraints that materially shape this change.>

## Proposed design
<The recommended architecture and main flow. Be decisive rather than listing possibilities.>

## Component changes
- `<component/path>` — <responsibility/change>
- ...

Use concrete paths when known. If exact files are not yet known, name the owning package/service/module rather than inventing filenames.

## Contracts and invariants
<Important APIs, types, persistence rules, ownership boundaries, state transitions, compatibility expectations, or other facts the implementation must preserve.>

## Edge cases and failure behavior
<Only meaningful cases that could affect correctness, data, UX, security, or operations. State the intended behavior.>

## Implementation sequence
1. <coherent implementation step>
2. ...

Keep this at architecture/component level. Do not prescribe every function or line of code.

## Acceptance criteria
- <observable outcome>
- <observable outcome>

## Validation
<Exact or likely tests/checks/proof required by the repo and this change.>

## Assumptions
<Low-risk decisions made by the planner without burdening the user.>

## Out of scope
<Explicit boundaries that prevent scope creep.>
```

## Detail policy

- **Do not include code by default.**
- Include small interface shapes, schemas, state diagrams, pseudocode, or example payloads only when they materially remove ambiguity for the implementer.
- Prefer stating ownership, invariants, data flow, and expected behavior over prescribing internal implementation mechanics.
- Do not plan speculative future features unless they are required by the current task.

## Final self-check before handoff

Before presenting the plan, challenge it once yourself:

1. Is any important user/product decision still being delegated to the implementer?
2. Is any architectural boundary ambiguous?
3. Could two competent agents implement materially different behavior while both claiming to follow this plan?
4. Are the important failure cases and acceptance criteria explicit?
5. Did the plan add complexity not justified by the current requirement?
6. Did delegated reconnaissance surface conflicting evidence that still needs synthesis or verification?

If 1–4 or 6 reveal a material gap, ask one more question or resolve it from the repo. If 5 is yes, simplify the plan.

## Optional adversarial review

For high-risk changes (auth, permissions, migrations, destructive operations, concurrency, billing, critical persistence), a separate read-only review can be useful after the plan is written.

Do not make multi-round adversarial review the default. Start with one review pass and revise only for material findings. Escalate to additional rounds only when the reviewer identifies unresolved high-impact issues.
