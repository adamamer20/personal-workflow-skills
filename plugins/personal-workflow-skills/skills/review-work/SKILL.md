---
name: review-work
description: "Independently review a fixed change or artifact through one explicitly named lens: outcome/spec, correctness/integration, standards/maintainability, visual quality, or security boundary. Use only when the plan names a review or promotion risk. Classify findings by evidence and promotion impact, and challenge disproportionate guarantees instead of rewarding theoretical completeness."
---

# Review Work

Review one fixed artifact through one named lens.

One review is the default. Additional independent review contexts are a
controller decision only when they cover materially distinct risks with
different evidence. One reviewer does not invoke another reviewer.

Do not review everything by default.

The controller owns dispatch, callbacks, routing, retries, successor
scheduling, worktrees, and ledger mutation. This skill owns findings and review
reasoning only.

## Leaf task boundary

A task assigned implementation, review or recovery does only that role. Never
spawn subagents or peer tasks, invoke workflow-control, launch independent
reviews, schedule successors or supervise other workers. The controller owns
those actions. Implementation includes tests, self-review and bounded repairs;
independent acceptance may remain pending when the assigned work is delivered.
A reviewer returns findings and a verdict without implementing fixes or
launching another review.

For an explicitly selected native task route, send exactly one terminal callback
to the supplied controller thread/host after the assigned work ends. Report the
exact candidate, validation or findings, and pending acceptance. A material
escalation may return earlier; do not send routine progress callbacks. For the
SDK route, return the raw typed result to the harness; do not send peer messages.
Callback delivery in native mode does not grant lifecycle authority.

## Pin the review target

Record:

- repository and workspace;
- fixed base revision or artifact version;
- reviewed exact lane commit tip/range or output;
- originating plan/spec;
- named acceptance criteria and guarantees;
- review lens.

Fail clearly when the fixed point, artifact, or source of truth cannot be
resolved. Do not guess.

For mutable lane work, verify that the reviewed commit descends from the
plan-recorded integration base, contains no unrelated commits or surfaces, and
matches the supplied workspace bytes. A repaired finding requires a successor
commit and a new fixed review range; amending a reviewed commit invalidates the
prior review. Review is read-only and never integrates the lane.

## Use exactly one lens

### Outcome / spec

Check whether the change implements the required behavior and avoids unrequested
scope. For architecture acceptance, include the named security/privacy and trust
boundaries in this same review. Do not split architecture and security into
separate default passes; escalate only a concrete unresolved risk. This does not
replace a separately requested security audit.

### Correctness / integration

Check runtime behavior, failure handling, production reachability, contracts,
data flow, cleanup, cancellation, and cross-component integration.

### Standards / maintainability

Check repository conventions, clarity, duplication, unnecessary indirection,
speculative generality, ownership, change surface, and semantic density. Compare
the planned and implemented semantic delta. Look specifically for multiple
symbols expressing one concept, one-implementation protocols without a real
boundary, pass-through services/adapters, config/state/result/context wrappers
without distinct invariants, repeated boundary validation/conversion, behavior
separated from its invariant, and tests that pin implementation ceremony.

Also detect under-abstraction: repeated domain decisions, relationships,
transitions, state handling, or parsing that a pure policy/reducer, declarative
specification, small relationship type, or typed codec could make disappear
from call sites. Do not recommend unifying incidental mechanical repetition.

Prefer deletion, collapse, a function, an existing domain concept, or direct
composition when accepted guarantees remain intact. Do not reward explicitness
or type count by itself; every retained abstraction must carry a distinct
invariant, domain distinction, policy, lifecycle/identity, boundary validation,
genuine substitution seam, or reusable algorithm.

### Visual

Inspect actual rendered artifacts at intended viewing sizes and relevant
responsive states. Evaluate the observable rubric. Do not infer visual quality
from code, component counts, metadata, or successful export alone.

### Security boundary

Review the named threat model and documented trust boundaries. Check that
secrets, sensitive payloads, permissions, persistence, logs, telemetry, APIs,
and external observability obey the accepted contract.

Do not silently strengthen the threat model to arbitrary process-memory or
object-graph inspection. When complete in-process isolation is explicitly
required, verify that the architecture provides an isolation boundary rather
than pretending application choreography proves it.

## Review guarantees as well as implementations

For each material problem, distinguish:

- the accepted outcome or guarantee is correct and the implementation violates
  it;
- the implementation mechanism is unnecessarily complex;
- the guarantee is stronger than the accepted outcome or named threat; or
- the evidence gate proves bookkeeping rather than the observable boundary.

When complexity appears disproportionate, return a `GUARANTEE_CHALLENGE` rather
than demanding more machinery:

```yaml
type: GUARANTEE_CHALLENGE
guarantee:
explicit_or_inferred:
required_outcome:
named_failure_or_threat:
documented_boundary:
current_proof_obligation:
complexity_created:
simpler_sufficient_guarantee:
user_or_plan_decision_needed: true | false
```

A reviewer may recommend replanning. It does not silently weaken an explicit
guarantee.

## Outcome and evidence priority

Outcome and evidence are ordered and non-negotiable:

1. Accepted observable outcome and user intent.
2. Required safety, integrity, lineage, isolation, recovery, and production/independent-proof guarantees.
3. Explicitly designated hard external constraints (legal/protocol/compatibility/deployment ceilings), with a recorded conflict policy.
4. Secondary proxy and optimization metrics (lines, files, duration, tokens, coverage, complexity, scores, inventories).

A lower-priority item never authorizes weakening a higher-priority item.
Numeric targets are secondary unless the accepted plan marks them hard external constraints. Even a hard cap never silently authorizes removing a required check: use proof-preserving replacement or return a bounded replan or genuine decision when it conflicts. Never report success merely because a proxy is exact.

Treat proxy-target success accompanied by lost semantic proof as a
promotion-blocking outcome regression. Challenge a disproportionate proxy or
hard-cap constraint with evidence and recommend proof-preserving replacement or
bounded replanning; never reward an exact metric that invalidates the outcome.

## Finding schema

Every finding must include:

```yaml
id:
lens:
severity: P0 | P1 | P2
promotion_blocking: true | false
acceptance_or_guarantee:
location_or_artifact:
evidence:
causal_class:
user_impact_or_dependency_risk:
required_action:
```

No evidence means no finding.

### Severity is not identical to promotion impact

P0 is normally promotion blocking.

P1 is promotion blocking when it:

- violates a named acceptance or safety boundary;
- invalidates the milestone outcome;
- creates unacceptable risk for dependent work;
- corrupts or loses data;
- exposes unauthorized behavior; or
- makes the production path materially incorrect.

A P1 may be non-blocking when it is isolated, non-propagating, and explicitly
safe to defer. State the evidence and target for deferral.

Do not inflate edge cases, style preferences, theoretical attacks, or possible
future extensibility into P1 without concrete impact.

## Review proportionality

Ask:

- Does this review depth match the milestone risk?
- Is the concern reachable through the production path?
- Is the threat inside the accepted model?
- Would fixing it now reduce real risk, or only satisfy a stronger invented
  proof obligation?
- Can dependent work safely continue?
- Is a final integrated hardening review the more efficient place to address it?

Review deeply enough to establish safe promotion. Do not search indefinitely for
theoretical completeness.

Reuse accepted evidence for unchanged behavior and spend new verification on
the changed causal path. Do not expand a milestone's gate to future deployment
work that is not required for its accepted outcome. When two lenses identify
the same cause, preserve both finding IDs but request one coherent repair.

## Re-review policy

One independent review is the normal promotion check.

Run another review only when the plan identifies a materially distinct risk and
the additional lens has different evidence. After a promotion-blocking repair,
re-review the repaired finding and changed causal surface; do not repeat broad
review without a reason.

If the same causal finding persists, return `REPLAN_RECOMMENDED` with the
evidence needed to change the approach. Do not start an endless review loop.

## Verdict

Return one:

- `ACCEPT`
- `REPAIR_REQUIRED`
- `REPLAN_RECOMMENDED`
- `NEEDS_DECISION`
- `EXTERNAL_BLOCKED`

```text
Lens:
Fixed point:
Verdict:
Promotion-blocking findings:
Deferred findings:
Guarantee challenges:
Evidence inspected:
Required next action:
```
