#!/usr/bin/env python3
"""Validate the personal workflow skills plugin without project dependencies."""

from __future__ import annotations

import json
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "personal-workflow-skills"
MARKETPLACE_NAME = "adam-workflows"
PLUGIN_ROOT = ROOT / "plugins" / PLUGIN_NAME
SKILLS_ROOT = PLUGIN_ROOT / "skills"
HOOKS_ROOT = PLUGIN_ROOT / "hooks"
HOOKS_FILE = HOOKS_ROOT / "hooks.json"
EXPECTED_SKILLS = {
    "abstraction-opportunity-audit",
    "codex-thread-handoff",
    "dead-code-elimination-audit",
    "dedup-naming-audit",
    "execute-milestone",
    "fallback-upstream-audit",
    "indirect-attribute-access-audit",
    "overabstraction-audit",
    "plan-work",
    "strong-typing-audit",
    "workflow-control",
}
FORBIDDEN_PROJECT_TEXT = (
    "sprintact",
    "dnd-platform",
    "open adventures",
    "open-adventures",
)
FORBIDDEN_AUTHORING_PATHS = (
    "/home/adam/",
    "~/.codex/skills/",
    ".codex/skills/",
)
GLOBAL_AGENTS_PATH = ROOT / "templates" / "AGENTS.md"
BASELINE_PLUGIN_VERSION = "0.1.4+codex.20260820180447"
WORKFLOW_PATHS = {
    "plan": SKILLS_ROOT / "plan-work" / "SKILL.md",
    "handoff": SKILLS_ROOT / "codex-thread-handoff" / "SKILL.md",
    "execute": SKILLS_ROOT / "execute-milestone" / "SKILL.md",
    "control": SKILLS_ROOT / "workflow-control" / "SKILL.md",
}
PROMPT_FIXTURES_ROOT = ROOT / "tests" / "fixtures" / "prompt-input"
STALE_ROUTING_CONTRACTS = (
    "Pass a model or reasoning override only when the user has explicitly requested it",
    "otherwise keep the recommendation in the capsule and let the new task use configured defaults",
    "Model selection is routing metadata, not workflow identity",
    "The skill records or recommends routing",
)


def fail(message: str) -> None:
    raise ValueError(message)


def read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        fail(f"{path}: missing YAML frontmatter")

    name_match = re.search(r"^name:\s*[\"']?([^\n\"']+)", match.group(1), re.MULTILINE)
    description_match = re.search(r"^description:\s*(.+)", match.group(1), re.MULTILINE)
    if name_match is None or description_match is None:
        fail(f"{path}: frontmatter must contain name and description")
    return {
        "name": name_match.group(1).strip(),
        "description": description_match.group(1).strip(),
    }


def validate_manifest() -> None:
    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME:
        fail("plugin name must match its marketplace directory")
    if manifest.get("skills") != "./skills/":
        fail("plugin manifest must expose ./skills/")
    if "hooks" in manifest:
        fail("plugin manifest must use default hooks/hooks.json discovery")
    version = manifest.get("version")
    if not isinstance(version, str):
        fail("plugin manifest must contain a string version")
    baseline_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\+codex\.(\d+)", BASELINE_PLUGIN_VERSION)
    current_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\+codex\.(\d+)", version)
    if baseline_match is None or current_match is None:
        fail("plugin version must use semver plus a codex build timestamp")
    baseline = tuple(int(part) for part in baseline_match.groups())
    current = tuple(int(part) for part in current_match.groups())
    if current <= baseline:
        fail(f"plugin version must be newer than {BASELINE_PLUGIN_VERSION}")


def validate_hooks() -> None:
    if not HOOKS_FILE.is_file():
        fail("plugin must include default hooks/hooks.json")
    hooks_manifest = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    if not isinstance(hooks_manifest, dict) or not isinstance(hooks_manifest.get("hooks"), dict):
        fail("hooks.json must contain a hooks object")
    configured = hooks_manifest["hooks"]
    expected_events = {"PreToolUse", "PostToolUse", "Stop", "SessionStart"}
    if set(configured) != expected_events:
        fail(f"hooks.json must configure exactly {sorted(expected_events)}")
    for event_name, groups in configured.items():
        if not isinstance(groups, list) or not groups:
            fail(f"hooks.json {event_name} must contain matcher groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                fail(f"hooks.json {event_name} matcher group is malformed")
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    fail("plugin lifecycle hooks must use command handlers")
                command = handler.get("command")
                if not isinstance(command, str) or "$PLUGIN_ROOT/hooks/lifecycle.py" not in command:
                    fail("plugin lifecycle hook must resolve lifecycle.py through PLUGIN_ROOT")
                if handler.get("async") is True:
                    fail("plugin lifecycle hooks must be synchronous")
    script = HOOKS_ROOT / "lifecycle.py"
    if not script.is_file():
        fail("hooks/lifecycle.py is missing")
    hook_text = script.read_text(encoding="utf-8")
    required = (
        "PLUGIN_DATA",
        "os.replace",
        "permissionDecision",
        "updatedInput",
        "threadId",
        "clientThreadId",
        "list_threads",
        "stop_hook_active",
        "SessionStart",
        "never invokes a Codex tool",
        "fail open",
        "MAX_TITLE_INPUT",
        "startingState",
        "recovery ledger is saturated",
        "title_identity",
        "normalised_digest",
        "reconciled_ambiguous",
        "MAX_CANDIDATES",
        "MAX_RETAINED_MATCHES",
        "invalid_snapshot",
        "blocks_create",
        "LIST_RESPONSE_KEYSETS",
        "character.isspace()",
        "set(response)",
    )
    normalized = " ".join(hook_text.split())
    for token in required:
        if token not in normalized:
            fail(f"hooks/lifecycle.py missing lifecycle contract: {token}")
    with tempfile.TemporaryDirectory(prefix="personal-workflow-hooks-validation-") as temp_dir:
        py_compile.compile(str(script), cfile=str(Path(temp_dir) / "lifecycle.pyc"), doraise=True)


def validate_marketplace() -> None:
    marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    if marketplace.get("name") != MARKETPLACE_NAME:
        fail(f"marketplace name must be {MARKETPLACE_NAME}")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        fail("marketplace must contain exactly one plugin")
    entry = plugins[0]
    expected_path = f"./plugins/{PLUGIN_NAME}"
    if entry.get("name") != PLUGIN_NAME:
        fail("marketplace entry must match the plugin name")
    if entry.get("version") != "0.1.5+codex.20260825000000":
        fail("marketplace entry version must match the packaged plugin")
    if entry.get("source") != {"source": "local", "path": expected_path}:
        fail(f"marketplace source must be {expected_path}")
    if entry.get("policy") != {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }:
        fail("marketplace policy must use explicit install defaults")
    if entry.get("category") != "Productivity":
        fail("marketplace category must be Productivity")


def validate_skill(skill_dir: Path) -> None:
    skill_name = skill_dir.name
    metadata = read_frontmatter(skill_dir / "SKILL.md")
    if metadata["name"] != skill_name:
        fail(f"{skill_name}: frontmatter name does not match directory")

    ui_path = skill_dir / "agents" / "openai.yaml"
    ui_text = ui_path.read_text(encoding="utf-8")
    if f"${skill_name}" not in ui_text:
        fail(f"{ui_path}: default_prompt must explicitly mention ${skill_name}")

    for path in skill_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".pyc", ".pyo"} or "__pycache__" in path.parts:
            fail(f"generated Python artifact is not allowed: {path}")
        if path.suffix not in {".md", ".yaml", ".py", ""}:
            continue
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in FORBIDDEN_PROJECT_TEXT:
            if token in lowered:
                fail(f"{path}: project-specific text is not allowed: {token}")
        for token in FORBIDDEN_AUTHORING_PATHS:
            if token in text:
                fail(f"{path}: authoring-location path is not allowed: {token}")

    scripts = (skill_dir / "scripts").glob("*") if (skill_dir / "scripts").is_dir() else ()
    with tempfile.TemporaryDirectory(prefix="personal-workflow-validation-") as temp_dir:
        for script in scripts:
            if script.suffix == ".py" or script.read_bytes().startswith(b"#!/usr/bin/env python3"):
                compiled = Path(temp_dir) / f"{skill_name}-{script.name}.pyc"
                py_compile.compile(str(script), cfile=str(compiled), doraise=True)
            elif script.read_bytes().startswith(b"#!/usr/bin/env bash"):
                subprocess.run(["bash", "-n", str(script)], check=True)


def validate_independence() -> None:
    plan = (SKILLS_ROOT / "plan-work" / "SKILL.md").read_text(encoding="utf-8")
    execute = (SKILLS_ROOT / "execute-milestone" / "SKILL.md").read_text(encoding="utf-8")
    if "$execute-milestone" in plan or "$plan-work" in execute:
        fail("planning and execution skills must not depend on each other")


def require_contract(path: Path, required_text: tuple[str, ...]) -> None:
    normalized_text = " ".join(path.read_text(encoding="utf-8").split())
    for token in required_text:
        if token not in normalized_text:
            fail(f"{path}: missing workflow contract: {token}")


def validate_plan_work_contract() -> None:
    plan_path = SKILLS_ROOT / "plan-work" / "SKILL.md"
    required_text = (
        "decision-ready",
        "exactly one active plan",
        "independently closable milestones",
        "acceptance modes",
        "objective",
        "visual",
        "architecture",
        "protected surfaces",
        "ModelFacingCapsule",
        "code authority",
        "visual authority",
        "boundary/security authority",
        "completion-biased",
    )
    require_contract(plan_path, required_text)


def validate_execute_milestone_contract() -> None:
    execute_path = SKILLS_ROOT / "execute-milestone" / "SKILL.md"
    required_text = (
        "exactly one decision-ready capsule",
        "self-review",
        "objective",
        "visual",
        "architecture",
        "protected surfaces",
        "workflow-control",
        "codex-flow",
        "completion-biased",
        "ModelFacingResult",
        "observable",
    )
    require_contract(execute_path, required_text)


def validate_workflow_control_contract() -> None:
    control_path = WORKFLOW_PATHS["control"]
    required_text = (
        "codex-flow control",
        "codex-flow status",
        "durable status",
        "ModelFacingCapsule",
        "JSON/JSONL",
        "--resume",
        "legacy",
        "must not be silently mixed",
    )
    require_contract(control_path, required_text)


def validate_prompt_fixtures() -> None:
    budget_path = PROMPT_FIXTURES_ROOT / "budget.json"
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    if budget.get("schema_version") != 1:
        fail("prompt budget schema must be version 1")
    limits = budget.get("budget_bytes")
    before = budget.get("before_bytes")
    if not isinstance(limits, dict) or not isinstance(before, dict):
        fail("prompt budget must contain budget_bytes and before_bytes")
    for skill_name in ("plan-work", "execute-milestone", "workflow-control"):
        fixture_path = PROMPT_FIXTURES_ROOT / f"{skill_name}.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if fixture.get("schema_version") != 1 or fixture.get("skill") != skill_name:
            fail(f"{fixture_path}: invalid prompt fixture identity")
        input_text = fixture.get("input_text")
        if not isinstance(input_text, str) or input_text.count(f"${skill_name}") != 1:
            fail(f"{fixture_path}: intended skill must occur exactly once")
        ui_prompt = (SKILLS_ROOT / skill_name / "agents" / "openai.yaml").read_text(encoding="utf-8")
        if ui_prompt.count(f"${skill_name}") != 1:
            fail(f"{skill_name}: default prompt must invoke the intended skill exactly once")
        skill_text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        normalized = " ".join(skill_text.split())
        for token in fixture.get("required", ()):
            if not isinstance(token, str) or token not in normalized:
                fail(f"{fixture_path}: missing required model-visible contract: {token}")
        for token in fixture.get("forbidden", ()):
            if not isinstance(token, str) or token in normalized:
                fail(f"{fixture_path}: obsolete model-visible contract remains: {token}")
        current_bytes = len(skill_text.encode("utf-8"))
        limit = limits.get(skill_name)
        if not isinstance(limit, int) or current_bytes > limit:
            fail(f"{fixture_path}: prompt exceeds measured budget")
        if skill_name in before:
            old_bytes = before[skill_name]
            if not isinstance(old_bytes, int) or current_bytes >= old_bytes:
                fail(f"{fixture_path}: before/after prompt reduction is not proven")


def validate_model_contracts() -> None:
    contracts_path = ROOT / "src" / "codex_flow" / "contracts.py"
    require_contract(
        contracts_path,
        (
            "class ModelFacingCapsule",
            "class ModelFacingResult",
            "class ModelAuthority",
            "class ModelValidation",
            "model_facing_capsule_schema",
            "model_facing_result_schema",
            "completion_biased",
        ),
    )


def validate_h5_evidence() -> None:
    evidence_path = ROOT / "docs" / "reviews" / "evidence" / "h5-workflow-control-medium.json"
    if not evidence_path.is_file():
        fail("H5 real workflow-control evidence is missing")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    required = {
        "schema",
        "status",
        "route",
        "command",
        "model",
        "reasoning_effort",
        "controller_status",
        "controller_checkpoint",
        "result_status",
        "observable_edit",
        "protected_unchanged",
        "authorized_changed_paths",
    }
    if set(evidence) != required | {"base_sha", "controller_exit_code", "final_file_sha256", "protected_file_sha256"}:
        fail("H5 evidence shape is not the sanitized controller contract")
    if evidence.get("schema") != "codex-flow/h5-workflow-control-medium/v1":
        fail("H5 evidence schema is unsupported")
    if evidence.get("status") != "passed" or evidence.get("route") != "workflow-control/codex-flow":
        fail("H5 evidence does not prove controller reachability")
    if evidence.get("controller_status") != "completed" or evidence.get("controller_checkpoint") != "result_durable":
        fail("H5 evidence does not prove a durable terminal result")
    if evidence.get("result_status") != "completed" or evidence.get("observable_edit") is not True:
        fail("H5 evidence does not prove the observable medium outcome")
    if evidence.get("protected_unchanged") is not True:
        fail("H5 evidence does not prove protected-surface integrity")
    command = evidence.get("command")
    if command != ["codex-flow", "control"]:
        fail("H5 evidence must name the controller entrypoint")
    changed_paths = evidence.get("authorized_changed_paths")
    if changed_paths != ["M medium.txt", "?? .codex-flow/"]:
        fail("H5 evidence changed-path scope is not the bounded medium outcome")


def validate_thread_handoff_contract() -> None:
    handoff_path = SKILLS_ROOT / "codex-thread-handoff" / "SKILL.md"
    required_text = (
        "one **START**, **RECOVER_START**, **MESSAGE**, or **RECOVER_THREAD** operation",
        "Peer tasks are durable Codex threads, not child/subagents",
        "call `list_projects` immediately before creation",
        "inspect `isGitRepository`",
        "Use only a project id returned by that current call",
        "Construct the call from the currently exposed `create_thread` schema",
        "`projectId` belongs only inside `target`",
        "one logical START",
        "`clientThreadId` is successful queued creation",
        "do not infer non-creation from the error text",
        "exactly one non-waiting `list_threads` reconciliation snapshot",
        "Never retry `create_thread`",
        "initial `prompt`",
        "only when a governing user instruction authorizes the resolved pair",
        "returns `threadId` and `hostId`",
        "return `clientThreadId`",
        "never pass it to tools that require `threadId`",
        "do not use `fork_thread`",
        "exact thread id",
        "Use `hostId` only when it came from",
        "callback_status: unsent",
        "complete unsent packet",
        "Do not wait for the peer",
        "Send exactly one",
        "`NEEDS_DECISION`, `EXTERNAL_BLOCKED`, and `FAILED`",
        "`REPLAN_NOTICE` and `CONTINUE_WITH_REPLAN` are nonterminal",
        "Never poll",
        "Never retry a create or an uncertain send",
        "silently use a fallback transport",
        "RECOVER_START never calls `create_thread`",
        "`creation_status: uncertain`",
        "`creation_status: not_found_after_reconciliation`",
        "Take exactly one non-waiting status snapshot",
        "If the latest turn is `interrupted`",
        "`REPUBLISH_CALLBACK` message",
        "`recovery_status: unsent`",
        "A replacement requires a separate explicit user decision",
        "START does not choose workspace topology",
        "A fresh thread or model context does not imply a fresh Git worktree",
        "`current_checkout`",
        "`existing_worktree`",
        "`managed_worktree`",
        "<repo-parent>/<repo-name>.worktrees/",
        "report `workspace_status: unsupported` and leave START undispatched",
        "Handoff never runs Git worktree creation",
    )
    require_contract(handoff_path, required_text)


def validate_native_routing_contract() -> None:
    workflow_text = {name: " ".join(path.read_text(encoding="utf-8").split()) for name, path in WORKFLOW_PATHS.items()}
    combined = " ".join(workflow_text[name] for name in ("plan", "execute"))
    for stale in STALE_ROUTING_CONTRACTS:
        if stale in combined:
            fail(f"workflow still contains stale routing contract: {stale}")
    # The native route remains intentionally documented only by the explicit
    # legacy handoff skill.  The default planning/execution prompts contain no
    # peer transport, callback, routing, or state-machine policy.
    handoff_required = ("Explicit legacy route", "$workflow-control", "never invoke both routes")
    require_contract(WORKFLOW_PATHS["handoff"], handoff_required)

    README_PATH = ROOT / "README.md"
    readme_text = " ".join(README_PATH.read_text(encoding="utf-8").split())
    readme_required = (
        "Authorized native routing defaults:",
        "Visual-judgment implementation",
        "acceptance modes: `objective`, `visual`, and `architecture`",
        "Objective code review uses Luna XHigh",
        "Sol Medium implements and Sol High performs",
        "A mixed objective/visual milestone must pass both gates",
        "Non-convergence triggers diagnosis and a change of authority or approach",
        "`CONTINUE_WITH_REPLAN` is internal and nonterminal",
        "`NEEDS_DECISION` only when user intent is genuinely underdetermined",
        "`EXTERNAL_BLOCKED` only for a missing external prerequisite",
        "Difficulty or a disproven plan alone never summons the user",
        "Native `model`",
        "Native `thinking`",
        "explicit user request or applicable user-owned `AGENTS.md` policy",
        "plugin installation alone is insufficient authorization",
        "passes both in one logical START",
        "fails closed on unsupported or rejected routes",
        "Error text alone never proves non-creation",
        "one non-waiting `list_threads` reconciliation snapshot",
        "never calls `create_thread` again",
        "queued `clientThreadId` confirms dispatch",
        "routing not enforced",
        "model enforcement is confirmed only",
        "Execution capsules carry the exact planning callback `threadId`",
        "planning task remains unarchived",
        "accurately labelled `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, or `FAILED`",
        "callback_status: unsent",
        "Runtime recovery is deliberately retry-free",
        "last turn is `interrupted`",
        "same thread republishes the existing terminal packet",
        "cannot guarantee an automatic callback",
        "A fresh execution thread is a model-context boundary, not a Git-workspace boundary",
        "<parent>/<repo>.worktrees/<program-slug>",
        "The handoff never defaults every Git task to a new worktree",
        "`$workflow-control` is the packaged agent entrypoint",
        "ModelFacingCapsule",
        "ModelFacingResult",
        "codex-flow control",
        "explicit legacy command `$codex-thread-handoff`",
    )
    require_contract(README_PATH, readme_required)


def validate_global_agents_template() -> None:
    text = GLOBAL_AGENTS_PATH.read_text(encoding="utf-8")
    normalized_text = " ".join(text.split())
    required_text = (
        "$plan-work",
        "$execute-milestone",
        "$codex-thread-handoff",
        "`docs/reviews/`",
        "Sol Medium architecture thread",
        "fresh peer execution thread",
        "Luna XHigh",
        "model=gpt-5.6-luna, thinking=xhigh",
        "model=gpt-5.6-luna, thinking=high",
        "model=gpt-5.6-sol, thinking=medium",
        "model=gpt-5.6-sol, thinking=high",
        "Every milestone declares one or more acceptance modes",
        "Route by the judgment required for acceptance, not by file type",
        "Objective code review uses Luna XHigh",
        "Architecture-conformance review uses Sol Medium",
        "single mutable implementation owner",
        "independent visual-quality promotion review uses Sol High",
        "passing code tests never implies that a rendered result is good",
        "When an owner stops converging",
        "A change of authority or approach is not a terminal condition",
        "Escalate to the user only as `NEEDS_DECISION`",
        "Use `EXTERNAL_BLOCKED` only for missing credentials",
        "`CONTINUE_WITH_REPLAN` is internal and nonterminal",
        "user-owned routing authorization",
        "plugin installation alone is not authorization",
        "native schema does not advertise an authorized pair",
        "routing was not enforced",
        "Error text alone does not prove that no task was created",
        "one non-waiting `list_threads` reconciliation snapshot",
        "never retries `create_thread`",
        "Every terminal outcome returns exactly one",
        "accurately labelled `NEEDS_DECISION`, `EXTERNAL_BLOCKED`, or `FAILED`",
        "Keep the planning thread unarchived",
        "never polls execution",
        "failed-looking START is never automatic retry authorization",
        "resume the same thread after an `interrupted` turn",
        "user explicitly authorizes replacement",
        "One milestone normally uses one fresh execution context",
        "A fresh Codex thread or model does not imply a fresh Git worktree",
        "<parent>/<repo>.worktrees/",
        "The plan chooses `current_checkout`, `existing_worktree`, or `managed_worktree`",
        "Before substantial execution",
        "open P0/P1 findings are zero",
        "Repository `AGENTS.md` files own project-specific plan paths",
    )
    for token in required_text:
        if token not in normalized_text:
            fail(f"{GLOBAL_AGENTS_PATH}: missing routing contract: {token}")

    lowered = text.lower()
    for token in FORBIDDEN_PROJECT_TEXT:
        if token in lowered:
            fail(f"{GLOBAL_AGENTS_PATH}: project-specific text is not allowed: {token}")
    for token in FORBIDDEN_AUTHORING_PATHS:
        if token in text:
            fail(f"{GLOBAL_AGENTS_PATH}: local authoring path is not allowed: {token}")


def main() -> int:
    validate_marketplace()
    validate_manifest()
    validate_hooks()
    actual_skills = {path.name for path in SKILLS_ROOT.iterdir() if path.is_dir()}
    if actual_skills != EXPECTED_SKILLS:
        fail(f"unexpected skill inventory: {sorted(actual_skills ^ EXPECTED_SKILLS)}")
    for skill_name in sorted(EXPECTED_SKILLS):
        validate_skill(SKILLS_ROOT / skill_name)
    validate_independence()
    validate_plan_work_contract()
    validate_execute_milestone_contract()
    validate_workflow_control_contract()
    validate_thread_handoff_contract()
    validate_native_routing_contract()
    validate_prompt_fixtures()
    validate_model_contracts()
    validate_h5_evidence()
    validate_global_agents_template()
    print(f"Validated {len(EXPECTED_SKILLS)} cross-project skills.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, py_compile.PyCompileError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
