# Plan: Peer-thread planning and milestone execution

## Goal

Keep planning in a Sol decision thread and execute each decision-ready milestone
in a fresh peer task whose model and reasoning are deliberately selected and
actually passed to native task creation when governing user instructions
authorize that routing. Never describe Luna routing in the capsule while
silently allowing an unrelated configured default to execute the work.

## Current context

- `plan-work` currently says to recommend Luna XHigh or Luna High.
- `codex-thread-handoff` currently passes `model` and reasoning only when the
  user names them in the immediate request; otherwise it puts the recommendation
  in text and omits the native fields.
- The global `AGENTS.md` template already defines Luna XHigh for substantial
  milestones and Luna High for bounded/mechanical work, but does not explicitly
  state that this policy authorizes native task-creation parameters.
- The observed result was a capsule recommending Luna XHigh while
  `create_thread` used the configured Sol Medium default. That violates the
  workflow's intended execution topology.
- Native `create_thread` exposes concrete `model` and `thinking` fields but
  requires model overrides to be authorized by the user. Applicable user-owned
  `AGENTS.md` routing policy is therefore the durable authorization source; the
  installed skill alone is not.

## Proposed design

Make routing a resolved task-creation contract, not descriptive metadata.
`plan-work` selects a concrete role, model family, and reasoning level from the
applicable routing policy and writes that resolved pair into the execution
capsule. `codex-thread-handoff` checks the current native tool schema and passes
both `model` and `thinking` when the user request or governing user/AGENTS
instructions authorize the pair.

The default authorized mappings in the shipped global template are:

- substantial decision-ready milestone: `gpt-5.6-luna`, `xhigh`;
- bounded/mechanical milestone: `gpt-5.6-luna`, `high`;
- independent normal code review: `gpt-5.6-luna`, `xhigh`;
- critical architecture/security review or planning: `gpt-5.6-sol`, `high`.

Resolve against models and reasoning levels actually advertised by the current
`create_thread` schema. If the authorized pair is unavailable or rejected,
report the dispatch failure once and do not retry, silently substitute another
model, or fall back to the configured default. If no applicable user routing
authorization exists, preserve the native permission boundary: omit overrides
and state that routing was not enforced rather than claiming it was.

`execute-milestone` continues to describe the model appropriate for the current
execution role, but must state that a fresh peer's resolved route is applied by
the creator. A skill cannot change the already-running task's model.

## Contracts and invariants

- A resolved route contains a concrete native model id and reasoning value, not
  only “Luna XHigh” prose.
- Governing user instructions, including an applicable user-owned `AGENTS.md`,
  may explicitly authorize a routing policy; the most specific applicable user
  instruction wins.
- A plugin installation without such an instruction is not authorization to
  override native task settings.
- When authorized and supported, `create_thread` receives both `model` and
  `thinking`; the capsule mirrors the same pair.
- Unsupported or rejected routing fails truthfully without retry or silent
  model substitution.
- Peer creation remains one non-blocking START operation with no polling,
  inherited chat, child agent, or fallback transport.
- Planning, execution, Git, completion-callback, and ownership boundaries remain
  unchanged.

## Program scope and non-goals

In scope: `plan-work`, `codex-thread-handoff`, `execute-milestone`, the global
`AGENTS.md` template, README routing documentation, deterministic validator
contracts, plugin version metadata, and the canonical plan.

Non-goals: changing Codex's native permission schema; selecting a model for an
already-running task; adding a custom task launcher; automatic plugin install;
publishing or installing the new plugin build; updating downstream pinned
commits; pushing or opening a PR without separate authorization; or changing
the seven audit skills.

## Milestone M1 — Enforce authorized model routing at peer creation

### Outcome

The plugin source deterministically converts an authorized routing policy into
the actual `create_thread` `model` and `thinking` arguments, and its docs/tests
can no longer describe a capsule-only recommendation as successful routing.

### Mutable ownership

- `plugins/personal-workflow-skills/skills/plan-work/SKILL.md`
- `plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md`
- `plugins/personal-workflow-skills/skills/execute-milestone/SKILL.md`
- `plugins/personal-workflow-skills/.codex-plugin/plugin.json`
- `templates/AGENTS.md`
- `README.md`
- `scripts/validate.py`
- this canonical plan for terminal evidence

### Protected surfaces

- all seven audit skills and their metadata/resources;
- marketplace identity, installation policy, and plugin name;
- native task tool implementation and permission contract;
- Git remote state and installed plugin cache;
- unrelated repositories and downstream pins.

### Implementation boundary

1. Change planning from “recommend a model” to resolving the authorized exact
   model/reasoning pair for the milestone.
2. Change START to apply that pair to native `create_thread` when authorized and
   supported, with explicit unavailable/no-authorization behavior.
3. Preserve one-call/no-retry semantics and make confirmed dispatch distinct
   from confirmed model enforcement.
4. Align `execute-milestone`, the global template, and README with the enforced
   task-creation behavior and current-task limitation.
5. Update deterministic validation so the old immediate-request-only/capsule
   behavior fails and the authorization, exact-pair, schema-check, and no-silent-
   fallback contracts are required.
6. Bump plugin build metadata monotonically so a published marketplace upgrade
   can distinguish this source from `0.1.0+codex.20260818104438`.

### Acceptance criteria

- No workflow instruction says an authorized resolved route is merely a
  recommendation.
- Handoff requires passing both `model` and `thinking` for an authorized,
  schema-supported route.
- Applicable user/AGENTS policy is explicitly recognized as authorization;
  skill installation alone is explicitly insufficient.
- Unsupported/rejected routes stop without default-model or alternate-model
  substitution.
- Template defaults unambiguously authorize the exact Luna/Sol mappings above.
- Validator rejects the previous immediate-prompt-only contract and requires
  the new behavior.
- Audit skill tree is byte-for-byte unchanged.
- Plugin version is newer and all metadata remains valid.

### Validation

- `python3 -B scripts/validate.py`
- quick validation for `plan-work`, `codex-thread-handoff`, and
  `execute-milestone` using the bundled `quick_validate.py`
- JSON parsing of plugin and marketplace manifests
- before/after hash comparison for all audit-skill files
- searches proving removal of the stale recommendation-only contract
- `git diff --check` and complete diff self-review

### Promotion gate

All validation passes, source/docs/template agree on the same authorization and
routing behavior, the diff contains no audit-skill change, and open P0/P1
findings are zero. Create one local green commit; do not push or publish.

### Review requirement

Self-review the permission boundary and failure semantics carefully. A separate
review/publish milestone is required only when the user authorizes remote
release work.

### Successor milestone

After separate push/PR/merge authorization: review and publish the plugin
change, then update downstream pinned plugin commit/version (including
SprintAct) and validate a real fresh-task dispatch using the installed build.

## Assumptions

- The user-provided global routing instructions in this thread are explicit
  authorization for the exact default mappings above.
- “Finds appropriate” means deterministic selection from the governing routing
  policy and task class, not unconstrained model choice.

## Open findings

- None blocking for local source implementation. Publication and downstream pin
  updates remain outside M1 until separately authorized.

## Current review log

- 2026-08-18: Reopened the workflow plan after observing capsule-only Luna
  routing execute on the configured Sol Medium default. Selected enforced native
  parameters under explicit user/AGENTS policy while preserving the native
  no-authorization boundary.

## Next execution

Milestone: M1

Recommended executor: `gpt-5.6-luna` with `thinking=xhigh`

Planning thread: `01a01405-e5b1-7dd1-b540-5fffc15538b0`

Plan path: `docs/reviews/peer-thread-workflow.md`

Owned surfaces: the three workflow skills, global template, README, validator,
plugin version metadata, and factual completion update in this plan.

Protected surfaces: audit skills, marketplace identity/policy, native tool
implementation, installed cache, downstream repositories/pins, and remotes.

Acceptance: enforce authorized exact model/reasoning at peer creation; fail
closed on unsupported routing; align validation/docs; preserve audits; create
one local green commit; no push or publication.

Escalate only for: evidence that governing AGENTS instructions cannot count as
user authorization under the native tool contract, unavailable exact mappings
in the current schema, or a required change outside owned surfaces.

Completion callback: send one terminal packet to
`01a01405-e5b1-7dd1-b540-5fffc15538b0` with commit, validation, permission-
boundary evidence, version, and any publication/downstream gate. No routine
updates.
