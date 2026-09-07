#!/usr/bin/env python3
"""Validate the personal workflow skills plugin without project dependencies."""

from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import py_compile
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import NamedTuple

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # Keep the direct validator error actionable before setup.
    Draft202012Validator = None  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]

try:
    from codex_flow.descriptive_naming import (
        PERSISTED_PROTOCOL_NAMES,
        REMOVED_PUBLIC_IDENTIFIERS,
        REMOVED_PUBLIC_MODULES,
        SEMANTIC_PUBLIC_IDENTIFIERS,
        SEMANTIC_PUBLIC_MODULES,
        changed_paths_from_git,
        validate_changed_names,
        validate_identifier_name,
        validate_path_name,
    )
except ModuleNotFoundError:  # Direct ``python3 scripts/validate.py`` from a source checkout.
    sys.path.insert(0, str(ROOT / "src"))
    from codex_flow.descriptive_naming import (
        PERSISTED_PROTOCOL_NAMES,
        REMOVED_PUBLIC_IDENTIFIERS,
        REMOVED_PUBLIC_MODULES,
        SEMANTIC_PUBLIC_IDENTIFIERS,
        SEMANTIC_PUBLIC_MODULES,
        changed_paths_from_git,
        validate_changed_names,
        validate_identifier_name,
        validate_path_name,
    )

from codex_flow.contracts import (  # noqa: E402 - source checkout fallback adjusts sys.path above.
    ModelAuthority,
    ModelFacingCapsule,
    ModelFacingResult,
    ModelResultStatus,
    ModelValidation,
    model_facing_capsule_schema,
    model_facing_result_schema,
)
from codex_flow.domain import AcceptanceMode, RoleId, validate_output_schema, validate_structured_output  # noqa: E402

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
    "collect-evidence",
    "run-discovery-spike",
    "define-visual-contract",
    "review-work",
    "recover-milestone",
}
RECONCILED_SKILLS = {
    "collect-evidence",
    "run-discovery-spike",
    "define-visual-contract",
    "review-work",
    "recover-milestone",
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
BASELINE_PLUGIN_VERSION = "0.1.5+codex.20260825000000"
CANONICAL_DEFAULT_PROMPT = (
    "Choose the smallest workflow: plan new substantial work, or execute one decision-ready milestone through "
    "normal $workflow-control. Use $codex-thread-handoff only when explicitly requested for legacy compatibility "
    "or deliberate comparison."
)
SCHEMA_NAMES = (
    "capsule",
    "evidence",
    "finding",
    "recovery",
    "result",
    "review",
    "visual-contract",
)
SCHEMAS_ROOT = ROOT / "schemas"
PARTITIONS_PATH = ROOT / "config" / "test-partitions.toml"
RECONCILIATION_EVIDENCE_PATH = ROOT / "docs" / "reviews" / "evidence" / "workflow-skill-contracts.json"
RETAINED_RECONCILIATION_WHEEL = Path("dist/structured-output-runtime/codex_flow-0.2.0-py3-none-any.whl")
PROMOTED_RECONCILIATION_INVENTORY_COUNT = 49
PROMOTED_RECONCILIATION_INVENTORY_SHA256 = "18ab3d341f5920691d42c2f6d6a2ae6571cd09aed9017599a918774d439594c3"
PROMOTED_RECONCILIATION_CANDIDATE_SHA256 = "fd9331c855d8352febc84ad56df142aec1bc7864f07fcd07d0b582321860996d"
PROMOTED_RECONCILIATION_SCOPE_BYTES = 576
PROMOTED_RECONCILIATION_SCOPE_SHA256 = "03c422680ae42c37b1e5367a1d3d53a41f2d5dd535d2b39d5c4a9ee811b1cb9c"
PARTITION_NAMES = ("contracts", "controller", "workers", "integrations", "workflow-assets")
PERMITTED_SERIAL_EDGE_REASONS = (
    "shared schema",
    "state authority",
    "entrypoint",
    "migration order",
    "acceptance dependency",
)
WORKFLOW_ASSET_PATHS = (
    Path("templates/AGENTS.workflow.md"),
    Path("config/workflow.toml.example"),
    Path("config/test-partitions.toml"),
)
WORKFLOW_PATHS = {
    "plan": SKILLS_ROOT / "plan-work" / "SKILL.md",
    "handoff": SKILLS_ROOT / "codex-thread-handoff" / "SKILL.md",
    "execute": SKILLS_ROOT / "execute-milestone" / "SKILL.md",
    "review": SKILLS_ROOT / "review-work" / "SKILL.md",
    "recover": SKILLS_ROOT / "recover-milestone" / "SKILL.md",
    "control": SKILLS_ROOT / "workflow-control" / "SKILL.md",
}
OUTCOME_EVIDENCE_POLICY_PATHS = (
    SKILLS_ROOT / "plan-work" / "SKILL.md",
    SKILLS_ROOT / "execute-milestone" / "SKILL.md",
    SKILLS_ROOT / "review-work" / "SKILL.md",
    GLOBAL_AGENTS_PATH,
)
_REQUIRED_HARD_CAP_PROOF_OBLIGATIONS = (
    "persisted DOCX reopening/recomputation",
    "rendered-page artifact-role binding",
    "rigorously typed/validated HarnessCase corpus/prompt/region contract",
)
PROMPT_FIXTURES_ROOT = ROOT / "tests" / "fixtures" / "prompt-input"
STALE_ROUTING_CONTRACTS = (
    "Pass a model or reasoning override only when the user has explicitly requested it",
    "otherwise keep the recommendation in the capsule and let the new task use configured defaults",
    "Model selection is routing metadata, not workflow identity",
    "The skill records or recommends routing",
)


def fail(message: str) -> None:
    raise ValueError(message)


class _ProofObligation(NamedTuple):
    name: str
    verified: bool


class _HardCapCase(NamedTuple):
    current_line_count: int
    hard_line_cap: int
    current_proof_obligations: tuple[_ProofObligation, ...]
    replacement_line_count: int | None = None
    replacement_proof_obligations: tuple[_ProofObligation, ...] | None = None


class _OutcomeEvidenceDecision(StrEnum):
    PROOF_PRESERVING_CONTINUATION = "proof_preserving_continuation"
    BOUNDED_REPLAN = "bounded_replan"
    PROMOTION_BLOCKING_LOSS = "promotion_blocking_loss"


def _decide_hard_cap_case(case: _HardCapCase) -> _OutcomeEvidenceDecision:
    if (
        isinstance(case.current_line_count, bool)
        or not isinstance(case.current_line_count, int)
        or case.current_line_count < 0
        or isinstance(case.hard_line_cap, bool)
        or not isinstance(case.hard_line_cap, int)
        or case.hard_line_cap < 0
        or (
            case.replacement_line_count is not None
            and (
                isinstance(case.replacement_line_count, bool)
                or not isinstance(case.replacement_line_count, int)
                or case.replacement_line_count < 0
            )
        )
    ):
        return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
    if tuple(obligation.name for obligation in case.current_proof_obligations) != _REQUIRED_HARD_CAP_PROOF_OBLIGATIONS:
        return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
    if not all(obligation.verified is True for obligation in case.current_proof_obligations):
        return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
    if case.replacement_line_count is None:
        if case.replacement_proof_obligations is not None:
            return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
        effective_line_count = case.current_line_count
    else:
        if case.replacement_proof_obligations is None:
            return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
        if (
            tuple(obligation.name for obligation in case.replacement_proof_obligations)
            != _REQUIRED_HARD_CAP_PROOF_OBLIGATIONS
        ):
            return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
        if not all(obligation.verified is True for obligation in case.replacement_proof_obligations):
            return _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
        effective_line_count = case.replacement_line_count
    if effective_line_count > case.hard_line_cap:
        return _OutcomeEvidenceDecision.BOUNDED_REPLAN
    return _OutcomeEvidenceDecision.PROOF_PRESERVING_CONTINUATION


def _normalized_outcome_evidence_policy(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "Outcome and evidence are ordered and non-negotiable:"
    end_marker = "Never report success merely because a proxy is exact."
    try:
        start = text.index(marker)
        end = text.index(end_marker, start) + len(end_marker)
    except ValueError:
        fail(f"{path}: outcome/evidence priority contract is missing")
    return re.sub(r"\s+", " ", text[start:end]).strip()


def validate_outcome_evidence_priority_contract() -> None:
    policies = tuple(_normalized_outcome_evidence_policy(path) for path in OUTCOME_EVIDENCE_POLICY_PATHS)
    if len(set(policies)) != 1:
        fail("planning, execution, review and repository outcome/evidence policies must be identical")
    for phrase in (
        "Accepted observable outcome and user intent",
        "Required safety, integrity, lineage, isolation, recovery",
        "Explicitly designated hard external constraints",
        "Secondary proxy and optimization metrics",
        "proof-preserving replacement",
        "bounded replan",
    ):
        if phrase not in policies[0]:
            fail(f"outcome/evidence priority contract is missing: {phrase}")

    verified_obligations = tuple(_ProofObligation(name, True) for name in _REQUIRED_HARD_CAP_PROOF_OBLIGATIONS)
    cases = (
        (
            _HardCapCase(9_120, 8_900, verified_obligations),
            _OutcomeEvidenceDecision.BOUNDED_REPLAN,
        ),
        (
            _HardCapCase(8_900, 8_900, verified_obligations[:-1]),
            _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS,
        ),
        (
            _HardCapCase(9_120, 8_900, verified_obligations, replacement_line_count=8_900),
            _OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS,
        ),
        (
            _HardCapCase(
                9_120,
                8_900,
                verified_obligations,
                replacement_line_count=8_900,
                replacement_proof_obligations=verified_obligations,
            ),
            _OutcomeEvidenceDecision.PROOF_PRESERVING_CONTINUATION,
        ),
    )
    for case, expected in cases:
        if _decide_hard_cap_case(case) is not expected:
            fail(f"outcome/evidence decision boundary returned the wrong result for {case}")


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
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        fail("plugin manifest must contain interface metadata")
    default_prompt = interface.get("defaultPrompt")
    if default_prompt != CANONICAL_DEFAULT_PROMPT:
        fail("plugin interface.defaultPrompt must equal the canonical normal $workflow-control prompt")


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
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if entry.get("version") != manifest.get("version"):
        fail("marketplace and plugin versions must match")
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

    for required_metadata in ("display_name:", "short_description:", "default_prompt:"):
        if required_metadata not in ui_text:
            fail(f"{ui_path}: missing metadata field {required_metadata}")

    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if skill_text.count("```") % 2:
        fail(f"{skill_dir / 'SKILL.md'}: Markdown fences are unbalanced")
    if skill_name in RECONCILED_SKILLS:
        normalized = " ".join(skill_text.lower().split())
        if not any(token in normalized for token in ("controller owns", "controller-only", "controller only")):
            fail(f"{skill_dir / 'SKILL.md'}: cognitive role must name controller ownership")
        for token, alternatives in {
            "callback": ("callback", "callbacks"),
            "routing": ("routing", "route"),
            "retry": ("retry", "retries"),
            "successor": ("successor", "successors"),
            "worktree": ("worktree", "worktrees"),
            "ledger": ("ledger",),
        }.items():
            if not any(option in normalized for option in alternatives):
                fail(f"{skill_dir / 'SKILL.md'}: non-ownership boundary is missing {token}")

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
        "architecture map",
        "dependency DAG",
        "current readiness",
        "independently closable vertical milestones",
        "disjoint mutable surfaces",
        "shared schemas",
        "state authority",
        "production entrypoints",
        "serial edge",
        "critical path",
        "fake boundaries",
        "single-owner",
        "nested swarm",
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
            if not isinstance(token, str):
                fail(f"{fixture_path}: forbidden contract must be a string")
            # The reconciled skills explicitly state that they do not own these
            # surfaces.  That non-ownership wording is safe and must not be
            # mistaken for the obsolete model-owned transport contract.
            non_ownership = re.search(rf"(?:never|do not) [^.\n]{{0,140}}{re.escape(token)}", normalized)
            if token in normalized and non_ownership is None:
                fail(f"{fixture_path}: obsolete model-visible contract remains: {token}")
        current_bytes = len(skill_text.encode("utf-8"))
        limit = limits.get(skill_name)
        if not isinstance(limit, int) or current_bytes > limit:
            fail(f"{fixture_path}: prompt exceeds measured budget")
        if skill_name in before:
            old_bytes = before[skill_name]
            if not isinstance(old_bytes, int) or current_bytes >= old_bytes:
                fail(f"{fixture_path}: before/after prompt reduction is not proven")
        if skill_name == "plan-work":
            validate_architecture_map_fixture(fixture.get("architecture_map"))


def validate_architecture_map_fixture(value: object) -> None:
    if not isinstance(value, dict):
        fail("plan-work fixture must contain an architecture_map object")
    paths = value.get("paths")
    if not isinstance(paths, list) or not paths:
        fail("architecture_map.paths must be non-empty")
    actions = {"create", "modify", "preserve", "remove"}
    for entry in paths:
        if not isinstance(entry, dict) or set(entry) != {"path", "action", "responsibility", "owner"}:
            fail("architecture_map paths must name path, action, responsibility and owner")
        if not all(isinstance(entry[key], str) and entry[key] for key in ("path", "responsibility", "owner")):
            fail("architecture_map path entries must use non-empty semantic strings")
        if entry["action"] not in actions:
            fail(f"architecture_map action must be one of {sorted(actions)}")
        if "executor" in entry["owner"].lower() or "executor" in entry["responsibility"].lower():
            fail("architecture_map may not delegate topology to the executor")
    if not isinstance(value.get("dependency_direction"), str) or not value["dependency_direction"]:
        fail("architecture_map must define dependency_direction")
    identifiers = value.get("primary_identifiers")
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or not all(isinstance(item, str) and item for item in identifiers)
    ):
        fail("architecture_map must define primary_identifiers")
    budget = value.get("new_artifact_budget")
    if not isinstance(budget, int) or budget != 22:
        fail("architecture_map new_artifact_budget must equal 22")
    validate_milestone_graph_fixture(value.get("milestone_graph"))


def validate_milestone_graph_fixture(value: object) -> None:
    """Validate the planning-only DAG/readiness projection.

    This is deliberately a closed fixture contract rather than a runtime graph
    implementation. It proves that planning freezes shared authority before
    fan-out and records enough ownership/dependency facts to reject unsafe
    parallel or nested-controller plans.
    """

    if not isinstance(value, dict):
        fail("architecture_map milestone_graph must be an object")
    required = {
        "milestones",
        "current_readiness",
        "serial_edges",
        "shared_authorities_frozen",
        "fan_out_after",
        "ready_parallel_groups",
        "critical_path",
        "critical_path_minimized",
        "nested_controller_default",
    }
    if set(value) != required:
        fail("milestone_graph must declare DAG, readiness, authority freeze, parallel groups and critical path")

    milestones = value["milestones"]
    if not isinstance(milestones, list) or not milestones:
        fail("milestone_graph.milestones must be non-empty")
    milestone_ids: list[str] = []
    by_id: dict[str, dict[str, object]] = {}
    for milestone in milestones:
        if not isinstance(milestone, dict) or set(milestone) != {
            "id",
            "owner",
            "mutable_surfaces",
            "dependencies",
            "readiness",
            "vertical",
            "independently_closable",
        }:
            fail(
                "milestones must declare id, owner, mutable_surfaces, dependencies, readiness, vertical and independently_closable"
            )
        identifier = milestone["id"]
        owner = milestone["owner"]
        surfaces = milestone["mutable_surfaces"]
        dependencies = milestone["dependencies"]
        readiness = milestone["readiness"]
        if not isinstance(identifier, str) or not identifier:
            fail("milestone ids must be non-empty semantic strings")
        if identifier in by_id:
            fail(f"milestone ids must be unique: {identifier}")
        if not isinstance(owner, str) or not owner:
            fail(f"milestone {identifier} must have one owner")
        if (
            not isinstance(surfaces, list)
            or not surfaces
            or not all(isinstance(item, str) and item for item in surfaces)
        ):
            fail(f"milestone {identifier} must declare non-empty mutable surfaces")
        if len(surfaces) != len(set(surfaces)):
            fail(f"milestone {identifier} repeats a mutable surface")
        if not isinstance(dependencies, list) or not all(isinstance(item, str) and item for item in dependencies):
            fail(f"milestone {identifier} dependencies must be semantic ids")
        if identifier in dependencies:
            fail(f"milestone {identifier} cannot depend on itself")
        if readiness not in {"completed", "ready", "blocked", "pending"}:
            fail(f"milestone {identifier} has unsupported readiness")
        if milestone["vertical"] is not True or milestone["independently_closable"] is not True:
            fail(f"milestone {identifier} must be independently closable vertical work")
        milestone_ids.append(identifier)
        by_id[identifier] = milestone

    known_ids = set(milestone_ids)
    for identifier, milestone in by_id.items():
        dependencies = milestone["dependencies"]
        assert isinstance(dependencies, list)
        unknown = sorted(set(dependencies) - known_ids)
        if unknown:
            fail(f"milestone {identifier} has unknown dependencies: {unknown}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            fail("milestone dependency graph must be acyclic")
        if identifier in visited:
            return
        visiting.add(identifier)
        dependencies = by_id[identifier]["dependencies"]
        assert isinstance(dependencies, list)
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in milestone_ids:
        visit(identifier)

    current_readiness = value["current_readiness"]
    if not isinstance(current_readiness, dict) or set(current_readiness) != known_ids:
        fail("milestone_graph.current_readiness must cover every milestone exactly once")
    for identifier, readiness in current_readiness.items():
        if readiness != by_id[identifier]["readiness"]:
            fail(f"milestone_graph readiness disagrees for {identifier}")
    for identifier, milestone in by_id.items():
        if milestone["readiness"] == "ready":
            dependencies = milestone["dependencies"]
            assert isinstance(dependencies, list)
            if any(by_id[dependency]["readiness"] != "completed" for dependency in dependencies):
                fail(f"ready milestone {identifier} has a non-completed dependency")

    serial_edges = value["serial_edges"]
    if not isinstance(serial_edges, list):
        fail("milestone_graph.serial_edges must be a list")
    expected_edges = {
        (identifier, dependency)
        for identifier, milestone in by_id.items()
        for dependency in milestone["dependencies"]  # type: ignore[union-attr]
    }
    actual_edges: set[tuple[str, str]] = set()
    for edge in serial_edges:
        if not isinstance(edge, dict) or set(edge) != {"from", "to", "reason"}:
            fail("serial edges must declare from, to and reason")
        source = edge["from"]
        target = edge["to"]
        reason = edge["reason"]
        if not isinstance(source, str) or not isinstance(target, str) or (target, source) not in expected_edges:
            fail("serial edge must match a declared dependency")
        if (source, target) in actual_edges:
            fail("serial edges must be unique")
        if not isinstance(reason, str) or reason.strip().lower() not in PERMITTED_SERIAL_EDGE_REASONS:
            fail(
                "serial edge reason must be shared schema, state authority, entrypoint, migration order or acceptance dependency"
            )
        actual_edges.add((source, target))
    if actual_edges != {(dependency, identifier) for identifier, dependency in expected_edges}:
        fail("every retained serial dependency must have one concrete reason")

    authorities = value["shared_authorities_frozen"]
    if (
        not isinstance(authorities, list)
        or not authorities
        or not all(isinstance(item, str) and item for item in authorities)
    ):
        fail("shared_authorities_frozen must be a non-empty list")
    authority_text = " ".join(authorities).lower()
    for required_authority in ("schema", "contract", "state authority", "entrypoint"):
        if required_authority not in authority_text:
            fail(f"shared authority freeze must name {required_authority}")
    fan_out_after = value["fan_out_after"]
    if not isinstance(fan_out_after, str) or fan_out_after not in known_ids:
        fail("fan_out_after must identify a milestone in the DAG")
    if by_id[fan_out_after]["readiness"] != "completed":
        fail("shared authority foundation must be completed before fan-out")

    def reaches_foundation(identifier: str) -> bool:
        dependencies = by_id[identifier]["dependencies"]
        assert isinstance(dependencies, list)
        return fan_out_after in dependencies or any(reaches_foundation(item) for item in dependencies)

    for identifier in known_ids - {fan_out_after}:
        if by_id[identifier]["readiness"] == "ready" and not reaches_foundation(identifier):
            fail(f"ready milestone {identifier} must follow the shared authority fan-out foundation")

    ready_parallel_groups = value["ready_parallel_groups"]
    if not isinstance(ready_parallel_groups, list) or not ready_parallel_groups:
        fail("ready_parallel_groups must record current ready lanes")
    ready_ids = {identifier for identifier, milestone in by_id.items() if milestone["readiness"] == "ready"}
    grouped_ids: set[str] = set()
    for group in ready_parallel_groups:
        if not isinstance(group, list) or len(group) < 2 or not all(isinstance(item, str) for item in group):
            fail("ready parallel groups must contain at least two ready milestone ids")
        if set(group) & grouped_ids:
            fail("ready parallel groups must not overlap")
        if set(group) - ready_ids:
            fail("parallel groups may contain only currently ready milestones")
        owners = [by_id[identifier]["owner"] for identifier in group]
        surfaces = [set(by_id[identifier]["mutable_surfaces"]) for identifier in group]
        if len(owners) != len(set(owners)):
            fail("parallel milestones must have disjoint single-owner lanes")
        for index, surface_set in enumerate(surfaces):
            if any(surface_set & other for other in surfaces[index + 1 :]):
                fail("parallel milestones must have disjoint mutable surfaces")
        grouped_ids.update(group)
    if grouped_ids != ready_ids:
        fail("current ready milestones must be fully represented by parallel groups")
    ready_owners = [by_id[identifier]["owner"] for identifier in ready_ids]
    if len(ready_owners) != len(set(ready_owners)):
        fail("ready parallel milestones must have disjoint owners")

    critical_path = value["critical_path"]
    if (
        not isinstance(critical_path, list)
        or not critical_path
        or not all(isinstance(item, str) for item in critical_path)
    ):
        fail("critical_path must be a non-empty milestone sequence")
    if set(critical_path) - known_ids:
        fail("critical_path contains an unknown milestone")
    first_dependencies = by_id[critical_path[0]]["dependencies"]
    assert isinstance(first_dependencies, list)
    if first_dependencies:
        fail("critical_path must begin at a foundation milestone")
    dependents = {
        dependency
        for milestone in by_id.values()
        for dependency in milestone["dependencies"]  # type: ignore[union-attr]
    }
    if critical_path[-1] in dependents:
        fail("critical_path must end at a terminal milestone")
    dependency_pairs = {
        (dependency, identifier) for identifier, milestone in by_id.items() for dependency in milestone["dependencies"]
    }  # type: ignore[union-attr]
    if any((source, target) not in dependency_pairs for source, target in itertools.pairwise(critical_path)):
        fail("critical_path must follow the dependency DAG")
    if value["critical_path_minimized"] is not True:
        fail("planning must minimize the safe critical path")
    if value["nested_controller_default"] is not False:
        fail("nested swarm/controller orchestration cannot be the default")


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


def validate_public_semantic_cutover() -> None:
    """Enforce the forward-only semantic source and CLI surface.

    Historical protocol values and retained evidence are validated elsewhere;
    this check is intentionally limited to importable source and command
    registrations so those compatibility exceptions remain readable without
    preserving removed public aliases.
    """

    source_root = ROOT / "src" / "codex_flow"
    semantic_modules = SEMANTIC_PUBLIC_MODULES
    removed_modules = REMOVED_PUBLIC_MODULES
    missing = sorted(name for name in semantic_modules if not (source_root / name).is_file())
    present_removed = sorted(name for name in removed_modules if (source_root / name).exists())
    if missing:
        fail("semantic public modules are missing: " + ", ".join(missing))
    if present_removed:
        fail("removed public modules remain: " + ", ".join(present_removed))

    source_text = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.glob("*.py") if path.name != "descriptive_naming.py"
    )
    semantic_identifiers = SEMANTIC_PUBLIC_IDENTIFIERS
    removed_identifiers = REMOVED_PUBLIC_IDENTIFIERS
    for identifier in semantic_identifiers:
        if not re.search(rf"\b{re.escape(identifier)}\b", source_text):
            fail(f"semantic public identifier is missing: {identifier}")
    for identifier in removed_identifiers:
        if re.search(rf"\b{re.escape(identifier)}\b", source_text):
            fail(f"removed public identifier remains reachable: {identifier}")

    cli_text = (source_root / "cli.py").read_text(encoding="utf-8")
    diagnostic_commands = {
        "schema-compatibility",
        "worker-sentinel",
        "review-pilot",
        "multi-authority-review-pilot",
        "workflow-control-pilot",
        "production-pilots",
        "sdk-compatibility-sentinel",
        "controller-recovery-sentinel",
        "live-control-sentinel",
    }
    removed_commands = {
        "h4-pilot",
        "h4b-pilot",
        "h5-pilot",
        "h6-pilot",
        "h6-app-pilot-capsule",
        "sdk-sentinel",
        "controller-sentinel",
    }
    for command in diagnostic_commands:
        if f'@diagnostics_app.command("{command}")' not in cli_text:
            fail(f"diagnostic CLI command is missing: {command}")
    if '@app.command("visible-worker-capsule", hidden=True)' not in cli_text:
        fail("semantic CLI command is missing: visible-worker-capsule")
    if "_register_hidden_diagnostic_aliases" not in cli_text:
        fail("diagnostic root compatibility aliases are missing")
    for command in removed_commands:
        if f'@app.command("{command}")' in cli_text:
            fail(f"removed CLI command remains registered: {command}")


def validate_workflow_control_evidence() -> None:
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
        "dispatch_id_sha256",
        "result_durable_status",
        "worker_result_acknowledged",
        "worker_attempts_observed",
        "git_head_unchanged",
        "supervisor_clean_shutdown",
        "temporary_repository_removed",
    }
    if set(evidence) != required | {
        "base_sha",
        "controller_exit_code",
        "final_file_sha256",
        "protected_file_sha256",
    }:
        fail("H5 evidence shape is not the sanitized controller contract")
    if evidence.get("schema") != "codex-flow/h5-workflow-control-medium/v1":
        fail("H5 evidence schema is unsupported")
    if evidence.get("status") != "passed" or evidence.get("route") != "workflow-control/codex-flow":
        fail("H5 evidence does not prove controller reachability")
    if (
        evidence.get("controller_exit_code") != 0
        or evidence.get("controller_status") != "completed"
        or evidence.get("controller_checkpoint") is not None
        or evidence.get("worker_result_acknowledged") is not True
    ):
        fail("H5 evidence does not prove a durable terminal result")
    if (
        evidence.get("result_status") != "completed"
        or evidence.get("result_durable_status") != "controller_acknowledged"
        or evidence.get("observable_edit") is not True
        or evidence.get("worker_attempts_observed") != 1
    ):
        fail("H5 evidence does not prove the observable medium outcome")
    if (
        evidence.get("protected_unchanged") is not True
        or evidence.get("git_head_unchanged") is not True
        or evidence.get("supervisor_clean_shutdown") is not True
        or evidence.get("temporary_repository_removed") is not True
    ):
        fail("H5 evidence does not prove protected-surface integrity")
    command = evidence.get("command")
    if command != ["codex-flow", "control"]:
        fail("H5 evidence must name the controller entrypoint")
    changed_paths = evidence.get("authorized_changed_paths")
    if changed_paths != ["M workflow_result.txt"]:
        fail("H5 evidence changed-path scope is not the bounded medium outcome")
    if evidence.get("final_file_sha256") != "be9e35885d26bab9689ab7a97cac19b9eea7deeb4ba071ab394057229ef9caf7":
        fail("H5 evidence does not bind the exact observable file bytes")
    if evidence.get("protected_file_sha256") != "4378f5f8c155589bc148ca4213669f0b620dd18942e10450eae5779ce6c59beb":
        fail("H5 evidence does not bind the exact protected file bytes")
    for field, length in (("base_sha", 40), ("dispatch_id_sha256", 64)):
        value = evidence.get(field)
        if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
            fail(f"H5 evidence {field} is malformed")


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
        "Every Luna task uses `speed=fast` by default",
        "When the native task schema advertises a `speed` field",
        "do not send an unsupported field",
        "app's configured fast speed",
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
    readme_required = (
        "Authorized native routing defaults:",
        "Visual-judgment implementation",
        "acceptance modes: `objective`, `visual`, and `architecture`",
        "Objective code review uses Luna XHigh",
        "visual implementation defaults to Astra Low",
        "Ordinary recovery defaults to Astra Low",
        "Visual ambiguity, non-convergence, or material recovery complexity explicitly escalates to Astra Medium",
        "Normal architecture conformance and semantic orchestration use Sol Medium",
        "combined architecture/security review",
        "High is an explicit exceptional escalation only",
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
    validate_readme_routing_table(README_PATH)


def _configured_route_pair(path: Path, role: str, *, effort_key: str) -> tuple[str, str]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"{path}: invalid workflow configuration: {exc}")
    roles = config.get("roles")
    route = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(route, dict):
        fail(f"{path}: missing routing role {role!r}")
    model = route.get("model")
    effort = route.get(effort_key)
    if not isinstance(model, str) or not isinstance(effort, str):
        fail(f"{path}: routing role {role!r} must define model and {effort_key}")
    return model, effort


def _validate_authorized_routing_defaults(workflow_path: Path, example_path: Path) -> None:
    """Reject drift from the user-authorized role boundaries."""

    workflow_expected = {
        "planner": ("gpt-6-astra", "medium"),
        "executor": ("gpt-5.6-luna", "xhigh"),
        "code-reviewer": ("gpt-5.6-luna", "xhigh"),
        "visual-reviewer": ("gpt-6-astra", "low"),
        "architecture-reviewer": ("gpt-5.6-sol", "medium"),
        # Recovery diagnosis is the explicit Medium escalation; ordinary
        # recovery implementation is recover_local below.
        "recovery": ("gpt-6-astra", "medium"),
        "decision": ("gpt-5.6-sol", "medium"),
    }
    for role, expected in workflow_expected.items():
        actual = _configured_route_pair(workflow_path, role, effort_key="reasoning_effort")
        if actual != expected:
            fail(f"{workflow_path}: authorized route for {role!r} drifted; expected={expected}, actual={actual}")

    example_expected = {
        "plan": ("gpt-6-astra", "medium"),
        "execute_bounded": ("gpt-5.6-luna", "xhigh"),
        "execute_substantial": ("gpt-5.6-luna", "xhigh"),
        "execute_visual": ("gpt-6-astra", "low"),
        "review": ("gpt-5.6-luna", "xhigh"),
        "review_implementation": ("gpt-5.6-luna", "xhigh"),
        "review_visual": ("gpt-6-astra", "low"),
        "recover_local": ("gpt-6-astra", "low"),
        "recover_architecture": ("gpt-6-astra", "medium"),
    }
    for role, expected in example_expected.items():
        actual = _configured_route_pair(example_path, role, effort_key="thinking")
        if actual != expected:
            fail(f"{example_path}: authorized route for {role!r} drifted; expected={expected}, actual={actual}")


def _readme_route_rows(path: Path) -> dict[str, tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"{path}: unable to read routing table: {exc}")
    header = "| Situation | Task context | Native `model` | Native `thinking` |"
    in_table = False
    rows: dict[str, tuple[str, str]] = {}
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped:
            break
        if not stripped.startswith("|"):
            fail(f"{path}: routing table ended before its rows were complete")
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 4:
            fail(f"{path}: routing table row must contain four cells")
        if all(set(cell) <= {"-", " "} for cell in cells):
            continue
        label, _context, model, effort = cells
        if label in rows:
            fail(f"{path}: routing table repeats {label!r}")
        rows[label] = (model.strip("`"), effort.strip("`"))
    if not rows:
        fail(f"{path}: routing table is missing or empty")
    return rows


def validate_readme_routing_table(path: Path | None = None) -> None:
    """Keep public route rows bound to the existing controller configurations."""

    readme_path = ROOT / "README.md" if path is None else path
    _validate_authorized_routing_defaults(ROOT / "workflow.toml", ROOT / "config" / "workflow.toml.example")
    rows = _readme_route_rows(readme_path)
    workflow_path = ROOT / "workflow.toml"
    example_path = ROOT / "config" / "workflow.toml.example"
    workflow_roles = {
        role: _configured_route_pair(workflow_path, role, effort_key="reasoning_effort")
        for role in ("planner", "architecture-reviewer", "recovery", "decision")
    }
    example_roles = {
        role: _configured_route_pair(example_path, role, effort_key="thinking")
        for role in ("execute_substantial", "execute_visual", "recover_local", "review_visual", "plan", "review")
    }
    expected = {
        "Small/local change": ("gpt-5.6-luna", "high"),
        "Decision-ready substantial milestone": example_roles["execute_substantial"],
        "Visual-judgment implementation (slides, landing pages, frontend/UI, rendered documents)": example_roles[
            "execute_visual"
        ],
        "Ordinary recovery implementation": example_roles["recover_local"],
        "Independent visual-quality promotion review": example_roles["review_visual"],
        "First-time large or uncertain program": example_roles["plan"],
        "Architecture conformance": workflow_roles["architecture-reviewer"],
        "Recovery diagnosis": workflow_roles["recovery"],
        "Controller decisions": workflow_roles["decision"],
        "Significant/ambiguous architecture or security boundary": workflow_roles["planner"],
        "Mechanical repair after a precise finding": ("gpt-5.6-luna", "high"),
        "Independent objective/code review": example_roles["review"],
    }
    if set(rows) != set(expected):
        missing = sorted(set(expected) - set(rows))
        unexpected = sorted(set(rows) - set(expected))
        fail(f"{readme_path}: routing table labels drifted; missing={missing}, unexpected={unexpected}")
    for label, route in expected.items():
        if rows[label] != route:
            fail(f"{readme_path}: route for {label!r} drifted; expected={route}, actual={rows[label]}")


def validate_global_agents_template() -> None:
    text = GLOBAL_AGENTS_PATH.read_text(encoding="utf-8")
    normalized_text = " ".join(text.split())
    required_text = (
        "$plan-work",
        "$execute-milestone",
        "$codex-thread-handoff",
        "$workflow-control",
        "`docs/reviews/`",
        "normal execution",
        "canonical plan path",
        "exact milestone id",
        "packaged controller entrypoint",
        "planner does not create a peer",
        "serialize a sidecar capsule",
        "explicit legacy compatibility",
        "deliberate comparison route",
        "never select it silently",
        "combine it with",
        "normal execution path",
        "Astra Medium architecture thread",
        "Luna XHigh",
        "model=gpt-5.6-luna, thinking=xhigh",
        "model=gpt-5.6-sol, thinking=medium",
        "model=gpt-6-astra, thinking=medium",
        "model=gpt-6-astra, thinking=low",
        "model=gpt-6-astra, thinking=high",
        "Every Luna task uses `speed=fast` by default",
        "If the native schema advertises `speed`, pass `speed=fast`",
        "app's configured fast speed",
        "do not claim that speed enforcement occurred",
        "Every milestone declares one or more acceptance modes",
        "Route by the judgment required for acceptance, not by file type",
        "Objective code review uses Luna XHigh",
        "Architecture-conformance review uses Sol Medium",
        "single mutable implementation owner",
        "independent visual-quality promotion review uses Astra Low",
        "combined review escalates to Astra Medium",
        "High is an explicit exceptional escalation only",
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

    stale_normal_routes = (
        "uses `$codex-thread-handoff` to create a fresh peer execution thread",
        "uses `$codex-thread-handoff` for normal execution",
        "fresh peer execution thread when native peer creation is available",
    )
    for stale in stale_normal_routes:
        if stale in normalized_text:
            fail(f"{GLOBAL_AGENTS_PATH}: legacy handoff cannot be the normal execution route: {stale}")

    stale_routing = (
        "Visual-judgment implementation uses Astra Medium",
        "thinking=medium for planning, visual implementation or recovery",
        "Astra Medium diagnostic continuation",
    )
    for stale in stale_routing:
        if stale in normalized_text:
            fail(f"{GLOBAL_AGENTS_PATH}: stale routing contract: {stale}")

    legacy_route = "$codex-thread-handoff` is only an explicit legacy compatibility or deliberate comparison route"
    if legacy_route not in normalized_text:
        fail(f"{GLOBAL_AGENTS_PATH}: legacy handoff route must be explicit and never silently selected")

    lowered = text.lower()
    for token in FORBIDDEN_PROJECT_TEXT:
        if token in lowered:
            fail(f"{GLOBAL_AGENTS_PATH}: project-specific text is not allowed: {token}")
    for token in FORBIDDEN_AUTHORING_PATHS:
        if token in text:
            fail(f"{GLOBAL_AGENTS_PATH}: local authoring path is not allowed: {token}")


def validate_milestone_graph_contract() -> None:
    """Enforce the architecture-first planning and fan-out policy in prose."""

    paths = (
        ROOT / "AGENTS.md",
        GLOBAL_AGENTS_PATH,
        ROOT / "templates" / "AGENTS.workflow.md",
        WORKFLOW_PATHS["plan"],
    )
    normalized = {path: " ".join(path.read_text(encoding="utf-8").lower().split()) for path in paths}
    required_by_path = {
        ROOT / "AGENTS.md": (
            "architecture map",
            "dependency dag",
            "current readiness",
            "independently closable vertical milestones",
            "disjoint mutable surfaces",
            "freeze shared schemas",
            "state authority",
            "production entrypoints",
            "serial edge",
            "migration order",
            "acceptance dependency",
            "critical path",
            "fake boundaries",
            "single-owner",
            "ready disjoint lanes",
            "nested swarm/controller",
        ),
        GLOBAL_AGENTS_PATH: (
            "architecture-first milestone graph",
            "dependency dag",
            "current readiness",
            "independently closable vertical milestones",
            "disjoint mutable surfaces",
            "shared schemas",
            "state authority",
            "production entrypoints",
            "serial edge",
            "migration order",
            "acceptance dependency",
            "critical path",
            "fake boundaries",
            "single-owner",
            "ready milestones",
            "nested swarm/controller",
        ),
        ROOT / "templates" / "AGENTS.workflow.md": (
            "dependency dag",
            "current readiness",
            "independently closable vertical milestones",
            "disjoint mutable surfaces",
            "shared schemas",
            "state authority",
            "production entrypoints",
            "serial edge",
            "migration order",
            "acceptance dependency",
            "critical path",
            "fake boundaries",
            "single-owner",
            "ready disjoint lanes",
            "nested swarm/controller",
        ),
        WORKFLOW_PATHS["plan"]: (
            "dependency dag",
            "current readiness",
            "independently closable vertical milestones",
            "disjoint mutable surfaces",
            "shared schemas",
            "state authority",
            "production entrypoints",
            "serial edge",
            "migration order",
            "acceptance dependency",
            "critical path",
            "fake boundaries",
            "single-owner",
            "ready milestones",
            "nested swarm",
        ),
    }
    for path, required in required_by_path.items():
        for token in required:
            if token not in normalized[path]:
                fail(f"{path}: missing architecture-first milestone contract: {token}")

    # A mention of nested orchestration is safe only when it rejects the
    # policy. This catches a template that quietly restores a nested default.
    for path, text in normalized.items():
        for sentence in re.split(r"[.!?]", text):
            if "nested" in sentence and re.search(r"\b(default|normal)\b", sentence):
                if not re.search(r"\b(reject|rejected|never|not|without|prohibit|do not)\b", sentence):
                    fail(f"{path}: nested swarm/controller orchestration cannot be the default")


def validate_semantic_density_contract() -> None:
    """Keep the shared implementation policy biased toward fewer stronger concepts."""

    workflow_template = ROOT / "templates" / "AGENTS.workflow.md"
    paths = (
        ROOT / "AGENTS.md",
        GLOBAL_AGENTS_PATH,
        workflow_template,
        WORKFLOW_PATHS["plan"],
        WORKFLOW_PATHS["execute"],
        WORKFLOW_PATHS["review"],
    )
    normalized = {path: " ".join(path.read_text(encoding="utf-8").lower().split()) for path in paths}
    required_by_path = {
        ROOT / "AGENTS.md": (
            "semantic density",
            "semantic compression, not abstraction count",
            "under-abstraction",
            "disappear from call sites",
            "smallest representation that makes the invariant obvious",
            "new vocabulary is more expensive than new lines",
            "strict edges and boring interiors",
            "functions are the default",
            "one production implementation",
            "tests alone",
            "pass-through layers",
            "typed boundary codecs",
            "incidental syntax",
            "externally meaningful guarantees",
        ),
        GLOBAL_AGENTS_PATH: (
            "semantic density",
            "semantic compression, not abstraction count",
            "under-abstraction",
            "disappear from call sites",
            "smallest representation that makes the invariant obvious",
            "new vocabulary is more expensive than new lines",
            "strict edges and boring interiors",
            "functions are the default",
            "one-implementation protocol",
            "tests alone",
            "pass-through layers",
            "typed boundary codecs",
            "incidental syntax",
            "observable guarantees",
        ),
        workflow_template: (
            "semantic density",
            "semantic compression, not abstraction count",
            "under-abstraction",
            "disappear from call sites",
            "smallest representation that makes the invariant obvious",
            "strict edges and boring interiors",
            "functions are the default",
            "one-implementation protocol",
            "semantic vocabulary as a budget",
            "pass-through layers",
            "typed boundary codecs",
            "incidental syntax",
            "observable guarantees",
        ),
        WORKFLOW_PATHS["plan"]: (
            "budget semantic vocabulary",
            "semantic delta",
            "new domain concepts",
            "new compatibility paths",
            "repeated semantic patterns compressed",
            "functions and direct composition",
            "one-implementation protocol",
            "pass-through layers",
            "disappear from call sites",
            "incidental mechanical repetition",
        ),
        WORKFLOW_PATHS["execute"]: (
            "preserve semantic density",
            "semantic delta",
            "strict edges and boring interiors",
            "one-implementation protocol",
            "pass-through layers",
            "semantic compression",
            "disappear from call sites",
            "incidental mechanical repetition",
            "observable guarantees",
        ),
        WORKFLOW_PATHS["review"]: (
            "semantic density",
            "planned and implemented semantic delta",
            "multiple symbols expressing one concept",
            "one-implementation protocols",
            "pass-through services/adapters",
            "under-abstraction",
            "disappear from call sites",
            "incidental mechanical repetition",
            "tests that pin implementation ceremony",
        ),
    }
    for path, required in required_by_path.items():
        for token in required:
            if token not in normalized[path]:
                fail(f"{path}: missing semantic-density contract: {token}")


def validate_git_lane_integration_contract() -> None:
    """Enforce commit-addressed integration trunk and parallel lane policy."""

    canonical_plan = ROOT / "docs" / "reviews" / "peer-thread-workflow.md"
    paths = (
        ROOT / "AGENTS.md",
        GLOBAL_AGENTS_PATH,
        ROOT / "templates" / "AGENTS.workflow.md",
        WORKFLOW_PATHS["plan"],
        WORKFLOW_PATHS["execute"],
        WORKFLOW_PATHS["review"],
        WORKFLOW_PATHS["recover"],
        WORKFLOW_PATHS["control"],
        canonical_plan,
    )
    normalized = {path: " ".join(path.read_text(encoding="utf-8").lower().split()) for path in paths}
    required_by_path = {
        ROOT / "AGENTS.md": (
            "sole local integration trunk",
            "frozen dag base",
            "unrelated dirty baseline",
            "physical sibling git worktree",
            "git diff --cached --check",
            "exact lane commit or commit range",
            "successor commit",
            "merge commit",
            "verify ancestry",
            "new integrated tip",
            "does not authorize push, rebase, history rewrite, discard, remote mutation",
        ),
        GLOBAL_AGENTS_PATH: (
            "sole local integration trunk",
            "frozen dag base",
            "unrelated dirty bytes",
            "physical siblings",
            "git diff --cached --check",
            "exact lane commit or range",
            "successor commits",
            "merge commit",
            "verify ancestry",
            "new integrated tip",
            "push, rebase, history rewrite, discard, remote mutation",
        ),
        ROOT / "templates" / "AGENTS.workflow.md": (
            "sole local integration trunk",
            "dag base",
            "unrelated dirty baseline",
            "physically sibling git worktrees",
            "git diff --cached --check",
            "exact commit tips/ranges",
            "successor commits",
            "merge commit",
            "verify ancestry",
            "new integrated tip",
            "push, rebase, history rewrite, discard, remote mutation",
        ),
        WORKFLOW_PATHS["plan"]: (
            "sole local integration trunk",
            "fan_out_base",
            "unrelated dirty baseline",
            "physical sibling worktree",
            "git diff --cached --check",
            "review commit/range",
            "successor commit",
            "merge commit",
            "ancestry/extraneous-commit verification",
            "new integrated tip",
            "push, rebase, history rewrite, discard, remote mutation",
        ),
        WORKFLOW_PATHS["execute"]: (
            "coherent local commit",
            "stage only owned surfaces",
            "git diff --cached --check",
            "exact commit tip or range",
            "do not merge into the program integration trunk",
            "amend a reviewed commit",
        ),
        WORKFLOW_PATHS["review"]: (
            "exact lane commit tip/range",
            "plan-recorded integration base",
            "unrelated commits or surfaces",
            "successor commit",
            "amending a reviewed commit invalidates",
            "never integrates the lane",
        ),
        WORKFLOW_PATHS["recover"]: (
            "successor local commit",
            "git diff --cached --check",
            "never amend a reviewed commit",
            "integrate the lane into the program trunk",
        ),
        WORKFLOW_PATHS["control"]: (
            "long-lived program integration worktree",
            "frozen verified commit sha",
            "physical sibling worktrees",
            "workers commit only their lane",
            "exact-commit review",
            "new trunk tip",
            "does not run git operations itself",
        ),
        canonical_plan: (
            "sole local integration trunk",
            "frozen dag base",
            "dirty baseline",
            "physical sibling git worktrees",
            "git diff --cached --check",
            "exact lane tip or commit range",
            "successor commit",
            "merge commit",
            "verifies ancestry",
            "new trunk tip",
            "does not authorize push, rebase, history rewrite, discard, remote mutation",
            "no checkpoint sha is fabricated",
        ),
    }
    for path, required in required_by_path.items():
        for token in required:
            if token not in normalized[path]:
                fail(f"{path}: missing Git lane integration contract: {token}")


def _validate_structured_identifier(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a non-empty string")
    try:
        validate_identifier_name(value)
    except ValueError as exc:
        fail(f"{label}: {exc}")


def _validate_structured_path(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a non-empty string")
    try:
        validate_path_name(value)
    except ValueError as exc:
        fail(f"{label}: {exc}")


def _load_structured_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{path}: invalid structured JSON: {exc}")


def _validate_architecture_identifiers(value: object, *, path: Path) -> None:
    if not isinstance(value, dict):
        fail(f"{path}: architecture fixture root must be an object")
    architecture = value.get("architecture_map")
    if not isinstance(architecture, dict):
        fail(f"{path}: architecture_map must be an object")
    primary_identifiers = architecture.get("primary_identifiers")
    if isinstance(primary_identifiers, list):
        for index, identifier in enumerate(primary_identifiers):
            _validate_structured_identifier(identifier, label=f"{path}: primary_identifiers[{index}]")
    graph = architecture.get("milestone_graph")
    if not isinstance(graph, dict):
        return
    milestones = graph.get("milestones")
    if not isinstance(milestones, list):
        return
    for index, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            continue
        prefix = f"{path}: milestone_graph.milestones[{index}]"
        for key in ("id", "owner"):
            if key in milestone:
                _validate_structured_identifier(milestone[key], label=f"{prefix}.{key}")
        dependencies = milestone.get("dependencies")
        if isinstance(dependencies, list):
            for dependency_index, dependency in enumerate(dependencies):
                _validate_structured_identifier(
                    dependency,
                    label=f"{prefix}.dependencies[{dependency_index}]",
                )
    current_readiness = graph.get("current_readiness")
    if isinstance(current_readiness, dict):
        for identifier in current_readiness:
            _validate_structured_identifier(identifier, label=f"{path}: milestone_graph.current_readiness key")
    serial_edges = graph.get("serial_edges")
    if isinstance(serial_edges, list):
        for index, edge in enumerate(serial_edges):
            if isinstance(edge, dict):
                for endpoint in ("from", "to"):
                    if endpoint in edge:
                        _validate_structured_identifier(
                            edge[endpoint], label=f"{path}: milestone_graph.serial_edges[{index}].{endpoint}"
                        )
    for field in ("critical_path", "ready_parallel_groups"):
        values = graph.get(field)
        if not isinstance(values, list):
            continue
        flattened = (
            values
            if field == "critical_path"
            else [item for group in values if isinstance(group, list) for item in group]
        )
        for index, identifier in enumerate(flattened):
            _validate_structured_identifier(identifier, label=f"{path}: milestone_graph.{field}[{index}]")
    if "fan_out_after" in graph:
        _validate_structured_identifier(graph["fan_out_after"], label=f"{path}: milestone_graph.fan_out_after")
    paths = architecture.get("paths")
    if isinstance(paths, list):
        for index, entry in enumerate(paths):
            if not isinstance(entry, dict):
                continue
            prefix = f"{path}: architecture_map.paths[{index}]"
            if "path" in entry:
                _validate_structured_path(entry["path"], label=f"{prefix}.path")
            if "owner" in entry:
                _validate_structured_identifier(entry["owner"], label=f"{prefix}.owner")


_GENERATED_FILENAME_FIELDS = frozenset(
    {"filename", "file_name", "generated_filename", "generated_filenames", "output_filename", "artifact_filename"}
)
_VISUAL_EVIDENCE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})
_VISUAL_CONTRACT_EVIDENCE = frozenset(
    {
        "docs/reviews/evidence/human-terminal-ui/conversation-first-visual-contract.md",
        "docs/reviews/evidence/human-terminal-ui/conversation-first-visual-contract.yaml",
    }
)
_IMMUTABLE_ACCEPTED_EVIDENCE = frozenset(
    {
        "docs/reviews/evidence/h1-sdk-sentinel.json",
        "docs/reviews/evidence/h3-controller-sentinel.json",
        "docs/reviews/evidence/h4-a-objective-pilot.json",
        "docs/reviews/evidence/h4-b-multi-authority-pilot.json",
        "docs/reviews/evidence/h5-workflow-control-medium.json",
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
        "docs/reviews/evidence/live-worker-control.json",
    }
)
_IMMUTABLE_PROTOCOL_PYTHON_IDENTIFIERS = frozenset(
    {
        "_encode_h4_event_data",
        "_ensure_h4_store",
        "test_sensitive_fields_and_h1_exception_details_never_persist",
    }
)


def _validate_generated_filename_fields(value: object, *, path: Path, field_path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{field_path}.{key}"
            if key.lower() in _GENERATED_FILENAME_FIELDS:
                values = child if isinstance(child, list) else (child,)
                for index, filename in enumerate(values):
                    _validate_structured_path(filename, label=f"{path}: {child_path}[{index}]")
            _validate_generated_filename_fields(child, path=path, field_path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_generated_filename_fields(child, path=path, field_path=f"{field_path}[{index}]")


def _validate_mutable_evidence_keys(value: object, *, path: Path, field_path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            _validate_structured_identifier(key, label=f"{path}: mutable evidence key {field_path}.{key}")
            _validate_mutable_evidence_keys(child, path=path, field_path=f"{field_path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_mutable_evidence_keys(child, path=path, field_path=f"{field_path}[{index}]")


def _validate_python_identifiers(path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(f"{path}: invalid Python source: {exc}")
    seen: set[str] = set()
    for node in ast.walk(tree):
        names: tuple[str, ...] = ()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = (node.name,)
        elif isinstance(node, ast.Name):
            names = (node.id,)
        elif isinstance(node, ast.arg):
            names = (node.arg,)
        elif isinstance(node, ast.alias):
            names = (node.asname or node.name.rsplit(".", 1)[-1],)
        elif isinstance(node, ast.Attribute):
            names = (node.attr,)
        for name in names:
            if name in seen or name in _IMMUTABLE_PROTOCOL_PYTHON_IDENTIFIERS:
                continue
            seen.add(name)
            _validate_structured_identifier(name, label=f"{path}: Python identifier")


def _validate_structured_content(changed_paths: tuple[str, ...]) -> None:
    """Validate identifier-bearing fields without scanning arbitrary prose."""

    for raw_path in changed_paths:
        relative = Path(raw_path)
        path = relative if relative.is_absolute() else ROOT / relative
        if not path.is_file():
            continue
        if relative.is_absolute():
            try:
                normalized = path.relative_to(ROOT).as_posix()
            except ValueError:
                continue
        else:
            normalized = relative.as_posix()
        if normalized.endswith(".py"):
            _validate_python_identifiers(path)
        if normalized == "tests/fixtures/prompt-input/plan-work.json":
            fixture = _load_structured_json(path)
            _validate_architecture_identifiers(fixture, path=path)
            _validate_generated_filename_fields(fixture, path=path)
        if normalized.startswith("schemas/") and path.name.endswith(".schema.json"):
            schema = _load_structured_json(path)
            if isinstance(schema, dict):
                if "$id" in schema:
                    _validate_structured_path(schema["$id"], label=f"{path}: schema $id")
                if "title" in schema:
                    _validate_structured_identifier(schema["title"], label=f"{path}: schema title")
        if normalized.startswith("docs/reviews/evidence/") and normalized not in _IMMUTABLE_ACCEPTED_EVIDENCE:
            if path.suffix.lower() in _VISUAL_EVIDENCE_SUFFIXES or normalized in _VISUAL_CONTRACT_EVIDENCE:
                continue
            if path.suffix.lower() != ".json":
                fail(f"{path}: unsupported mutable evidence format")
            _validate_structured_path(normalized, label=f"{path}: mutable evidence path")
            evidence = _load_structured_json(path)
            _validate_mutable_evidence_keys(evidence, path=path)
            _validate_generated_filename_fields(evidence, path=path)


def validate_descriptive_names() -> None:
    """Reject coupled paths and structured identifiers in the promotion gate."""

    changed_paths = changed_paths_from_git(ROOT)
    violations = validate_changed_names(
        changed_paths,
        immutable_paths=_IMMUTABLE_ACCEPTED_EVIDENCE,
        protocol_names=PERSISTED_PROTOCOL_NAMES,
    )
    if violations:
        fail("descriptive-name violations: " + "; ".join(violations))
    _validate_structured_content(changed_paths)


def _schema_paths() -> tuple[Path, ...]:
    return tuple(SCHEMAS_ROOT / f"{name}.schema.json" for name in SCHEMA_NAMES)


def _walk_schema_nodes(value: object) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_walk_schema_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_walk_schema_nodes(child))
    return nodes


def validate_schemas() -> None:
    if Draft202012Validator is None:
        fail("jsonschema is required to validate Draft 2020-12 workflow schemas")
    actual = tuple(sorted(path.name.removesuffix(".schema.json") for path in SCHEMAS_ROOT.glob("*.schema.json")))
    if actual != tuple(sorted(SCHEMA_NAMES)):
        fail(f"schema inventory must be exactly {list(SCHEMA_NAMES)}")
    ids: set[str] = set()
    titles: set[str] = set()
    for path in _schema_paths():
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"{path}: invalid JSON: {exc}")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"{path}: schema must declare Draft 2020-12")
        schema_id = schema.get("$id")
        title = schema.get("title")
        if not isinstance(schema_id, str) or not schema_id:
            fail(f"{path}: schema must have a stable $id")
        if not isinstance(title, str) or not title:
            fail(f"{path}: schema must have a semantic title")
        if schema_id in ids or title in titles:
            fail(f"{path}: schema $id and title values must be unique")
        ids.add(schema_id)
        titles.add(title)
        for node in _walk_schema_nodes(schema):
            if node.get("type") == "object" and not (
                node.get("additionalProperties") is False
                or (
                    isinstance(node.get("additionalProperties"), dict)
                    and isinstance(node.get("propertyNames"), dict)
                    and "minProperties" in node
                    and "maxProperties" in node
                )
            ):
                fail(f"{path}: every object boundary must be closed or a bounded semantic map")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # jsonschema exposes multiple validator exception classes.
            fail(f"{path}: invalid Draft 2020-12 schema: {exc}")

    if json.loads((SCHEMAS_ROOT / "capsule.schema.json").read_text(encoding="utf-8")) != model_facing_capsule_schema():
        fail("static capsule schema differs from the generated typed schema")
    if json.loads((SCHEMAS_ROOT / "result.schema.json").read_text(encoding="utf-8")) != model_facing_result_schema():
        fail("static result schema differs from the generated typed schema")

    _validate_model_projection_closure()


def _validate_model_projection_closure() -> None:
    """Prove the published schemas are the typed controller projections."""

    capsule_path = SCHEMAS_ROOT / "capsule.schema.json"
    result_path = SCHEMAS_ROOT / "result.schema.json"
    capsule_schema = _load_structured_json(capsule_path)
    result_schema = _load_structured_json(result_path)
    if not isinstance(capsule_schema, dict) or not isinstance(result_schema, dict):
        fail("model-facing schemas must be JSON objects")
    result_properties = result_schema.get("properties")
    expected_capsule_keys = {
        "schema_version",
        "objective",
        "decomposition",
        "acceptance_criteria",
        "surfaces",
        "acceptance",
        "prompt",
        "recovery_policy",
        "plugin_requirements",
    }
    expected_result_keys = {
        "schema_version",
        "status",
        "summary",
        "changed_surfaces",
        "validations",
        "durable_status",
        "next_action",
        "blocker",
    }
    branches = capsule_schema.get("oneOf")
    if not isinstance(branches, list) or len(branches) != 4 or any(not isinstance(item, dict) for item in branches):
        fail(f"{capsule_path}: oneOf must contain exactly the closed v1, v2, v3 and v4 branches")
    branch_properties = [item.get("properties") for item in branches[:3]]
    if any(not isinstance(item, dict) for item in branch_properties):
        fail(f"{capsule_path}: v1-v3 branches must contain closed properties")
    schema_v4 = branches[3]
    v4_branches = schema_v4.get("oneOf")
    if not isinstance(v4_branches, list) or len(v4_branches) != 2 or any(
        not isinstance(item, dict) for item in v4_branches
    ):
        fail(f"{capsule_path}: v4 must contain exactly the closed commit and runtime-evidence branches")
    v4_properties = [item.get("properties") for item in v4_branches]
    if any(not isinstance(item, dict) for item in v4_properties):
        fail(f"{capsule_path}: v4 branches must contain closed properties")
    if set(branch_properties[0]) != expected_capsule_keys - {"plugin_requirements"}:
        fail(f"{capsule_path}: v1 properties must be the exact historical projection")
    if set(branch_properties[1]) != expected_capsule_keys:
        fail(f"{capsule_path}: v2 properties must be the exact plugin projection")
    if set(branch_properties[2]) != expected_capsule_keys | {"local_image_paths"}:
        fail(f"{capsule_path}: v3 properties must be the exact local-image projection")
    expected_v4_keys = expected_capsule_keys | {
        "local_image_paths",
        "outcome_kind",
        "integration_mode",
        "runtime_artifact_paths",
        "runtime_artifact_max_files",
        "runtime_artifact_max_bytes",
        "approval_gates",
    }
    if any(set(item) != expected_v4_keys for item in v4_properties):
        fail(f"{capsule_path}: v4 properties must be the exact runtime projection")
    if not isinstance(result_properties, dict) or set(result_properties) != expected_result_keys:
        fail(f"{result_path}: properties must be the exact ModelFacingResult projection")
    if "anyOf" in capsule_schema or "$defs" in capsule_schema:
        fail(f"{capsule_path}: only the bounded v1/v2/v3/v4 oneOf authority is permitted")
    if [item["properties"].get("schema_version") for item in branches[:3]] != [
        {"type": "integer", "const": 1},
        {"type": "integer", "const": 2},
        {"type": "integer", "const": 3},
    ]:
        fail(f"{capsule_path}: schema versions must be the closed integer constants 1, 2 and 3")
    if [item["properties"].get("schema_version") for item in v4_branches] != [
        {"type": "integer", "const": 4},
        {"type": "integer", "const": 4},
    ]:
        fail(f"{capsule_path}: schema-v4 branches must use the integer constant 4")
    if [item["properties"].get("outcome_kind") for item in v4_branches] != [
        {"const": "commit"},
        {"const": "runtime_evidence"},
    ]:
        fail(f"{capsule_path}: schema-v4 outcome branches are invalid")
    if "anyOf" in result_schema or "oneOf" in result_schema or "$defs" in result_schema:
        fail(f"{result_path}: alternate model-facing schema branches are not permitted")
    if result_schema["properties"].get("schema_version") != {"type": "integer", "const": 1}:
        fail(f"{result_path}: schema_version must be the integer constant 1")
    validate_output_schema(capsule_schema)

    capsule = ModelFacingCapsule(
        1,
        "validate the typed projection",
        ("build the capsule", "check the schema"),
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        ("the projection validates", "unknown fields are rejected"),
        ("schemas",),
        ("runtime",),
        (
            ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
            ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
        ),
        "Return one typed projection.",
    )
    result = ModelFacingResult(
        1,
        ModelResultStatus.COMPLETED,
        "typed projection validated",
        ("schemas",),
        (ModelValidation("draft schema", True, "closed"),),
        "completed",
    )
    capsule_projection = capsule.to_json()
    result_projection = result.to_json()
    capsule_validator = Draft202012Validator(capsule_schema)
    result_validator = Draft202012Validator(result_schema)
    if (
        not capsule_validator.is_valid(capsule_projection)
        or ModelFacingCapsule.from_json(capsule_projection).to_json() != capsule_projection
    ):
        fail("capsule typed projection does not round-trip through its schema")
    if (
        not result_validator.is_valid(result_projection)
        or ModelFacingResult.from_json(result_projection).to_json() != result_projection
    ):
        fail("result typed projection does not round-trip through its schema")

    invalid_capsule = {**capsule_projection, "schema_version": "1"}
    if capsule_validator.is_valid(invalid_capsule):
        fail("capsule schema accepts a string schema_version")
    plugin_requirement = {
        "canonical_id": "demo",
        "version": "1",
        "source": "bundled",
        "bundle_digest": "0" * 64,
        "bundled_skill_ids": ["demo.skill"],
        "mcp_connectors": [],
    }
    v2_capsule = {
        **capsule_projection,
        "schema_version": 2,
        "plugin_requirements": [plugin_requirement],
    }
    if not capsule_validator.is_valid(v2_capsule) or ModelFacingCapsule.from_json(v2_capsule).to_json() != v2_capsule:
        fail("capsule schema does not round-trip the compatible plugin-requirement version")
    v1_with_plugins = {
        **capsule_projection,
        "plugin_requirements": [plugin_requirement],
    }
    if capsule_validator.is_valid(v1_with_plugins):
        try:
            ModelFacingCapsule.from_json(v1_with_plugins)
        except ValueError:
            pass
        else:
            fail("schema-v1 capsule unexpectedly implies plugin requirements")
    if capsule_validator.is_valid({**capsule_projection, "schema_version": 2, "plugin_requirements": []}):
        fail("schema-v2 capsule accepts an empty plugin requirement array")
    if capsule_validator.is_valid({**capsule_projection, "schema_version": 2}):
        fail("schema-v2 capsule accepts a missing plugin requirement array")
    validate_structured_output(capsule_projection, capsule_schema)
    validate_structured_output(v2_capsule, capsule_schema)
    invalid_capsule = {**capsule_projection, "program": "legacy-shape"}
    if capsule_validator.is_valid(invalid_capsule):
        fail("capsule schema accepts the discarded legacy branch")
    mismatched_authority = {**capsule_projection, "acceptance": {"visual": "code-reviewer"}}
    if capsule_validator.is_valid(mismatched_authority):
        fail("capsule schema accepts a noncanonical acceptance mapping")
    invalid_result = {**result_projection, "schema_version": "1"}
    if result_validator.is_valid(invalid_result):
        fail("result schema accepts a string schema_version")
    invalid_result = {**result_projection, "findings": []}
    if result_validator.is_valid(invalid_result):
        fail("result schema accepts the discarded legacy findings branch")
    invalid_result = {**result_projection, "next_action": {"action": "continue"}}
    if result_validator.is_valid(invalid_result):
        fail("result schema accepts an object next_action")


def load_partition_manifest() -> dict[str, tuple[str, ...]]:
    try:
        raw = tomllib.loads(PARTITIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"{PARTITIONS_PATH}: invalid TOML: {exc}")
    if raw.get("schema_version") != 1:
        fail(f"{PARTITIONS_PATH}: schema_version must be 1")
    partitions = raw.get("partitions")
    if not isinstance(partitions, dict) or set(partitions) != set(PARTITION_NAMES):
        fail(f"{PARTITIONS_PATH}: partitions must be exactly {list(PARTITION_NAMES)}")
    result: dict[str, tuple[str, ...]] = {}
    for name in PARTITION_NAMES:
        entries = partitions[name]
        if not isinstance(entries, list) or not entries or not all(isinstance(item, str) for item in entries):
            fail(f"{PARTITIONS_PATH}: {name} must contain non-empty test paths")
        result[name] = tuple(entries)
    return result


def repository_test_paths() -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(ROOT)).replace("\\", "/") for path in (ROOT / "tests").rglob("test_*.py")))


def validate_partition_manifest() -> dict[str, tuple[str, ...]]:
    partitions = load_partition_manifest()
    seen: dict[str, str] = {}
    for partition, paths in partitions.items():
        for path in paths:
            if path in seen:
                fail(f"{PARTITIONS_PATH}: duplicate test path {path} in {seen[path]} and {partition}")
            seen[path] = partition
            path_object = Path(path)
            if (
                not (ROOT / path).is_file()
                or not path_object.parts
                or path_object.parts[0] != "tests"
                or not path_object.name.startswith("test_")
                or path_object.suffix != ".py"
            ):
                fail(f"{PARTITIONS_PATH}: stale or invalid test path {path}")
    expected = set(repository_test_paths())
    actual = set(seen)
    if actual != expected:
        missing = sorted(expected - actual)
        stale = sorted(actual - expected)
        fail(f"{PARTITIONS_PATH}: partition union mismatch; missing={missing}, stale={stale}")
    return partitions


def partition_paths(name: str) -> tuple[str, ...]:
    partitions = validate_partition_manifest()
    if name not in partitions:
        fail(f"unknown test partition {name!r}; choose one of {', '.join(PARTITION_NAMES)}")
    return partitions[name]


def _validate_workflow_routing_speeds(workflow_config: object) -> None:
    if not isinstance(workflow_config, dict) or not isinstance(workflow_config.get("roles"), dict):
        fail("workflow.toml.example must define role-class routing")
    for role_name, route in workflow_config["roles"].items():
        if not isinstance(route, dict):
            fail(f"workflow.toml.example roles.{role_name} must be a table")
        model = route.get("model")
        is_luna = model == "gpt-5.6-luna"
        if is_luna and route.get("speed") != "fast":
            fail(f"workflow.toml.example roles.{role_name} must set Luna speed=fast")
        if not is_luna and "speed" in route:
            fail(f"workflow.toml.example roles.{role_name} must not define speed for non-Luna roles")


def validate_workflow_assets() -> None:
    validate_schemas()
    for relative in WORKFLOW_ASSET_PATHS:
        path = ROOT / relative
        if not path.is_file():
            fail(f"workflow asset is missing: {relative}")
    workflow_config = tomllib.loads((ROOT / "config" / "workflow.toml.example").read_text(encoding="utf-8"))
    if not isinstance(workflow_config.get("roles"), dict) or not workflow_config["roles"]:
        fail("workflow.toml.example must define role-class routing")
    _validate_workflow_routing_speeds(workflow_config)
    _validate_authorized_routing_defaults(ROOT / "workflow.toml", ROOT / "config" / "workflow.toml.example")
    validate_partition_manifest()


def _required_repository_file(raw_path: object, *, field: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\0" in raw_path or "\\" in raw_path:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: {field} must be a safe repository-relative path")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: {field} must be a safe repository-relative path")
    path = ROOT.joinpath(*relative.parts)
    try:
        path.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: {field} is unsafe or missing: {raw_path}")
    if not path.is_file():
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: {field} is not a file: {raw_path}")
    return path


def validate_reconciliation_evidence() -> None:
    if not RECONCILIATION_EVIDENCE_PATH.is_file():
        fail("workflow-skill reconciliation evidence is missing")
    try:
        evidence = json.loads(RECONCILIATION_EVIDENCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: invalid JSON: {exc}")
    if not isinstance(evidence, dict):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: evidence must be a closed object")
    required = {
        "schema",
        "status",
        "candidate_sha256",
        "candidate_digest_scope",
        "inventory",
        "new_file_count",
        "partition_counts",
        "wheel",
        "review",
    }
    if set(evidence) != required:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: evidence keys are not the sanitized contract")
    if evidence.get("schema") != "codex-flow/workflow-skill-contracts/v1" or evidence.get("status") != "promoted":
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: unsupported evidence identity or status")
    digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    if (
        not isinstance(evidence.get("candidate_sha256"), str)
        or digest_pattern.fullmatch(evidence["candidate_sha256"]) is None
    ):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: candidate digest must be sha256")
    if (
        not isinstance(evidence.get("candidate_digest_scope"), str)
        or not evidence["candidate_digest_scope"].strip()
        or "\0" in evidence["candidate_digest_scope"]
    ):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: candidate digest scope is required")
    if evidence["candidate_sha256"] != PROMOTED_RECONCILIATION_CANDIDATE_SHA256:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: promoted candidate identity is invalid")
    candidate_digest_scope = evidence["candidate_digest_scope"].encode("utf-8")
    if (
        len(candidate_digest_scope) != PROMOTED_RECONCILIATION_SCOPE_BYTES
        or hashlib.sha256(candidate_digest_scope).hexdigest() != PROMOTED_RECONCILIATION_SCOPE_SHA256
    ):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: promoted candidate digest scope commitment is invalid")
    inventory = evidence.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: digest inventory is empty")
    decisions = {"adopted", "merged", "superseded"}
    inventory_paths: set[tuple[str, str]] = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {
            "capability",
            "source_path",
            "destination_path",
            "source_sha256",
            "destination_sha256",
            "decision",
            "reason",
        }:
            fail(f"{RECONCILIATION_EVIDENCE_PATH}: malformed inventory entry")
        if not isinstance(item["capability"], str) or not item["capability"].strip() or "\0" in item["capability"]:
            fail(f"{RECONCILIATION_EVIDENCE_PATH}: inventory capability is empty")
        if (
            not isinstance(item["decision"], str)
            or item["decision"] not in decisions
            or not isinstance(item["reason"], str)
            or not item["reason"].strip()
            or "\0" in item["reason"]
        ):
            fail(f"{RECONCILIATION_EVIDENCE_PATH}: inventory decision is incomplete")
        for key in ("source_sha256", "destination_sha256"):
            value = item[key]
            if not isinstance(value, str) or digest_pattern.fullmatch(value) is None:
                fail(f"{RECONCILIATION_EVIDENCE_PATH}: {key} must be sha256")
        source_path = _required_repository_file(item["source_path"], field="source_path")
        destination_path = _required_repository_file(item["destination_path"], field="destination_path")
        path_pair = (source_path.relative_to(ROOT).as_posix(), destination_path.relative_to(ROOT).as_posix())
        if path_pair in inventory_paths:
            fail(f"{RECONCILIATION_EVIDENCE_PATH}: duplicate inventory path pair")
        inventory_paths.add(path_pair)
    if len(inventory) != PROMOTED_RECONCILIATION_INVENTORY_COUNT:
        fail(
            f"{RECONCILIATION_EVIDENCE_PATH}: promoted inventory must contain exactly "
            f"{PROMOTED_RECONCILIATION_INVENTORY_COUNT} entries"
        )
    inventory_commitment = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if inventory_commitment != PROMOTED_RECONCILIATION_INVENTORY_SHA256:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: promoted inventory commitment is invalid")
    if isinstance(evidence.get("new_file_count"), bool) or evidence.get("new_file_count") != 22:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: new-file budget evidence must equal 22")
    partition_counts = evidence.get("partition_counts")
    if not isinstance(partition_counts, dict) or set(partition_counts) != set(PARTITION_NAMES):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: partition count keys are incomplete")
    if not all(
        not isinstance(value, bool) and isinstance(value, int) and value > 0 for value in partition_counts.values()
    ):
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: partition counts must be positive integers")
    wheel = evidence.get("wheel")
    if not isinstance(wheel, dict) or set(wheel) != {
        "asset_root",
        "parity",
        "sha256",
        "assets",
        "path",
        "size_bytes",
    }:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: wheel proof is incomplete")
    if wheel.get("asset_root") != "codex_flow/workflow_assets" or wheel.get("parity") is not True:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: wheel proof does not establish exact parity")
    if not isinstance(wheel.get("sha256"), str) or digest_pattern.fullmatch(wheel["sha256"]) is None:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: wheel digest must be sha256")
    if wheel.get("path") != RETAINED_RECONCILIATION_WHEEL.as_posix():
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: retained wheel path is not canonical")
    retained_wheel = ROOT / RETAINED_RECONCILIATION_WHEEL
    if retained_wheel.is_symlink() or not retained_wheel.is_file():
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: retained wheel is missing or indirect")
    size_bytes = wheel.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: retained wheel size is invalid")
    if retained_wheel.stat().st_size != size_bytes:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: retained wheel size is stale")
    if hashlib.sha256(retained_wheel.read_bytes()).hexdigest() != wheel["sha256"]:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: retained wheel digest is stale")
    expected_assets = sorted(
        [f"schemas/{name}.schema.json" for name in SCHEMA_NAMES]
        + ["templates/AGENTS.workflow.md", "config/workflow.toml.example", "config/test-partitions.toml"]
    )
    if wheel.get("assets") != expected_assets:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: wheel asset inventory is incomplete")
    review = evidence.get("review")
    if not isinstance(review, dict) or review != {"p0": 0, "p1": 0}:
        fail(f"{RECONCILIATION_EVIDENCE_PATH}: self-review must report zero P0/P1")


def validate_wheel_asset_layout(wheel_path: Path) -> None:
    """Check a built wheel contains exactly the read-only workflow assets."""

    expected = {f"codex_flow/workflow_assets/schemas/{name}.schema.json" for name in SCHEMA_NAMES} | {
        "codex_flow/workflow_assets/templates/AGENTS.workflow.md",
        "codex_flow/workflow_assets/config/workflow.toml.example",
        "codex_flow/workflow_assets/config/test-partitions.toml",
    }
    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        actual = {name for name in names if name.startswith("codex_flow/workflow_assets/")}
        if actual != expected:
            fail(f"{wheel_path}: workflow asset parity mismatch; expected={sorted(expected)}, actual={sorted(actual)}")
        for name in sorted(expected):
            source = ROOT / name.removeprefix("codex_flow/workflow_assets/")
            if hashlib.sha256(archive.read(name)).hexdigest() != hashlib.sha256(source.read_bytes()).hexdigest():
                fail(f"{wheel_path}: packaged asset differs from repository: {name}")


def main(partition: str | None = None) -> int:
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
    validate_public_semantic_cutover()
    validate_workflow_control_evidence()
    validate_global_agents_template()
    validate_milestone_graph_contract()
    validate_semantic_density_contract()
    validate_git_lane_integration_contract()
    validate_workflow_assets()
    validate_outcome_evidence_priority_contract()
    validate_reconciliation_evidence()
    validate_descriptive_names()
    if partition is not None:
        print("\n".join(partition_paths(partition)))
    else:
        print(f"Validated {len(EXPECTED_SKILLS)} cross-project skills and {len(SCHEMA_NAMES)} workflow schemas.")
    return 0


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="Validate workflow assets and select semantic test partitions")
        parser.add_argument("--partition", choices=PARTITION_NAMES)
        args = parser.parse_args()
        raise SystemExit(main(args.partition))
    except (OSError, ValueError, subprocess.CalledProcessError, py_compile.PyCompileError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
