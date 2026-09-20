# Evidence: controller ownership of App-created worktrees

## Conclusion

The reported controller behavior follows the current instructions but produces
the wrong operational result for an App-created, task-bound worktree. The policy
conflates three different identities: the saved Codex project used by
`create_thread`, the exact mutable checkout already owned by the controller, and
the long-lived integration worktree that will later receive the promoted commit.

The typed SDK controller can already represent the checkout as
`existing_worktree` and verifies it by shared Git common-directory identity. The
gap is primarily in the App-native handoff/rollover instructions: they require a
saved project whose path equals the execution checkout, forbid `fork_thread`,
and only recognize semantic sibling managed worktrees. An App-created checkout
under `~/.codex/worktrees/...` therefore has no valid native continuation route.

## Claims and support

### Claim 1: project registration is incorrectly treated as workspace ownership

Evidence: `plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md`
requires the saved project's real path to equal the exact execution path and
returns `workspace_status: unsupported` otherwise.

Inference: absence from `list_projects` proves only that `create_thread` cannot
start a new local project task at that path. It does not prove that the current
controller does not own its already-open task-bound checkout.

Confidence: high.

### Claim 2: the topology policy excludes the real App worktree origin

Evidence: `AGENTS.md` and `templates/AGENTS.md` reserve managed worktrees for
`<repo-parent>/<repo-name>.worktrees/<semantic-lane>` and reject substituting a
runtime-generated worktree. The handoff skill repeats that restriction.

Inference: the policy has no concept for an already-existing App-created
worktree whose lifecycle is task-bound and whose promoted commit is intended for
a different integration checkout.

Confidence: high.

### Claim 3: the core controller already supports the Git relationship

Evidence: `src/codex_flow/worktrees.py` accepts `existing_worktree` at any
distinct physical Git toplevel when it shares the repository's exact Git common
directory, branch matches, and HEAD descends from the frozen base SHA.

Inference: no new workspace mode is required for SDK execution merely because
the path is below `~/.codex/worktrees`. The capsule must distinguish
`repository_root` (integration authority) from `workspace_path` (current mutable
checkout) and select `existing_worktree`.

Confidence: high.

### Claim 4: context rollover is underspecified for same-directory ownership

Evidence: the handoff skill says fresh START uses `create_thread` and explicitly
forbids `fork_thread`; `RECOVER_THREAD` only resumes an interrupted existing
thread or republishes a callback. General policy says context degradation may
trigger rollover while preserving the same workspace, but supplies no native
operation that implements that rollover for a task-bound App worktree.

Inference: controllers invent an extra gate such as “the latest turn must be a
503” because the policy lacks an explicit, evidence-based same-directory
rollover operation. HTTP 503 should be evidence of degradation, not the sole
authorization condition.

Confidence: high.

## Contradictions

The policy correctly says a new model context must reuse the same workspace,
but the native handoff rules make that impossible whenever the workspace is not
a saved project. The safety goal (no duplicate mutable owner) is valid; the
chosen proxy (saved-project path equality) is too strong.

## Concrete e-Justice workspace verification

Read-only Git inspection confirmed that
`/home/adam/.codex/worktrees/9b1a/more-sources` is a distinct worktree of the
same repository as the long-lived integration checkout: its Git common
directory is `/home/adam/SprintAct/.git`, its branch is
`agent/judicial-source-ownership`, and its HEAD is
`154eca55b0b66cebb8e35b9fc3ffad817025eb3f`. The integration checkout is on
`agent/normattiva-r9-recovery-controller`.

This proves that the e-Justice checkout fits the existing `existing_worktree`
contract. A concrete rollover still requires the controller to snapshot the
source task and establish that it has no active writer before transferring
ownership; task liveness was intentionally not changed or inferred here.

## Recommended follow-up

Planning can proceed.

## Source and artifact paths

- `AGENTS.md`
- `templates/AGENTS.md`
- `templates/AGENTS.workflow.md`
- `plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md`
- `plugins/personal-workflow-skills/skills/plan-work/SKILL.md`
- `plugins/personal-workflow-skills/skills/recover-milestone/SKILL.md`
- `plugins/personal-workflow-skills/skills/workflow-control/SKILL.md`
- `src/codex_flow/domain.py`
- `src/codex_flow/worktrees.py`
