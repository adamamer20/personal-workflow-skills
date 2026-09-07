---
name: recover-milestone
description: "Diagnose and recover a milestone after repeated failure, context degradation, interruption, or a guarantee/architecture mismatch. Use the assigned workspace and preserve accepted scope. Default to finishing or replanning automatically; require the user only for genuinely material underdetermination or external authority."
---

# Recover Milestone

Recovery changes the reasoning authority or approach. It does not terminate the
milestone by itself.

Use the exact assigned workspace. Preserve the logical owner and accepted
outcome.

The controller owns dispatch, callbacks, routing, retries, successor
scheduling, worktrees, and ledger mutation. This skill owns diagnosis and
bounded recovery reasoning only.

Preserve the lane's reviewed Git history. A mutable repair produces a successor
local commit after owned-surface staging, staged-diff inspection,
`git diff --cached --check` and the required gates. Never amend a reviewed
commit or integrate the lane into the program trunk; read-only diagnosis and an
explicit no-commit recovery contract are the only exceptions.

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

## Read recorded state first

Inspect:

- canonical plan and accepted decisions;
- milestone outcome and guarantees;
- workspace path, branch, HEAD, and dirty diff;
- attempted fixes and their evidence;
- review findings and causal IDs;
- last successful checkpoint;
- failing commands, artifacts, services, and environment state;
- context pressure or repeated tool patterns.

Do not restart from the original prompt. Do not discard working code or repeat
already proven work.

## Diagnose before another fix

Enter diagnosis after two attempts at the same causal failure without new
evidence or improvement in the same observable check. Also reassess when work
changes nature (implementation to redesign, oracle repair or hardening), a
material architecture premise changes, or compaction loses the active causal
context. The controller owns any fresh context, routing and resume; preserve
the same workspace and candidate. Token/call thresholds are backstops, not the
primary signal. Do not automatically select a stronger model.

Before blaming execution capability, distinguish a wrong outcome/oracle, an
oversized milestone containing several deliverables, a wrong implementation,
and an environmental blocker. Use the existing causal classes below; do not
create another recovery taxonomy or durable protocol.


Classify the dominant cause:

- `near_completion`: the approach is sound and remaining work is bounded;
- `wrong_hypothesis`: repairs targeted the wrong cause;
- `wrong_implementation`: the outcome is clear but the chosen mechanism is
  poor;
- `guarantee_mismatch`: a proof obligation is stronger than the accepted
  outcome, threat, or boundary;
- `architecture_replan`: implementation evidence invalidates an architecture or
  decomposition assumption;
- `material_decision`: two or more valid paths change explicit product,
  contract, safety, cost, destructive, or major-scope decisions;
- `environment`: service, configuration, dependency, filesystem, permission, or
  runtime state is the actual blocker;
- `external_dependency`: required credential, authority, service, hardware, or
  third-party action is unavailable;
- `infeasible`: the accepted outcome cannot reasonably be achieved under the
  accepted constraints.

Run one discriminating check when evidence is insufficient; name its expected
observation and stopping condition. Use a valid alternative plus a genuinely
invalid outcome when testing the oracle. Do not try another full repair cycle
merely to see what happens. Escalate implementation complexity only after the
target is sound; replan an invalid target or split an oversized milestone.

## Reassess the guarantee

For a suspected guarantee mismatch, reconstruct:

```yaml
required_outcome:
explicit_user_guarantee:
inferred_guarantee:
failure_or_threat:
trust_or_product_boundary:
current_proof_obligation:
implementation_cost:
simpler_sufficient_boundary:
effect_of_narrowing:
```

Rules:

- Preserve explicit user guarantees unless the user changes them.
- Preserve named product, public, persisted, security, privacy, data-integrity,
  authority, and destructive-operation boundaries.
- An inferred implementation-level guarantee may be narrowed or removed when
  evidence shows it is stronger than the accepted outcome and doing so does not
  change those explicit boundaries.
- If complete in-process isolation is truly required, recommend or implement a
  process/service boundary rather than unbounded application-level object
  policing.
- Record every guarantee change in the canonical plan.

## Act within accepted authority

Recovery may finish bounded work, replace an implementation strategy, simplify
machinery, split an oversized milestone or return a decision-ready replan while
preserving accepted outcome, contracts, safety, cost and destructive authority.
The planning owner updates canonical intent; a leaf does not become the planner
or dispatch its own continuation. Near completion means finish, not just report.

For wrong hypotheses, test a new causal explanation. For wrong implementation,
choose the simplest production-reachable mechanism and prove replacement parity
before deletion. For architecture/oracle mismatch, return the changed premise
and smallest sufficient plan delta. A stronger model does not repair a wrong
target. Diagnose environment failures as environment failures.

Reuse user decisions. Ask only when a material product/public/persisted contract,
security/privacy/integrity boundary, cost, authority, destructive effect or scope
choice remains underdetermined. Quote an instruction only if it actually prevents
continuation and identify the missing authority. Difficulty alone is not a reason
to stop. Preserve partial work and report its actual state.

Set one short falsification checkpoint: what observation would reject this new
approach, what is the smallest integrated outcome, and what unnecessary work can
be avoided? Do not grant an unbounded retry budget. A required external prerequisite
returns EXTERNAL_BLOCKED; a truly infeasible accepted constraint returns FAILED
with evidence. Material user choice returns NEEDS_DECISION. Changed strategy alone
remains CONTINUE.

## Output and continuation

Default result is `CONTINUE`.

The recovery result below is the canonical recovery evidence. Do not create a
parallel `evidence.md` or `evidence.json` by default. A separate evidence pack
is justified only by a controller-assigned bounded investigation; that pack
uses one canonical `evidence.md`, with JSON only for a named machine consumer.

```text
Diagnosis:
Evidence:
Remaining work: small | medium | fundamental
Guarantee assessment:
Plan delta:
Action taken:
Current workspace:
Next owner/role:
User decision required: yes | no
Terminal status: CONTINUE | COMPLETED | NEEDS_DECISION | EXTERNAL_BLOCKED | FAILED
```

When continuing, preserve the same workspace and causal finding IDs.
