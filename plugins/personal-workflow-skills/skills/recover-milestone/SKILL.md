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

Run one discriminating check when evidence is insufficient. Do not try another
full repair cycle merely to see what happens.

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

## Recovery authority

Without asking the user, recovery may:

- finish bounded remaining work;
- replace a failed local implementation strategy;
- simplify unnecessary machinery;
- change helper structure and internal decomposition;
- revise implementation architecture while preserving accepted behavior,
  explicit contracts, named safety boundaries, authorized cost, and scope;
- split or combine milestones when this improves convergence without changing
  program intent;
- remove or narrow an inferred, non-material guarantee;
- update the canonical plan and continuation capsule;
- continue in the same recovery context or hand back a decision-ready
  continuation.

Recovery must request a user decision when continuing would change an explicit:

- product behavior;
- public or persisted contract;
- security, privacy, or data-integrity boundary;
- authority or cost;
- destructive action;
- major scope or compatibility promise;

and existing decisions do not determine the choice.

Implementation difficulty alone is not a reason to ask the user.

Check the user's existing decisions before requesting permission again. An
implementation-level plan correction within accepted intent should continue
through the planning owner. If an instruction prevents that continuation, quote
the exact instruction and identify the material decision or authority missing.
Preserve the partial candidate and report the recovery outcome to its owner even
when the repair cannot close; never leave a silent completed-looking task.

## Act on the diagnosis

### Near completion

Finish the milestone in this context when safe. Do not stop merely to report
that little work remains.

### Wrong hypothesis

Return to the failing boundary, form one new hypothesis, run one discriminating
check, and repair the root cause.

### Wrong implementation

Choose the simplest production-reachable mechanism that satisfies the accepted
guarantees. Remove superseded machinery only after replacement parity is
proven.

### Guarantee mismatch

Update the plan with the minimum sufficient guarantee when authorized by the
rules above, simplify the implementation, and continue.

### Architecture replan

Update the canonical plan, preserve already valid work, and continue or produce
one decision-ready continuation. Do not repeat the same architecture with a
stronger role.

### Environment

Repair or document the environment issue through real available tools. Do not
misclassify it as product failure.

### Material decision

Return `NEEDS_DECISION` with evidence, a recommendation, and the smallest set of
meaningfully different options.

### External dependency

Return `EXTERNAL_BLOCKED`.

### Infeasible

Return `FAILED` with the specific accepted constraint that makes the outcome
unachievable and the closest feasible alternative.

## Prevent another rabbit hole

Before continuing, ask:

- What did the previous loop optimize instead of the real outcome?
- Which guarantee or assumption created the most complexity?
- What evidence would falsify the new approach quickly?
- What is the smallest integrated checkpoint?
- What work can be deleted or avoided?
- Does the new approach reduce total system complexity?

Set a short falsification checkpoint for the new approach. Do not grant it an
unbounded retry budget.

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
