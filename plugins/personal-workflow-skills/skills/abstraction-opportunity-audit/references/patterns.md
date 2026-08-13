# Abstraction Patterns Rubric

## Choose Abstract Base Class

Use when:
- A strict lifecycle must run in the same order everywhere.
- Shared protected state and invariant enforcement are required.
- Subclasses override a small number of well-defined steps.

Avoid when:
- Variability appears as independent policies.
- Callers only need capability-level contracts.

## Choose Protocol or Interface

Use when:
- Multiple implementations should stay decoupled.
- Callers use only a small method surface.
- Tests benefit from lightweight fakes.

Avoid when:
- The abstraction must enforce shared algorithm sequencing.

## Choose Strategy Object

Use when:
- One orchestration flow selects among interchangeable behaviors.
- Runtime switching is expected (config, feature flags, input type).
- Behavior units should stay independently testable.

Avoid when:
- There is only one likely implementation.

## Choose Mini-Framework Extraction

Use when:
- Several modules repeat the same stage model (load, normalize,
  validate, persist, report).
- Teams need a common extension mechanism with clear hooks.
- Operational concerns (logging, retries, metrics) are duplicated.

Guardrails:
- Keep framework surface small and explicit.
- Publish extension contracts first; migrate one adopter at a time.
- Delay broad generalization until two adopters are stable.

## Risk Signals (Do Not Extract Yet)

- Similarity is mostly naming, not behavior.
- Edge cases diverge in preconditions or error semantics.
- Abstraction would require many boolean flags.
- Inheritance depth would exceed two levels.
