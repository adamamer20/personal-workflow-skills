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
BASELINE_PLUGIN_VERSION = "0.1.0+codex.20260818104438"
WORKFLOW_PATHS = {
    "plan": SKILLS_ROOT / "plan-work" / "SKILL.md",
    "handoff": SKILLS_ROOT / "codex-thread-handoff" / "SKILL.md",
    "execute": SKILLS_ROOT / "execute-milestone" / "SKILL.md",
}
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
    version = manifest.get("version")
    if not isinstance(version, str):
        fail("plugin manifest must contain a string version")
    baseline_match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)\+codex\.(\d+)", BASELINE_PLUGIN_VERSION
    )
    current_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\+codex\.(\d+)", version)
    if baseline_match is None or current_match is None:
        fail("plugin version must use semver plus a codex build timestamp")
    baseline = tuple(int(part) for part in baseline_match.groups())
    current = tuple(int(part) for part in current_match.groups())
    if current <= baseline:
        fail(f"plugin version must be newer than {BASELINE_PLUGIN_VERSION}")


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
    execute = (SKILLS_ROOT / "execute-milestone" / "SKILL.md").read_text(
        encoding="utf-8"
    )
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
        "one milestone, one fresh peer execution thread",
        "If implementation was requested",
        "planning thread's exact id",
        "Do not pass inherited chat history, poll, wait for progress",
        "never silently fall back to a subagent",
    )
    require_contract(plan_path, required_text)


def validate_execute_milestone_contract() -> None:
    execute_path = SKILLS_ROOT / "execute-milestone" / "SKILL.md"
    required_text = (
        "exactly one decision-ready milestone",
        "planning thread owns the program plan",
        "Escalate material decisions only",
        "Do not poll, wait for, or repeatedly list",
        "Hard context rollover",
        "self-review",
        "Subjective-quality promotion",
        "production reachability",
        "prove parity before deletion",
        "stage only exact task-owned paths",
        "Never push, rebase, merge, stash, discard",
        "send exactly one bounded completion packet",
    )
    require_contract(execute_path, required_text)


def validate_thread_handoff_contract() -> None:
    handoff_path = SKILLS_ROOT / "codex-thread-handoff" / "SKILL.md"
    required_text = (
        "one **START** or **MESSAGE** operation",
        "Peer tasks are durable Codex threads, not child/subagents",
        "call `list_projects` first",
        "inspect `isGitRepository`",
        "Call `create_thread` once",
        "initial `prompt`",
        "only when a governing user instruction authorizes the resolved pair",
        "returns `threadId` and `hostId`",
        "return `clientThreadId`",
        "never pass it to tools that require `threadId`",
        "do not use `fork_thread`",
        "exact thread id",
        "Do not wait for the peer",
        "Send exactly one",
        "Never poll",
        "Never retry an uncertain create or send",
        "silently use a fallback transport",
    )
    require_contract(handoff_path, required_text)


def validate_native_routing_contract() -> None:
    workflow_text = {
        name: " ".join(path.read_text(encoding="utf-8").split())
        for name, path in WORKFLOW_PATHS.items()
    }
    combined = " ".join(workflow_text.values())
    for stale in STALE_ROUTING_CONTRACTS:
        if stale in combined:
            fail(f"workflow still contains stale routing contract: {stale}")

    plan_required = (
        "Resolve execution routing",
        "concrete native route",
        "applicable user-owned `AGENTS.md` routing policy",
        "installing this skill is not authorization",
        "`routing_status: not_authorized`",
        "`gpt-5.6-luna`",
        "`gpt-5.6-sol`",
        "schema advertises both fields",
        "substitute another model",
        "resolved exact native pair",
        "passing the same exact pair as native `model` and `thinking` arguments",
        "confirmed dispatch distinct from confirmed model enforcement",
        "Never retry a rejected or unsupported route",
    )
    require_contract(WORKFLOW_PATHS["plan"], plan_required)

    handoff_required = (
        "Native routing authorization",
        "applicable user-owned `AGENTS.md` policy",
        "skill installation by itself is not authorization",
        "advertises both `model` and `thinking` plus the authorized values",
        "pass both exact fields as top-level native arguments",
        "`routing_status: not_authorized`",
        "do not create a peer with a default or alternate route",
        "`routing_status: enforced`",
        "confirms dispatch",
        "native response or tool contract confirms the exact pair",
    )
    require_contract(WORKFLOW_PATHS["handoff"], handoff_required)

    execute_required = (
        "concrete native pair",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "Skill installation alone does not authorize model overrides",
        "omits `model` and `thinking`",
        "dispatch fails closed",
        "no default-model, alternate-model, or retry fallback",
        "task-creation contract, not capsule-only recommendation text",
        "native confirmation of the exact pair",
    )
    require_contract(WORKFLOW_PATHS["execute"], execute_required)

    README_PATH = ROOT / "README.md"
    readme_text = " ".join(README_PATH.read_text(encoding="utf-8").split())
    readme_required = (
        "Authorized native routing defaults:",
        "Native `model`",
        "Native `thinking`",
        "explicit user request or applicable user-owned `AGENTS.md` policy",
        "plugin installation alone is insufficient authorization",
        "passes both in one call",
        "fails closed on unsupported or rejected routes",
        "routing not enforced",
        "model enforcement is confirmed only",
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
        "Sol High planning thread",
        "fresh peer execution thread",
        "Luna XHigh",
        "model=gpt-5.6-luna, thinking=xhigh",
        "model=gpt-5.6-luna, thinking=high",
        "model=gpt-5.6-sol, thinking=high",
        "user-owned routing authorization",
        "plugin installation alone is not authorization",
        "native schema does not advertise an authorized pair",
        "routing was not enforced",
        "never polls execution",
        "One milestone normally uses one fresh execution context",
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
    actual_skills = {path.name for path in SKILLS_ROOT.iterdir() if path.is_dir()}
    if actual_skills != EXPECTED_SKILLS:
        fail(f"unexpected skill inventory: {sorted(actual_skills ^ EXPECTED_SKILLS)}")
    for skill_name in sorted(EXPECTED_SKILLS):
        validate_skill(SKILLS_ROOT / skill_name)
    validate_independence()
    validate_plan_work_contract()
    validate_execute_milestone_contract()
    validate_thread_handoff_contract()
    validate_native_routing_contract()
    validate_global_agents_template()
    print(f"Validated {len(EXPECTED_SKILLS)} cross-project skills.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, py_compile.PyCompileError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
