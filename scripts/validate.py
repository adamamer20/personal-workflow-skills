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
    "dead-code-elimination-audit",
    "dedup-naming-audit",
    "fallback-upstream-audit",
    "grill-me-light",
    "implement-and-adversarial-review",
    "indirect-attribute-access-audit",
    "overabstraction-audit",
    "reasonix-go",
    "sol-luna-route",
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
    grill = (SKILLS_ROOT / "grill-me-light" / "SKILL.md").read_text(encoding="utf-8")
    route = (SKILLS_ROOT / "sol-luna-route" / "SKILL.md").read_text(encoding="utf-8")
    if "$sol-luna-route" in grill or "$grill-me-light" in route:
        fail("planning and execution skills must not depend on each other")


def validate_global_agents_template() -> None:
    text = GLOBAL_AGENTS_PATH.read_text(encoding="utf-8")
    required_text = (
        "$grill-me-light",
        "$sol-luna-route",
        "Repository `AGENTS.md` files own project-specific plan paths",
    )
    for token in required_text:
        if token not in text:
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
    validate_global_agents_template()
    print(f"Validated {len(EXPECTED_SKILLS)} cross-project skills.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, py_compile.PyCompileError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
