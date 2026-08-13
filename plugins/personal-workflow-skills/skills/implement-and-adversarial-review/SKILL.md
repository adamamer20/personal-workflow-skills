---
name: implement-and-adversarial-review
description: Implement a requested feature, then run independent adversarial branch or diff reviews with sub-agents and fix substantive findings until only minor or stylistic observations remain. Use only when the user explicitly invokes `$implement-and-adversarial-review`; never trigger it implicitly.
---

# Implement and Adversarially Review

Own the requested feature through implementation, verification, independent
review, and remediation. Do not stop after producing a review report.

## Establish scope

1. Read repository instructions and inspect the current worktree before editing.
2. Resolve the review target to the feature's complete branch diff against its
   appropriate merge base. Include relevant pre-existing feature changes;
   exclude clearly unrelated user work.
3. Record the requested acceptance criteria and infer only low-risk details.
   Ask only when a missing decision would materially change product behavior.

## Implement the feature

1. Inspect existing architecture, contracts, tests, and nearby patterns.
2. Implement the smallest coherent solution, updating every in-repository
   caller and current mock or fixture contract when applicable.
3. Add or update focused tests. Run the most relevant checks after changes.
4. Preserve unrelated worktree changes and never use destructive Git cleanup.

## Run the adversarial review loop

After the implementation and focused checks succeed:

1. Spawn at least one fresh sub-agent dedicated to reviewing the complete
   feature branch diff. Do not review solely in the implementing agent's context.
2. Give the reviewer the merge base, diff scope, acceptance criteria,
   repository instructions, and test commands. Do not give it the implementing
   agent's conclusions or suggest expected findings.
3. Require concrete file and line evidence, severity, impact, and a proposed
   remedy across correctness, regressions, missing requirements, security,
   privacy, data integrity, concurrency, error handling, typing, API contracts,
   user experience, accessibility, and missing tests where relevant.
4. Independently validate every finding against the code. Fix all validated
   findings above minor or stylistic severity. Add regression tests where
   practical; do not silence or weaken checks.
5. Re-run relevant checks, then spawn a fresh adversarial review pass over the
   updated complete diff. A review by the same stale context is insufficient.
6. Repeat remediation and fresh review until the latest pass contains no
   validated substantive finding.

Use parallel reviewers only when independent review dimensions are useful and
concurrency is available. Never let sub-agents edit the same files concurrently;
reviewers report findings while the primary agent owns remediation.

## Exit criteria

Finish only when all of the following are true:

- The requested acceptance criteria are implemented.
- Relevant tests and checks pass, or an external blocker is reported precisely.
- The latest fresh adversarial pass has no unresolved substantive finding.
- Remaining observations, if any, are genuinely cosmetic, stylistic, or
  optional cleanup without meaningful correctness, security, contract, user
  experience, or maintainability impact.

Do not relabel unresolved defects as minor to terminate the loop. If progress
is blocked by unavailable infrastructure, missing authority, or a product
decision, report the exact blocker and unresolved finding instead of claiming
completion.

## Handoff

Summarize the implemented behavior, notable files changed, checks run and
results, adversarial review passes, substantive findings fixed, and remaining
minor or style notes. State explicitly whether the exit criteria were met.
