---
name: execute-milestone
description: Implement exactly one decision-ready typed capsule, verify its observable gates, and return one durable typed result. Not for planning or broad refactors.
---

# Execute Milestone

Own exactly one decision-ready capsule through verification. It governs intent,
acceptance, mutable/protected surfaces, and scope. Do not invent another
milestone, change public contracts, or turn implementation into planning.

## Work inside the capsule

Read instructions, plan, capsule, relevant code/tests, and worktree status.
State scope, non-goals, protected surfaces, acceptance modes, and promotion
gates. Preserve unrelated changes and singular ownership of shared contracts
and entrypoints.

Trace before changing: locate the real production path, inspect existing
implementations and callers, and identify the shared/root cause when a symptom
repeats across callers. Do not distribute the same defensive patch across
consumers when one owned boundary can fix the cause.

Implement the smallest coherent change and prove protected surfaces unchanged.
Repair ordinary failures and self-review. After two non-improving attempts
without new causal evidence, preserve the candidate and diagnose outcome/oracle,
scope, implementation or environment; no automatic retry/model change. Successors
own new ambition; regressions stay. Do not build a benchmark/framework before the
vertical works. Acceptance is rendered for `visual`, boundary/safety evidence for
`architecture`, deterministic for `objective`; mixed capsules pass every mode.

Use stable capability/domain names for every durable path or identifier; reject
temporary milestone/task labels. Preserve numbered historical/protocol names
only for documented compatibility/provenance, and migrate all owned references
together.

## Preserve semantic density

Implement the minimum semantic delta that satisfies the capsule guarantee. Stop
at the first sufficient rung: remove work the outcome does not need; reuse an
existing concept/helper/module/production path; use the standard library; use a
native platform/runtime capability; use an already-owned dependency; implement
locally with functions/direct composition; only then introduce a new abstraction,
module, dependency or framework. A planned artifact may be omitted when an
earlier rung satisfies the same guarantees; report the smaller delta. Never
weaken correctness, security, integrity, accessibility or an explicit
architecture guarantee.

Vocabulary needs an invariant, policy,
identity, validation, substitution or algorithm, not style or tests alone.
Use functions/direct composition; classes need state/lifecycle/policy and a
one-implementation protocol needs a real replaceable boundary.
Keep strict edges and boring interiors: validate once at each untrusted edge,
keep behavior beside invariants, remove pass-through layers and synonym types.
Tests express observable guarantees. Semantic compression makes repeated domain
decisions disappear from call sites; do not abstract incidental mechanical repetition.
Return unplanned modules, public types, registries, schemas, entrypoints or
boundaries to planning. Preserve every capsule acceptance mode: objective code authority, render-aware
visual authority and architecture boundary/security authority. Passing one mode
cannot substitute for another.

## Outcome and evidence priority

Outcome and evidence are ordered and non-negotiable:

1. Accepted observable outcome and user intent.
2. Required safety, integrity, lineage, isolation, recovery, and production/independent-proof guarantees.
3. Explicitly designated hard external constraints (legal/protocol/compatibility/deployment ceilings), with a recorded conflict policy.
4. Secondary proxy and optimization metrics (lines, files, duration, tokens, coverage, complexity, scores, inventories).

A lower-priority item never authorizes weakening a higher-priority item.
Numeric targets are secondary unless the accepted plan marks them hard external constraints. Even a hard cap never silently authorizes removing a required check: use proof-preserving replacement or return a bounded replan or genuine decision when it conflicts. Never report success merely because a proxy is exact.

## Proportional validation and findings

Use discriminating checks, then affected partitions and named closure gates.
Shared contracts, collection or packaging require the repository gate. Reuse
unchanged green evidence; tests/metadata do not replace observable outcome proof.
Report pending independent acceptance separately.

Separate severity from promotion impact. P0 is presumptively blocking; P1
blocks accepted guarantees, contracts, boundaries, production reachability, or
integrity. Deferrals name owner and `defer_to`; close with none blocking.

Mutable `COMPLETION` needs an owned coherent local commit: stage only owned surfaces, inspect staged paths,
run `git diff --cached --check` and gates, exclude unrelated artifacts. Read-only
or explicit no-commit capsules are exempt. Report the exact commit tip or range;
do not merge into the program integration trunk, amend a reviewed commit, push,
rebase, rewrite, discard or mutate remotes.

Skills own cognition only. They do not own dispatch, callbacks, routing,
retries, successor scheduling, worktree creation, or ledger mutation.

## Controller boundary and result

The parent owns workflow-control/codex-flow, dispatch, reviews, repairs and
successors. This leaf launches no agents, reviews or controller calls. Implement,
test and self-review; completion-biased recovery stays within accepted intent,
contracts, safety, cost, destructive authority and scope.

Return one raw codex_flow.contracts.ModelFacingResult for the SDK harness.
JSON/JSONL is controller serialization. Keep outcome, exact candidate, key checks,
contract changes, blocker and remainder concise; link required domain evidence.
For an explicitly selected native route, send exactly one terminal callback to
the supplied controller thread/host, or an earlier material escalation. No routine
progress callbacks or lifecycle authority. Stop after assigned work.
