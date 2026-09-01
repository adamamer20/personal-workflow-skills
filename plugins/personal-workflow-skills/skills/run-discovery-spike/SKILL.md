---
name: run-discovery-spike
description: Answer one material feasibility, integration, performance, or architecture question with a bounded throwaway experiment. Use when inspection alone cannot distinguish viable approaches and choosing incorrectly would cause substantial rework. Do not use for production implementation or an open-ended prototype.
---

# Run Discovery Spike

Build the smallest experiment that can resolve one decision.

The spike is evidence, not a hidden production implementation. The controller
owns workspace creation and should normally place mutating spikes in an isolated
managed worktree.

The controller owns dispatch, callbacks, routing, retries, successor
scheduling, worktrees, and ledger mutation. This skill owns cognition and
evidence only.

## 1. State the decision

Write:

- decision to make;
- competing hypotheses or approaches;
- why repository inspection is insufficient;
- shared fixture or workload;
- objective comparison criteria;
- budget and stopping condition;
- production surfaces that must remain untouched.

A spike must be able to end with `adopt`, `reject`, or `inconclusive`.

## 2. Design a discriminating experiment

Prefer a thin experiment over a miniature product.

Good spike:

```text
same real input
→ approach A
→ approach B
→ same observable output/rubric
```

Bad spike:

```text
build a second architecture and see whether it feels promising
```

Use the canonical production interfaces where possible, but keep experimental
code isolated. Do not add compatibility layers or broad abstractions to support
the spike.

For visual or artifact engines, compare the same sentinel input and inspect the
actual rendered result. For performance questions, use the same dataset,
machine assumptions, warm-up policy, and metrics.

## 3. Preserve production safety

Before mutation:

- confirm the isolated workspace and branch;
- record base SHA;
- identify protected production paths;
- avoid secrets and destructive external operations;
- keep fixtures and outputs under an explicit spike directory.

Do not merge spike code into production. A later milestone may independently
implement the chosen approach.

## 4. Run the minimum experiment

For each approach record:

- exact setup;
- command or entrypoint;
- result;
- failures;
- output/artifact paths;
- measured cost, latency, complexity, or quality;
- evidence that the result is representative enough for the decision.

Do not keep iterating to polish an approach after the decision is clear.

## 5. Decide sceptically

Use the predeclared criteria. Do not change the rubric because one approach is
easier to implement.

Return:

- `adopt`: evidence is sufficient to choose an approach;
- `reject`: evidence is sufficient to rule it out;
- `inconclusive`: identify the one missing discriminating test.

Separate:

- empirical evidence;
- implementation estimate;
- architectural inference;
- subjective judgment.

Do not silently make a user-owned product or architecture decision. Record it
as an open decision or recommendation with the evidence and uncertainty that
the owner must weigh.

## 6. Write the artifacts

Write:

- `spike-report.md`;
- `spike-result.json`;
- durable fixture/output paths.

Use this report shape:

```markdown
# Discovery Spike: <decision>

## Decision
## Hypotheses
## Shared fixture and criteria
## Experiment
## Results by approach
## Evidence and uncertainty
## Recommendation
## Production implementation implications
## Cleanup status
```

The JSON result must include:

```json
{
  "decision": "adopt | reject | inconclusive",
  "recommended_approach": null,
  "criteria": [],
  "results": [],
  "confidence": "low | medium | high",
  "production_code_promoted": false,
  "artifact_paths": [],
  "follow_up": ""
}
```

If an approach wins, recommend a production milestone with explicit boundaries.
Do not implement it in this spike.
