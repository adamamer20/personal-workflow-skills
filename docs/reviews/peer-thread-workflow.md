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

M2 — publish a ready plugin PR.

## Milestone M2 — Publish the routed plugin change for review

### Outcome

The complete green branch is pushed and a ready-for-review PR targets `main`
with the permission-boundary rationale and validation evidence. No merge occurs
in this milestone.

### Mutable ownership

- local branch `agent/enforce-peer-model-routing` and its existing commits;
- validation-only repairs inside the M1 owned surfaces if a gate regresses;
- remote branch of the same name and its pull request metadata.

### Protected surfaces

- audit skills, marketplace identity/policy, installed plugin cache, downstream
  repositories and pins, unrelated local branches/worktrees, and `main` history.

### Dependencies

- M1 commits `e580c1f`, `0aaf014`, and `aed7502` are attached to the branch.
- GitHub CLI is authenticated as `adamamer20`; `origin` is the canonical remote.
- The user has explicitly authorized push, PR, review, merge, installation, and
  downstream updates for this program.

### Implementation boundary

1. Inspect branch status, complete diff against `origin/main`, commit history,
   and remote/default-branch identity.
2. Re-run decisive local validators if no newer evidence exists; repair only a
   real regression within M1 ownership.
3. Push the current branch with tracking.
4. Open a non-draft PR to `main` explaining the observed Sol-default failure,
   enforced authorized route, no-authorization behavior, failure semantics,
   version bump, unchanged audit skills, and validation.
5. Do not self-approve, merge, install, or update downstream pins in this
   milestone.

### Acceptance criteria

- Remote branch head equals the final local green head.
- A ready PR targets `main`, contains the full three-commit change, and accurately
  documents the authorization boundary and checks.
- No unrelated file or commit appears in the PR.
- Local source validation remains green and open P0/P1 self-review findings are
  zero.

### Validation

- `python3 -B scripts/validate.py`
- bundled quick validators for all three workflow skills
- JSON parse for plugin and marketplace manifests
- `git diff --check origin/main...HEAD`
- complete diff/name-status review and audit-skill path exclusion
- GitHub PR metadata confirming ready state, base, head, and head SHA

### Promotion gate

Branch push and ready PR creation are confirmed with local/remote SHA equality.
Independent review and merge remain M3 gates.

### Review requirement

M3 uses a fresh independent peer. The M2 owner must not approve or merge its own
change.

### Successor milestone

M3 — independent review and merge.

## Milestone M3 — Independently review and merge the plugin

Outcome: a fresh peer reviews the complete PR, records findings, and merges only
with zero open P0/P1 and green required checks. Repairs return to a fresh
implementation context rather than expanding the reviewer into an owner.

Promotion gate: PR is merged to `main`; record the immutable merge commit and
published plugin version. A self-authored GitHub approval is not claimed when
GitHub identity rules prohibit it; record an independent review result
truthfully.

Successor milestone: M4 — install and smoke-test the merged build.

## Milestone M4 — Install and prove fresh-task routing

Outcome: upgrade the marketplace/plugin to the merged commit, start a new Codex
task, and retain evidence that an authorized route passes the exact native
`model`/`thinking` pair. Also verify the documented no-authorization and
unsupported/rejected decisions without creating an unintended fallback task.

Promotion gate: installed version and marketplace ref match the merged source;
the fresh authorized dispatch is confirmed; no fallback or duplicate task is
created for negative cases.

Successor milestone: M5 — update SprintAct's pinned commit/version and propagate
the new workflow pin through its required PR/review/merge and named active
worktrees.

## Assumptions

- The user-provided global routing instructions in this thread are explicit
  authorization for the exact default mappings above.
- “Finds appropriate” means deterministic selection from the governing routing
  policy and task class, not unconstrained model choice.

## Open findings

- M1 has no open P0/P1 finding. Publication, installed-build verification, and
  downstream pin updates are now authorized and ordered as M2–M5.

## Current review log

- 2026-08-18: Reopened the workflow plan after observing capsule-only Luna
  routing execute on the configured Sol Medium default. Selected enforced native
  parameters under explicit user/AGENTS policy while preserving the native
  no-authorization boundary.
- 2026-08-18: M1 source implementation completed locally. The workflow skills,
  template, README, validator, and plugin metadata now resolve and pass the
  authorized native `model`/`thinking` pair, omit overrides without governing
  authorization, and fail closed on unsupported or rejected routes. `python3
  -B scripts/validate.py`, the three bundled `quick_validate.py` checks, JSON
  parsing for plugin and marketplace manifests, `git diff --check`, and stale-
  contract searches passed. All 21 audit-skill file hashes are byte-for-byte
  unchanged from the pre-edit baseline. Version is
  `0.1.1+codex.20260818104438`; one local green commit is created, with push,
  publication, installation, and downstream pin updates intentionally open.

## Next execution

Milestone: M2 — publish the routed plugin change for review

Resolved route: `model=gpt-5.6-luna`, `thinking=xhigh`

Routing authorization: the user explicitly said “vai continua” after the exact
push/PR/merge/install/downstream operations were enumerated.

Planning thread: `01a01405-e5b1-7dd1-b540-5fffc15538b0`

Plan path: `docs/reviews/peer-thread-workflow.md`

Owned surfaces: `agent/enforce-peer-model-routing`, validation-only repairs in
M1 surfaces, its remote branch, and a ready PR to plugin `main`.

Protected surfaces: audit skills, marketplace identity/policy, native tool
implementation, installed cache, downstream repositories/pins, and remotes.

Acceptance: decisive validation green; exact local/remote SHA equality; complete
ready PR to `main`; zero unrelated paths or open P0/P1; no merge/install/pin
mutation yet.

Escalate only for: authentication/remote ambiguity, a validation regression
requiring scope outside M1, unexpected branch history, or inability to create a
truthful ready PR.

Completion callback: send one terminal packet to
`01a01405-e5b1-7dd1-b540-5fffc15538b0` with final SHA, PR URL/metadata,
validation, and exact M3 gate. No routine updates.
