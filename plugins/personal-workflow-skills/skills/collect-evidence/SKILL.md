---
name: collect-evidence
description: Perform a bounded read-only investigation over a repository, documents, logs, artifacts, or primary sources and return a compact evidence pack. Use when planning, review, or recovery would otherwise load a large amount of material into the main worker context. Do not use to implement, decide product intent, or produce a broad open-ended research report.
---

# Collect Evidence

Answer one explicit question with traceable evidence while preserving the
downstream worker's context.

This is a read-only specialist. Do not mutate source, create a worktree, update a
plan, or recommend broad implementation unless the request explicitly asks for
a bounded comparison.

The controller owns dispatch, callbacks, routing, retries, successor
scheduling, worktrees, and ledger mutation. This skill owns cognition and
evidence only.

## 1. Fix the question

Restate:

- the exact question;
- the decision or task the evidence will support;
- the in-scope repositories, paths, commits, documents, artifacts, or sources;
- the evidence freshness requirement;
- the stopping condition.

Reject an unbounded request such as “understand the entire repository.” Convert
it into a small set of answerable subquestions or return the missing scope.

## 2. Inspect authoritative material first

Prefer:

1. governing instructions and accepted decisions;
2. production code and real call sites;
3. tests and executable contracts;
4. current generated/runtime artifacts;
5. canonical documentation;
6. historical notes only when needed to explain current state.

Distinguish current truth from historical intent. Do not let a recently modified
archive override current production behavior.

For external technical research, use primary sources and record source identity
and date. Do not silently replace repository-specific evidence with generic
knowledge.

## 3. Keep evidence and inference separate

For every material conclusion, record:

- claim;
- exact evidence path, symbol, line/range, command, or artifact;
- whether the conclusion is direct evidence or inference;
- confidence;
- unresolved contradiction, if any.

Mark source facts (direct evidence), inferences, and residual uncertainty separately. If an
answer depends on product intent or another user-owned decision, name that
decision instead of silently deciding it.

Do not paste large files or logs into the result. Preserve full material at its
durable path and quote only the minimum decisive fragment.

## 4. Use discriminating checks

When inspection leaves competing explanations, run the smallest read-only check
that distinguishes them.

Examples:

- resolve the real call graph rather than guessing from names;
- inspect the actual Git diff and branch;
- compare a failing and working fixture;
- render or open the real artifact;
- query the current schema or generated manifest;
- count occurrences with a deterministic script.

Do not mutate production state merely to collect evidence. If mutation is
required to answer the question, stop and recommend `run-discovery-spike`.

## 5. Stop when the decision is supported

Do not continue exploring after the question has a sufficiently supported
answer. Record residual uncertainty rather than broadening scope.

A useful evidence pack is materially smaller than the material inspected.

## 6. Write the artifacts

Write:

- `evidence.md` — readable conclusions and traceable support;
- `evidence.json` — conforming to `schemas/evidence.schema.json`.

Use this Markdown shape:

```markdown
# Evidence: <question>

## Conclusion
## Claims and support
### Claim 1
Evidence
Inference
Confidence

## Contradictions
## Unknowns
## Recommended follow-up
## Source and artifact paths
```

`recommended_follow_up` should be one of:

- no further work;
- planning can proceed;
- run a bounded spike;
- request one material decision;
- inspect a named missing source.

Do not dispatch the follow-up. Return the artifact paths to the controller.
