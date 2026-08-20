# Plan: Peer-thread planning and milestone execution

## Goal

Keep planning in a Sol decision thread and execute each decision-ready milestone
in a fresh peer task whose model and reasoning are deliberately selected and
actually passed to native task creation when governing user instructions
authorize that routing. Never describe Luna routing in the capsule while
silently allowing an unrelated configured default to execute the work.

## Current context

- M1-M4 routing and recovery source history is represented by merged base
  `a12c206e4c6cd420cdc228b04e36f7e8bf92cda8`.
- M5 hook source is implemented in the isolated repair worktree. Its current
  plugin source version is `0.1.4+codex.20260820180447`; the hook-enabled build
  has not been published, installed, or trusted from this worktree.
- Review of M5 reopened three parser/identity P1s. The local repair now uses
  narrow native response allowlists, rejects Unicode whitespace in peer ids,
  and preserves permanent duplicate blocking for uncertain create results.
- M5 promotion remains pending an independent green review of the final local
  repair commit. M6 is the next milestone; publication, installation, and hook
  trust remain separate actions and are not implied by source validation.

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
- visual-judgment implementation: `gpt-5.6-sol`, `medium`;
- critical visual direction, repeated visual failure remediation, or final
  qualitative promotion review: `gpt-5.6-sol`, `high`;
- Luna implementation/review loop after two unsuccessful repair cycles:
  `gpt-5.6-sol`, `medium` in a fresh context;
- Sol Medium implementation/review loop after two further unsuccessful repair
  cycles: `gpt-5.6-sol`, `high` in a fresh context;
- independent normal code review: `gpt-5.6-luna`, `xhigh`;
- critical architecture/security review or planning: `gpt-5.6-sol`, `high`.

Classify by the judgment required for acceptance before applying the generic
milestone-size mapping. Slides, landing pages, frontend/UI work, visual systems,
and rendered-document quality use Sol Medium when acceptance depends on visual
judgment. Sol High owns novel or critical visual direction, weak or conflicting
references, repeated visual failure, and final qualitative promotion review.
Luna remains available only for visually adjacent work whose target and
acceptance are frozen and whose remaining execution is mechanical and
objectively verifiable.

Implementation/review loops have a two-stage circuit breaker. Two unsuccessful
cycles in Luna escalate the bounded continuation to fresh Sol Medium; two
further unsuccessful cycles in Sol Medium escalate once to fresh Sol High. If
Sol High exhausts ordinary repair, return a truthful terminal `BLOCKED` or
`FAILED` outcome to the planning owner rather than forming an indefinite loop.

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
- Visual judgment takes precedence over generic milestone size: Sol Medium is
  the default implementation route and Sol High is the critical-direction or
  final-promotion route.
- Review-loop escalation preserves the diff, evidence, findings, scope, and
  acceptance gate; it changes model/context and never weakens the gate.
- Luna escalates to Sol Medium after two unsuccessful implementation/review
  cycles; Sol Medium escalates once to Sol High after two further cycles; Sol
  High failure terminates truthfully rather than looping.
- A failed or uncertain START is reconciled read-only and never retried
  automatically. Delayed recovery may discover the original task but never
  creates one.
- An interrupted known peer is resumed in the same thread/worktree after one
  non-waiting status snapshot. Active work is left alone; completed work may
  only republish its established callback packet.
- Replacement requires an explicit user decision after the previous owner is
  proven unavailable or terminal; server/client errors and missing callbacks do
  not implicitly authorize a duplicate owner.
- Peer creation remains one non-blocking logical START operation with no
  polling, inherited chat, child agent, or fallback transport. After an error,
  one non-waiting peer-list reconciliation may recover a created task id; no
  result authorizes another create call.
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
3. Preserve one logical START and zero create retries, reconcile one
   failed-looking result read-only, and make confirmed dispatch distinct from
   confirmed model enforcement.
4. Align `execute-milestone`, the global template, and README with the enforced
   task-creation behavior and current-task limitation.
5. Update deterministic validation so the old immediate-request-only/capsule
   behavior fails and the authorization, exact-pair, schema-check, and no-silent-
   fallback contracts are required.
6. Bump plugin build metadata monotonically so a published marketplace upgrade
   can distinguish this source from `0.1.0+codex.20260818104438`.
7. Route subjective visual implementation to Sol Medium, critical visual work
   and qualitative promotion to Sol High, and reserve Luna for frozen,
   mechanical visually adjacent work.
8. Add a two-stage review-loop circuit breaker: Luna to fresh Sol Medium, then
   Sol Medium to fresh Sol High, with terminal failure after Sol High.
9. Add retry-free delayed START reconciliation and same-thread recovery for
   interrupted peers or missing terminal callbacks.

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
- Visual-judgment routing and the two-stage review-loop circuit breaker are
  consistent across the plan, execution skill, template, README, and validator.
- Handoff recovery leaves active work alone, resumes an interrupted task in
  place, republishes a completed task's callback without mutation, and never
  creates a replacement without separate explicit authorization.
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

Outcome: an independent reviewer or repository review check reviews the complete
final PR head, records findings, and the integration owner merges only with zero
open P0/P1 and green required checks. Repairs return to a fresh implementation
context rather than expanding the reviewer into an owner.

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

Successor milestone: M5 — deterministic lifecycle hooks for retry-free native
handoff recovery.

## Milestone M5 — deterministic lifecycle hooks for retry-free handoff recovery

### Outcome

Package plugin-bundled lifecycle hooks that validate and safely repair native
`create_thread` payloads, persist a privacy-bounded attempt fingerprint before
dispatch, prevent same-turn, unresolved, and terminally unsafe duplicate
creates, classify native results, reserve at most one read-only `list_threads`
reconciliation, and
restore unresolved context on same-session resume. Hooks are advisory recovery
state, not a second native transport.

### Mutable ownership

- `plugins/personal-workflow-skills/hooks/`
- `plugins/personal-workflow-skills/.codex-plugin/plugin.json`
- `plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md`
- `README.md`
- `scripts/validate.py`
- focused tests under `tests/`
- this canonical plan for M5 evidence

### Protected surfaces and non-goals

- all seven audit skills/resources, marketplace identity and policy, and native
  Codex task tools/server behavior;
- routing authorization/model policy, global `~/.codex` state, downstream
  repositories, and unrelated branches/worktrees;
- no manifest `hooks` override when default `hooks/hooks.json` discovery works;
- no hook invocation of native tools, polling, retries, unarchive/replace/model
  switching, continuation loop, prompt-body/raw-response persistence, push, PR,
  merge, or plugin installation without separate authorization.

### Implementation boundary

1. Add synchronous `PreToolUse`, `PostToolUse`, `Stop`, and `SessionStart`
   handlers in `hooks/hooks.json` using the released command-hook schema and
   `PLUGIN_ROOT`/`PLUGIN_DATA` environment contracts.
2. Before `create_thread`, validate the object and deny malformed/conflicting
   project targets. Move/remove only an unambiguous top-level `projectId`; hash
   bounded title/target/model/thinking plus a bounded prompt digest without
   storing the prompt body. Persist atomically before dispatch and block
   same-turn, unresolved, or terminally unsafe duplicates.
3. After `create_thread`, classify a real `threadId` as confirmed and a queued
   `clientThreadId` as queued; every other result is uncertain/error and asks
   for one read-only reconciliation. Before and after `list_threads`, reserve
   and classify exactly one exact title + target-project-context snapshot as
   found, not-found, or ambiguous.
4. Surface unresolved state once at `Stop`, honor `stop_hook_active`, and restore
   bounded unresolved context once on same-session `resume`. Internal hook
   errors fail open with a warning.
5. Document the explicit hook trust-review gate truthfully, bump plugin build
   metadata monotonically, extend deterministic validation, add focused tests,
   and prove the audit-skill tree is unchanged.

### Acceptance and promotion gates

- safe payload repair, conflict denial, duplicate prevention,
  confirmed/queued/error classification, exact reconciliation, bounded Stop,
  resume, privacy-bounded state, and fail-open behavior have focused tests;
- `python3 -B scripts/validate.py`, hook/skill validators, JSON parsing,
  Python compilation, and `git diff --check` pass;
- hooks are synchronous, use default discovery, never call native tools, and
  do not persist prompt bodies or raw tool responses;
- plugin version is newer than `0.1.3+codex.20260820180447`; audit-skill paths
  remain byte-for-byte unchanged; open P0/P1 findings are zero;
- create one safe local commit only after complete self-review. Publication,
  installation, and hook trust remain M6/user-owned gates.

### Successor milestone

M6 — independently review and publish/install the hook-enabled plugin build.

## Milestone M6 — independently review and publish/install the hook build

### Outcome and dependency

Independently review the complete M5 diff through its final local repair commit.
Only after zero open P0/P1 findings and all M5 gates remain green may an
explicitly authorized integration owner publish and install the exact
`0.1.4+codex.20260820180447` source. Hook trust is a separate user decision and
is never inferred from installation.

### Mutable ownership

- read-only review of the complete M5 source and evidence;
- focused M5 repair surfaces only if the planning owner dispatches a finding;
- remote branch/PR/merge and local plugin installation only under explicit
  authority in the M6 integration task.

### Protected surfaces and non-goals

- all seven audit skills/resources, marketplace identity/policy, native Codex
  tools, dirty primary checkout state, downstream repositories, and unrelated
  worktrees/remotes;
- no publication, installation, or hook trust during M5 repair/review; no
  self-authored review result substitutes for independent promotion evidence.

### Acceptance and promotion gates

The independent reviewer reports zero P0/P1 on the exact final M5 commit; all
M5 unit, validator, JSON, compilation, diff, protected-tree, and no-native-call
gates pass. A later authorized integration records the immutable published and
installed commit/version. Trust of `hooks/hooks.json` remains explicitly open
until the user reviews and approves it.

## Assumptions

- The user-provided global routing instructions in this thread are explicit
  authorization for the exact default mappings above.
- “Finds appropriate” means deterministic selection from the governing routing
  policy and task class, not unconstrained model choice.

## Open findings

- Source self-review has no open P0/P1 finding after the first Sol Medium M5
  repair cycle. Independent promotion review of the final repair commit remains
  open; no hook-build publication, installation, or trust is claimed.

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
- 2026-08-18: M2 validation remained green, remote branch
  `agent/enforce-peer-model-routing` matched local head `0e4a427`, and ready PR
  #3 opened against `main` with exactly the eight intended paths. No merge,
  installation, or downstream change occurred.
- 2026-08-19: Reviewed recent native creation failures. Confirmed one malformed
  top-level `projectId`, one `Unknown projectId` response that nevertheless
  created a task and led to a duplicate retry, and ambiguous generic failures;
  successful worktree creation normally returned queued `clientThreadId`.
  Tightened START preflight and payload shape, prohibited all create retries,
  and added one read-only post-error reconciliation snapshot.
- 2026-08-19: Reviewed missing Luna returns. Observed an archived planning
  target and a non-routable environment-derived `hostId`; the existing executor
  also required callbacks only for green completion. Added exact callback-route
  ownership, unarchived-planner availability, one callback for every terminal
  status, and a recoverable unsent-packet final state.
- 2026-08-20: Added judgment-first visual routing after repeated weak visual
  outcomes: Sol Medium for normal visual implementation, Sol High for critical
  direction, repeated visual failure, or final qualitative promotion, and Luna
  only for frozen mechanical execution. Added the authorized review-loop
  circuit breaker Luna -> fresh Sol Medium -> fresh Sol High, with terminal
  `BLOCKED`/`FAILED` after Sol High instead of indefinite repair/review cycles.
  Target plugin build is `0.1.3+codex.20260820180447`.
- 2026-08-20: Audited recent native failures. Malformed client payloads produced
  `invalid arguments`; current or stale project identifiers produced unknown-
  project failures; one schema-valid START returned a generic server/tool error
  while the target host was unavailable for reconciliation. Recent execution
  turns also recorded `interrupted`, while the durable task thread remained
  resumable and later accepted follow-up turns. Added RECOVER_START for delayed
  read-only reconciliation and RECOVER_THREAD for one same-thread resume or
  callback republication. No automatic create/send retry, unarchive, model
  switch, or replacement is introduced.
- 2026-08-20: M5 hook implementation and the first escalated target/reconciliation
  repair landed locally through `d9de620`; 22 focused tests and all deterministic
  gates passed, but independent review reopened contradictory list/create
  envelope and Unicode-whitespace P1s. The first Sol Medium repair cycle now
  uses exact native-envelope allowlists and permanent duplicate blocking. M5
  still requires independent promotion review; no publish/install/trust action
  has occurred for version `0.1.4+codex.20260820180447`.

## Next execution

Milestone: M6 — independently review the final M5 hook build, then publish and
install only if the planning owner supplies the required authority.

Execution mode: fresh independent review context first; a distinct authorized
integration action owns any later remote or installed-state mutation.

Plan path: `docs/reviews/peer-thread-workflow.md`

Owned surfaces: read-only review of the M5 diff and evidence. Conditional
integration ownership is limited to its exact remote branch/PR/merge and the
exact `0.1.4+codex.20260820180447` plugin installation.

Protected surfaces: audit skills, marketplace identity/policy, native tool
implementation, dirty primary checkout state, downstream repositories/pins,
unrelated remotes/worktrees, and hook trust configuration.

Acceptance: independently verify the exact final M5 commit and its
permission/failure semantics; zero open P0/P1; required checks green. Only an
authorized integration task may then publish and install the unchanged reviewed
head, recording its immutable commit and version
`0.1.4+codex.20260820180447`. Installation does not authorize hook trust.

Escalate only for: a P0/P1 finding, changed reviewed head, non-green required
check, merge conflict, or missing publication/install authority.
