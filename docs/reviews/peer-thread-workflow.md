# Plan: Deterministic Codex workflow controller

## Current outcome and next gate

Deliver reliable controller-owned workflow execution and install a verified
matching tool/plugin. User update 2026-09-07 adds evidence-led instruction
simplification and explicitly separates this current plan from history.

[Workflow history](peer-thread-workflow-history.md) retains the previous plan
verbatim, including uncommitted planning/recovery detail and historical capsule
identities. It is not an execution queue. Read the selected design below when
working on that milestone; do not reload all history into every task.

| Work | Current state / dependency | Next gate |
| --- | --- | --- |
| refresh-start-intent | Existing uncommitted recovery candidate, single original owner; acceptance unverified here | Required checks, successor commit, one focused Sol Medium review of ROTATION-START-REENTRY-DOUBLE-START |
| evidence-led-delivery-instructions | User-directed instruction maintenance implemented locally; no worker dispatch | Focused consistency review and instruction/plugin validation |
| Current plan/history separation | Direct planner-owned reference migration requested by user | Historical capsule identity and active-plan compilation checks |
| Instruction plugin installation | Plugin-only 0.1.12+codex.20260907000000; documented CLI route avoids publishing dirty runtime | Plugin version, enabled state and byte parity |
| Controller runtime installation | Depends on accepted refresh-start-intent; canonical installer packages all checkout bytes | Original controller retains cutover authority |
| Other prior program successors | Deferred, not selected by historical headings | Reassess prerequisites after the above close |

Installed preflight: codex-flow 0.2.0; installed candidate schema 21, live ledger
schema 20 requires migration. Source recovery targets schema 22. The user unit
is inactive with MainPID=0. These observations do not prove a prior start was
never submitted or authorize bypassing the existing pending-intent guarantee.
Do not install the dirty recovery merely to deliver instruction changes.

## Ownership and preserved boundaries

Workspace: `/home/adam/personal-workflow-skills.worktrees/python-sdk-controller`,
branch `agent/python-sdk-controller`; inspected HEAD
`b3478d1b0ced5017b685f974ff82daf92e20f88b`. Preserve the pre-existing dirty
ledger/service and four recovery test files, plus conversational-tui artifacts.
The planner owns this direct user-requested instruction maintenance: plan/history,
README, root and template AGENTS, plan-work/execute-milestone/recover-milestone/
review-work skills, historical fixture references in tests/test_plan_compilation.py,
and the matching version constants in plugin/marketplace manifests and installer.
The exact history relocation is exempted from the large-new-file pre-commit hook;
all other paths retain the existing size gate. Targeted global AGENTS synchronization
preserves unrelated local policy and follows independent consistency review.
Only installer PLUGIN_VERSION changes; no installer behavior or runtime code does.
No runtime, global configuration, authentication or downstream change belongs
to the history split. Preserve source-block hashes and compiler behavior.

This worktree remains the sole local integration trunk. There is no concurrent
mutable fan-out or frozen DAG base for this update; no checkpoint SHA is
fabricated and the dirty baseline stays protected. Future fan-out uses physical
sibling Git worktrees only from a verified owned commit. Reviews bind to an
exact lane tip or commit range; a repair adds a successor commit after staged
inspection and `git diff --cached --check`. The integration owner verifies
ancestry, defaults to a merge commit for actual fan-out and validates the new
trunk tip. This plan does not authorize push, rebase, history rewrite, discard,
remote mutation or implicit cleanup.

## Current recovery — durable refresh start intent

Retain the exact frozen [recovery design and six-path ownership map](peer-thread-workflow-history.md#current-recovery--durable-refresh-start-intent)
as the active contract for refresh-start-intent only. Its pending-intent,
fencing, migration, re-entry and P1 proof requirements are unchanged. The
capsule below is retained byte-for-byte. Its reference to “Current recovery”
means that explicitly selected design, not unrelated historical instructions.
No replacement worker, installation or retry is authorized by this relocation.

## Instruction update — evidence-led delivery

User correction explicitly requires replacing/refactoring old rules, not merely
adding new ones. Direct instruction maintenance supersedes the archived worker capsule; no
separate implementation worker is dispatched for these edits.
Risk discovery/first vertical, lightweight history, bounded scope, minimal evidence,
proportional review/gates, costly-guarantee challenge, controller-owned protocol,
causal diagnosis/rollover, mergeability and lightweight events are incorporated.
Duplicate semantic/workspace/recovery prose is consolidated. Existing configured
models and worker leaf authority remain unchanged; optional free-subagent runtime
experiments from the supplied analyses are not silently enabled by prompt edits.
The remaining independent risk is consistency without loss of protected guarantees.

Retain the bounded [six-surface instruction design](peer-thread-workflow-history.md#instruction-update--evidence-led-delivery),
with the following user-requested refinement: current plan contains outcome,
current state, risks, ready/deferred milestones and next gate; history contains
closed/superseded decisions and evidence. Keep selected active design details
reachable by exact links. Historical entries cannot select work. Never move an
active contract without retaining its explicit authority and references.

Use existing discovery skills for material unknowns before freezing speculative
architecture; require the first feasible complete production vertical and a
concrete blocker for prerequisite infrastructure. Keep ready-milestone maps,
necessary safety gates, independent visual judgment and single controller
ownership. Two attempts without new causal evidence or improvement trigger
bounded diagnosis, not a third identical repair or automatic model escalation.
No runtime schema, unrestricted worker subagents or automatic retry changes.

Validate uncertain work, small known repair, repeated causal failure and an
existing mandatory gate against the updated instructions. Run workflow-assets
and validator checks; full make check before packaging. Do not weaken assertions
to erase a conflict. Existing accepted capsules retain required reviews.

## Plan maintenance

Replace current status when it changes; append a concise closure/decision entry
to history with outcome, exact candidate, discriminating evidence and remaining
limits. Keep one current source of readiness and ownership. New history does
not duplicate full transcripts, test logs or whole plan snapshots; this initial
verbatim snapshot preserves prior references during migration. Already-bound
plan revisions and capsule identities are not retroactively rewritten.

## Next execution — refresh-start-intent

```python
ModelFacingCapsule(
    schema_version=1,
    objective="Close ROTATION-START-REENTRY-DOUBLE-START at b3478d1b0ced5017b685f974ff82daf92e20f88b using durable intent before systemctl start and restart-safe exact replacement reconciliation without a second unresolved start.",
    decomposition=(
        "Add the frozen service_refresh_starts table and existing-ledger APIs with schema 22 migration and supported predecessor 21 handling, preserving all historical rows.",
        "Arm exactly once immediately before start, retain pending on every uncertain result and reconcile exact healthy/acquired-stopped replacement without same-predecessor replay.",
        "Make default arm_harness_refresh_fence atomically reject any pending intent; permit only the exact paired pending-start/acquired-unhealthy-authority CAS from service recovery, keeping installer source protected.",
        "Replace the violating second-start test with two-invocation/crash/concurrency/public-installer regressions; preserve all accepted refresh guarantees, run required gates and commit six owned paths.",
    ),
    acceptance_modes=(AcceptanceMode.ARCHITECTURE,),
    acceptance_criteria=(
        "Nonzero/raised/uncertain prior start plus unchanged fenced predecessor blocks every fresh start, despite inactive manager/MainPID zero/socket absence; one intent insertion grants at most one invocation permission.",
        "Crash before manager call after durable intent is conservatively blocked; exact healthy replacement resolves without start and exact acquired unhealthy replacement permits later retry only under its proven newer epoch.",
        "Intent/resolve replay and concurrent attempts are idempotent, conflicting identity fails, and no exception clears pending state or bypasses existing profile/v18/current/manager/executable/claim/socket proof.",
        "Default installer and stale-read refresher fencing reject any pending start within the write transaction before authority/tool mutation; only exact paired unhealthy-replacement CAS may fence, while healthy reconciliation never fences or starts.",
        "Schema 22 migration preserves old history and supports current-topology predecessor 21; full/affected checks pass and one successor is delivered for one focused Sol Medium P1 re-review only.",
    ),
    mutable_surfaces=("src/codex_flow/ledger.py", "src/codex_flow/service.py", "tests/test_ledger_integrity.py", "tests/test_service_lifecycle.py", "tests/test_plugin_installation.py", "tests/test_controller_execution.py"),
    protected_surfaces=("docs/reviews/peer-thread-workflow.md", "docs/reviews/conversational-tui", "scripts/install_personal_workflow_skills.py", "src/codex_flow/domain.py", "src/codex_flow/contracts.py", "src/codex_flow/harness.py", "src/codex_flow/ipc.py", "workflow.toml", "AGENTS.md", "config/test-partitions.toml"),
    authorities=(ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),),
    prompt="Use recover-milestone as Astra Low bounded recovery for refresh-start-intent at exact b3478d1b0ced5017b685f974ff82daf92e20f88b in same workspace. Follow Current recovery durable contract and six-path map. Existing observations cannot prove absence after a submitted start: pending survives crashes and blocks replay, with no elapsed-time/returncode inference. Add only frozen existing-ledger table/APIs/schema22 and predecessor21 support; preserve old history and all accepted lifecycle/profile guarantees. Prove public two-invocation first checkpoint, intent-before-command crash and exact replacement resolution, migration/concurrency tests; no sidecar/controller/public reset or manual unit mutation. No live install/service/ledger/global/auth/provider/SDK/downstream action, subagents/peers/reviews/workflow-control or retry. Preserve dirty plan/TUI. Run affected/full make check, self-review/stage allowed paths/git diff --cached --check and successor commit without amend. Return exact result with one focused Sol Medium ROTATION-START-REENTRY-DOUBLE-START review pending. Missing fixture ownership returns exact counterexample before expansion; parent owns lifecycle/cutover reconciliation.",
    recovery_policy="completion_biased",
)
```
