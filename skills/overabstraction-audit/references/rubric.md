# Over-Abstraction Rubric

Use this rubric to score each finding before proposing refactors.

## Severity Bands

- `high`
  - Blocks safe changes or incident debugging.
  - Domain policy is hidden behind multiple glue layers.
  - Typical change requires edits across 8+ files, mostly wiring.

- `medium`
  - Slows contributors and increases review complexity.
  - Some boundaries are useful, but one or more layers are pass-through.
  - Typical change requires edits across 4-7 files with mixed policy/wiring.

- `low`
  - Noticeable but contained overhead.
  - Limited pass-through or speculative interfaces with low churn impact.

## Evidence Checklist

Collect concrete evidence for each signal:

1. Pass-through indirection
- Method body mostly forwards to one dependency.
- Layer adds no invariant checks, policy, batching, caching, or error semantics.

2. Speculative interface
- Protocol/port has one implementation and no near-term second backend/strategy.
- Adapter exists only to satisfy an interface boundary.

3. Ownership fracture
- Team cannot answer "where should this rule live?" quickly and consistently.
- Same rule appears in planner/resolver/engine/store branches.

4. Debug archaeology
- Logs are emitted in helpers, not at skip/build/retry decision boundaries.
- Stack trace crosses many orchestration classes before business rule appears.

5. Change amplification
- Small requirement touches many files, most of them registration/wiring.
- Code diff has more glue changes than policy changes.

## Rent Test

A layer pays rent only if at least one condition is true:
- Enforce invariant or validation boundary.
- Own domain policy decision.
- Deliver a capability: batching, caching, resumability, error semantics,
  observability semantics, transaction scope.

If none apply, recommend merge/inline.

## Output Template

Use this template per finding:

- `Severity`: high | medium | low
- `Type`: pass-through-indirection | speculative-interface |
  ownership-fracture | debug-archaeology | change-amplification
- `Evidence`: file/line anchors and a brief call-path summary
- `Why it does not pay rent`: missing invariant/policy/capability
- `Recommendation`: merge, inline, move policy boundary, or defer protocol
- `Expected impact`: fewer hops, clearer ownership, lower edit surface
- `Validation`: tests/checks to run

## Suggested PR Review Sentence

"Abstractions should pay rent: enforce an invariant, own a policy decision, or
provide a concrete capability. This boundary currently appears pass-through and
adds cognitive/debugging overhead without compensating value."
