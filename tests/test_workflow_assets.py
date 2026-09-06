from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from codex_flow.contracts import (
    ModelAuthority,
    ModelFacingCapsule,
    ModelFacingResult,
    ModelResultStatus,
    ModelValidation,
)
from codex_flow.domain import AcceptanceMode, RoleId

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "plugins" / "personal-workflow-skills" / "skills"
SCHEMAS_ROOT = ROOT / "schemas"

_VALIDATOR_SPEC = importlib.util.spec_from_file_location("workflow_asset_validator", ROOT / "scripts" / "validate.py")
assert _VALIDATOR_SPEC is not None and _VALIDATOR_SPEC.loader is not None
_VALIDATOR = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR)
EXPECTED_SKILLS = _VALIDATOR.EXPECTED_SKILLS
PARTITION_NAMES = _VALIDATOR.PARTITION_NAMES
SCHEMA_NAMES = _VALIDATOR.SCHEMA_NAMES
load_partition_manifest = _VALIDATOR.load_partition_manifest
validate_partition_manifest = _VALIDATOR.validate_partition_manifest
validate_architecture_map_fixture = _VALIDATOR.validate_architecture_map_fixture
validate_milestone_graph_fixture = _VALIDATOR.validate_milestone_graph_fixture
validate_manifest = _VALIDATOR.validate_manifest
validate_outcome_evidence_priority_contract = _VALIDATOR.validate_outcome_evidence_priority_contract
validate_reconciliation_evidence = _VALIDATOR.validate_reconciliation_evidence
validate_workflow_routing_speeds = _VALIDATOR._validate_workflow_routing_speeds


def _schemas() -> list[tuple[str, dict[str, object]]]:
    return [
        (name, json.loads((SCHEMAS_ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))) for name in SCHEMA_NAMES
    ]


def _positive_fixtures() -> dict[str, dict[str, object]]:
    capsule_projection = ModelFacingCapsule(
        1,
        "Reconcile workflow assets.",
        ("Validate contracts.",),
        (AcceptanceMode.OBJECTIVE,),
        ("observable result",),
        ("schemas",),
        ("runtime",),
        (ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),),
        "Use the fixed architecture map.",
    ).to_json()
    result_projection = ModelFacingResult(
        1,
        ModelResultStatus.COMPLETED,
        "Assets validated.",
        ("schemas",),
        (ModelValidation("Draft 2020-12", True, "closed"),),
        "completed",
    ).to_json()
    finding = {
        "schema_version": 1,
        "id": "F-contract-boundary",
        "lens": "correctness",
        "severity": "P1",
        "promotion_blocking": True,
        "promotion_reason": "The named contract is not met.",
        "acceptance_or_guarantee": "The typed boundary remains closed.",
        "location_or_artifact": "schemas/result.schema.json",
        "evidence": ["tests/test_workflow_assets.py"],
        "causal_class": "contract",
        "user_impact_or_dependency_risk": "Dependent work cannot trust the result.",
        "required_action": "Repair the boundary before promotion.",
    }
    return {
        "capsule": capsule_projection,
        "evidence": {
            "schema_version": 1,
            "question": "Does the validator reject stale assets?",
            "conclusion": "Yes, through a closed check.",
            "claims": [
                {
                    "claim": "The check is deterministic.",
                    "evidence": ["scripts/validate.py"],
                    "inference": False,
                    "confidence": "high",
                }
            ],
        },
        "finding": finding,
        "recovery": {
            "schema_version": 1,
            "milestone": "workflow-assets",
            "diagnosis": "near_completion",
            "remaining_work": "small",
            "status": "completed",
            "evidence": ["tests/test_workflow_assets.py"],
            "action": "Run the named validation gates.",
        },
        "result": result_projection,
        "review": {
            "schema_version": "1",
            "milestone": "workflow-assets",
            "fixed_point": "HEAD",
            "lens": "spec",
            "verdict": "accepted",
            "findings": [],
            "evidence_inspected": ["schemas"],
        },
        "visual-contract": {
            "schema_version": 1,
            "name": "workflow-artifact",
            "intent": "Make the artifact easy to inspect.",
            "sentinels": [{"id": "desktop", "description": "Primary view", "render_path": "artifacts/desktop.png"}],
            "acceptance_rubric": ["Hierarchy is observable."],
        },
    }


def test_schemas_are_valid_closed_draft_2020_12_contracts() -> None:
    assert tuple(name for name, _ in _schemas()) == SCHEMA_NAMES
    ids: set[str] = set()
    for name, schema in _schemas():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert isinstance(schema["$id"], str) and schema["$id"] not in ids
        ids.add(schema["$id"])
        Draft202012Validator.check_schema(schema)

        def walk(node: object) -> list[dict[str, object]]:
            if isinstance(node, dict):
                result = [node]
                for child in node.values():
                    result.extend(walk(child))
                return result
            if isinstance(node, list):
                result: list[dict[str, object]] = []
                for child in node:
                    result.extend(walk(child))
                return result
            return []

        assert all(
            node.get("additionalProperties") is False
            or (
                isinstance(node.get("additionalProperties"), dict)
                and isinstance(node.get("propertyNames"), dict)
                and "minProperties" in node
                and "maxProperties" in node
            )
            for node in walk(schema)
            if node.get("type") == "object"
        ), name


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_positive_and_unknown_property_negative(name: str) -> None:
    schema = json.loads((SCHEMAS_ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = _positive_fixtures()[name]
    assert validator.is_valid(valid)
    invalid = {**valid, "unexpected_property": True}
    assert not validator.is_valid(invalid)


def test_typed_controller_projections_validate_closed_schemas() -> None:
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

    capsule_schema = json.loads((SCHEMAS_ROOT / "capsule.schema.json").read_text(encoding="utf-8"))
    result_schema = json.loads((SCHEMAS_ROOT / "result.schema.json").read_text(encoding="utf-8"))
    capsule_validator = Draft202012Validator(capsule_schema)
    result_validator = Draft202012Validator(result_schema)
    capsule_projection = capsule.to_json()
    result_projection = result.to_json()
    assert capsule_validator.is_valid(capsule_projection)
    assert result_validator.is_valid(result_projection)
    assert not capsule_validator.is_valid({**capsule_projection, "unexpected_property": True})
    assert not result_validator.is_valid({**result_projection, "unexpected_property": True})


def test_typed_parsers_and_schemas_reject_alternate_projection_shapes() -> None:
    capsule = _positive_fixtures()["capsule"]
    result = _positive_fixtures()["result"]
    capsule_schema = json.loads((SCHEMAS_ROOT / "capsule.schema.json").read_text(encoding="utf-8"))
    result_schema = json.loads((SCHEMAS_ROOT / "result.schema.json").read_text(encoding="utf-8"))
    capsule_validator = Draft202012Validator(capsule_schema)
    result_validator = Draft202012Validator(result_schema)

    invalid_capsule_values = (
        {**capsule, "program": "legacy"},
        {**capsule, "schema_version": "1"},
        {
            **capsule,
            "authorities": [
                {"mode": "visual", "role": "code-reviewer"},
            ],
        },
    )
    for invalid in invalid_capsule_values:
        assert not capsule_validator.is_valid(invalid)
        with pytest.raises(ValueError):
            ModelFacingCapsule.from_json(invalid)

    invalid_result_values = (
        {**result, "findings": []},
        {**result, "schema_version": "1"},
        {**result, "next_action": {"action": "continue"}},
    )
    for invalid in invalid_result_values:
        assert not result_validator.is_valid(invalid)
        with pytest.raises(ValueError):
            ModelFacingResult.from_json(invalid)


def test_model_projection_adversarial_bounds_match_typed_parsers() -> None:
    capsule = _positive_fixtures()["capsule"]
    result = _positive_fixtures()["result"]
    capsule_validator = Draft202012Validator(
        json.loads((SCHEMAS_ROOT / "capsule.schema.json").read_text(encoding="utf-8"))
    )
    result_validator = Draft202012Validator(
        json.loads((SCHEMAS_ROOT / "result.schema.json").read_text(encoding="utf-8"))
    )

    valid_capsule_edges = (
        {**capsule, "objective": "bounded\n"},
        {**capsule, "decomposition": []},
        {**capsule, "surfaces": {}},
        {**capsule, "acceptance": {"visual": "visual-reviewer"}},
    )
    for value in valid_capsule_edges:
        assert capsule_validator.is_valid(value)
        assert ModelFacingCapsule.from_json(value).to_json() == value

    invalid_capsules = (
        {**capsule, "objective": " \n\t"},
        {**capsule, "objective": "contains\x00nul"},
        {**capsule, "objective": "x" * 16_385},
        {**capsule, "decomposition": ["same", "same"]},
        {**capsule, "decomposition": [f"item-{index}" for index in range(129)]},
        {**capsule, "surfaces": {" ": "mutable"}},
        {**capsule, "surfaces": {f"surface-{index}": "mutable" for index in range(129)}},
        {**capsule, "surfaces": {"src": "unknown"}},
        {**capsule, "acceptance": {}},
        {**capsule, "acceptance": {"objective": "visual-reviewer"}},
        {**capsule, "acceptance": {"unknown": "code-reviewer"}},
        {**capsule, "prompt_budget_bytes": 12_000},
        {**capsule, "mutable_surfaces": ["legacy"]},
    )
    for value in invalid_capsules:
        assert not capsule_validator.is_valid(value)
        with pytest.raises((TypeError, ValueError)):
            ModelFacingCapsule.from_json(value)

    valid_result_edge = {**result, "next_action": "continue\n"}
    assert result_validator.is_valid(valid_result_edge)
    assert ModelFacingResult.from_json(valid_result_edge).to_json() == valid_result_edge

    invalid_results = (
        {**result, "status": "unknown"},
        {**result, "summary": "\x00"},
        {**result, "changed_surfaces": ["same", "same"]},
        {**result, "changed_surfaces": [f"surface-{index}" for index in range(129)]},
        {
            **result,
            "validations": [
                {"name": f"validation-{index}", "passed": True, "evidence": "bounded"} for index in range(129)
            ],
        },
        {**result, "validations": [{"name": "x" * 257, "passed": True, "evidence": "bounded"}]},
        {**result, "validations": [{"name": "bounded", "passed": True, "evidence": "x" * 513}]},
        {**result, "durable_status": "x" * 129},
        {**result, "next_action": " \n"},
    )
    for value in invalid_results:
        assert not result_validator.is_valid(value)
        with pytest.raises((TypeError, ValueError)):
            ModelFacingResult.from_json(value)


def test_plugin_manifest_rejects_normal_legacy_handoff_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_root = tmp_path / "plugin"
    manifest_path = manifest_root / ".codex-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = json.loads(
        (ROOT / "plugins/personal-workflow-skills/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    manifest["interface"]["defaultPrompt"] = "Choose workflow-control or hand off to a peer thread."
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "PLUGIN_ROOT", manifest_root)
    with pytest.raises(ValueError, match="normal \\$workflow-control"):
        validate_manifest()


def test_skill_inventory_metadata_and_nonownership() -> None:
    actual = {path.name for path in SKILLS_ROOT.iterdir() if path.is_dir()}
    assert actual == EXPECTED_SKILLS
    reconciled_skills = {
        "collect-evidence",
        "run-discovery-spike",
        "define-visual-contract",
        "review-work",
        "recover-milestone",
    }
    for skill_name in sorted(reconciled_skills):
        skill_text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = re.match(r"^---\n(.*?)\n---\n", skill_text, re.DOTALL)
        assert frontmatter is not None
        assert re.search(rf"^name:\s*{re.escape(skill_name)}\s*$", frontmatter.group(1), re.MULTILINE)
        assert "description:" in frontmatter.group(1)
        lowered = skill_text.lower()
        assert any(phrase in lowered for phrase in ("do not own", "never own", "controller owns"))


def test_template_and_config_assets_resolve() -> None:
    template = (ROOT / "templates" / "AGENTS.workflow.md").read_text(encoding="utf-8")
    assert "Controller and skill ownership" in template
    config = tomllib.loads((ROOT / "config" / "workflow.toml.example").read_text(encoding="utf-8"))
    assert config["roles"]["execute_substantial"]["model"] == "gpt-5.6-luna"
    assert config["roles"]["execute_bounded"]["thinking"] == "xhigh"
    assert config["roles"]["review_visual"]["thinking"] == "low"
    for _role_name, route in config["roles"].items():
        if route["model"] == "gpt-5.6-luna":
            assert route["speed"] == "fast"
        else:
            assert "speed" not in route
    partitions = tomllib.loads((ROOT / "config" / "test-partitions.toml").read_text(encoding="utf-8"))
    assert set(partitions["partitions"]) == set(PARTITION_NAMES)


def test_default_routes_preserve_role_and_effort_boundaries() -> None:
    from codex_flow.config import load_workflow_config
    from codex_flow.harness import WorkflowHarness

    config = load_workflow_config(ROOT / "workflow.toml")
    for role in ("planner", "recovery"):
        route = config.route(role)
        assert (route.model, route.reasoning_effort.value) == ("gpt-6-astra", "medium")
    for role in ("architecture-reviewer", "decision"):
        route = config.route(role)
        assert (route.model, route.reasoning_effort.value) == ("gpt-5.6-sol", "medium")
    for role in ("executor", "code-reviewer"):
        route = config.route(role)
        assert (route.model, route.reasoning_effort.value) == ("gpt-5.6-luna", "xhigh")
    visual = config.route("visual-reviewer")
    assert (visual.model, visual.reasoning_effort.value) == ("gpt-6-astra", "low")
    assert WorkflowHarness._controller_model_and_effort({}) == ("gpt-5.6-sol", "medium")


@pytest.mark.parametrize(
    ("role", "change", "message"),
    (
        ("execute_substantial", {"speed": "slow"}, "must set Luna speed=fast"),
        ("execute_substantial", {}, "must set Luna speed=fast"),
        ("plan", {"speed": "fast"}, "must not define speed for non-Luna roles"),
    ),
)
def test_workflow_routing_speed_policy_fails_closed(
    role: str,
    change: dict[str, object],
    message: str,
) -> None:
    config = tomllib.loads((ROOT / "config" / "workflow.toml.example").read_text(encoding="utf-8"))
    config["roles"][role].pop("speed", None)
    config["roles"][role].update(change)
    with pytest.raises(ValueError, match=message):
        validate_workflow_routing_speeds(config)


def test_outcome_evidence_priority_contract_is_consistent() -> None:
    assert _VALIDATOR.OUTCOME_EVIDENCE_POLICY_PATHS == (
        SKILLS_ROOT / "plan-work" / "SKILL.md",
        SKILLS_ROOT / "execute-milestone" / "SKILL.md",
        SKILLS_ROOT / "review-work" / "SKILL.md",
        ROOT / "templates" / "AGENTS.md",
    )
    validate_outcome_evidence_priority_contract()


def test_hard_line_cap_requires_proof_preservation_or_replan() -> None:
    obligation_names = (
        "persisted DOCX reopening/recomputation",
        "rendered-page artifact-role binding",
        "rigorously typed/validated HarnessCase corpus/prompt/region contract",
    )
    assert obligation_names == _VALIDATOR._REQUIRED_HARD_CAP_PROOF_OBLIGATIONS
    verified = tuple(_VALIDATOR._ProofObligation(name, True) for name in obligation_names)

    without_replacement = _VALIDATOR._HardCapCase(9_120, 8_900, verified)
    assert _VALIDATOR._decide_hard_cap_case(without_replacement) is _VALIDATOR._OutcomeEvidenceDecision.BOUNDED_REPLAN

    missing_obligation_at_cap = _VALIDATOR._HardCapCase(8_900, 8_900, verified[:-1])
    assert (
        _VALIDATOR._decide_hard_cap_case(missing_obligation_at_cap)
        is _VALIDATOR._OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
    )

    replacement_count_without_replacement_proof = _VALIDATOR._HardCapCase(
        9_120,
        8_900,
        verified,
        replacement_line_count=8_900,
    )
    assert (
        _VALIDATOR._decide_hard_cap_case(replacement_count_without_replacement_proof)
        is _VALIDATOR._OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS
    )

    proof_preserving_replacement = _VALIDATOR._HardCapCase(
        9_120,
        8_900,
        verified,
        replacement_line_count=8_900,
        replacement_proof_obligations=verified,
    )
    assert (
        _VALIDATOR._decide_hard_cap_case(proof_preserving_replacement)
        is _VALIDATOR._OutcomeEvidenceDecision.PROOF_PRESERVING_CONTINUATION
    )
    validate_outcome_evidence_priority_contract()


@pytest.mark.parametrize("proof_defect", ("missing", "reordered", "false"))
def test_hard_line_cap_rejects_defective_current_proof(proof_defect: str) -> None:
    verified = tuple(
        _VALIDATOR._ProofObligation(name, True) for name in _VALIDATOR._REQUIRED_HARD_CAP_PROOF_OBLIGATIONS
    )
    defective = {
        "missing": verified[:-1],
        "reordered": (verified[1], verified[0], verified[2]),
        "false": (*verified[:-1], _VALIDATOR._ProofObligation(verified[-1].name, False)),
    }[proof_defect]
    case = _VALIDATOR._HardCapCase(9_120, 8_900, defective)
    assert _VALIDATOR._decide_hard_cap_case(case) is _VALIDATOR._OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS


@pytest.mark.parametrize("proof_defect", ("missing", "reordered", "false"))
def test_hard_line_cap_rejects_defective_replacement_proof(proof_defect: str) -> None:
    verified = tuple(
        _VALIDATOR._ProofObligation(name, True) for name in _VALIDATOR._REQUIRED_HARD_CAP_PROOF_OBLIGATIONS
    )
    defective = {
        "missing": verified[:-1],
        "reordered": (verified[1], verified[0], verified[2]),
        "false": (*verified[:-1], _VALIDATOR._ProofObligation(verified[-1].name, False)),
    }[proof_defect]
    case = _VALIDATOR._HardCapCase(
        9_120,
        8_900,
        verified,
        replacement_line_count=8_900,
        replacement_proof_obligations=defective,
    )
    assert _VALIDATOR._decide_hard_cap_case(case) is _VALIDATOR._OutcomeEvidenceDecision.PROMOTION_BLOCKING_LOSS


def _reconciliation_evidence() -> dict[str, object]:
    return json.loads(
        (ROOT / "docs" / "reviews" / "evidence" / "workflow-skill-contracts.json").read_text(encoding="utf-8")
    )


def _write_reconciliation_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    evidence: dict[str, object],
) -> None:
    evidence_path = tmp_path / "workflow-skill-contracts.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "RECONCILIATION_EVIDENCE_PATH", evidence_path)


def test_promoted_reconciliation_evidence_allows_legitimate_successor_edits() -> None:
    evidence = _reconciliation_evidence()
    inventory = evidence["inventory"]
    assert isinstance(inventory, list)
    plan_record = next(
        item
        for item in inventory
        if isinstance(item, dict)
        and item.get("destination_path") == "plugins/personal-workflow-skills/skills/plan-work/SKILL.md"
    )
    current_digest = hashlib.sha256((ROOT / str(plan_record["destination_path"])).read_bytes()).hexdigest()
    assert current_digest != plan_record["destination_sha256"]
    validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_deleted_inventory_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    inventory = evidence["inventory"]
    assert isinstance(inventory, list) and len(inventory) == 49
    inventory.pop()
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="must contain exactly 49 entries"):
        validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_valid_digest_substitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    inventory = evidence["inventory"]
    assert isinstance(inventory, list) and isinstance(inventory[0], dict)
    inventory[0]["source_sha256"] = "0" * 64
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="promoted inventory commitment is invalid"):
        validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_valid_candidate_substitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    evidence["candidate_sha256"] = "a" * 64
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="promoted candidate identity is invalid"):
        validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_valid_scope_substitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    scope = evidence["candidate_digest_scope"]
    assert isinstance(scope, str) and len(scope.encode("utf-8")) == 576
    evidence["candidate_digest_scope"] = f"S{scope[1:]}"
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="promoted candidate digest scope commitment is invalid"):
        validate_reconciliation_evidence()


@pytest.mark.parametrize("open_surface", ("record", "inventory"))
def test_promoted_reconciliation_evidence_rejects_open_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    open_surface: str,
) -> None:
    evidence = _reconciliation_evidence()
    if open_surface == "record":
        evidence["unexpected"] = True
    else:
        inventory = evidence["inventory"]
        assert isinstance(inventory, list) and isinstance(inventory[0], dict)
        inventory[0]["unexpected"] = True
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match=r"sanitized contract|malformed inventory"):
        validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_unsupported_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    evidence["status"] = "passed"
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="unsupported evidence identity or status"):
        validate_reconciliation_evidence()


@pytest.mark.parametrize(
    ("digest_location", "bad_digest"),
    (("candidate", "0" * 63), ("source", "G" * 64), ("destination", 7)),
)
def test_promoted_reconciliation_evidence_rejects_malformed_digests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    digest_location: str,
    bad_digest: object,
) -> None:
    evidence = _reconciliation_evidence()
    if digest_location == "candidate":
        evidence["candidate_sha256"] = bad_digest
    else:
        inventory = evidence["inventory"]
        assert isinstance(inventory, list) and isinstance(inventory[0], dict)
        inventory[0][f"{digest_location}_sha256"] = bad_digest
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="must be sha256"):
        validate_reconciliation_evidence()


@pytest.mark.parametrize(
    ("path_field", "bad_path"),
    (
        ("source_path", "../outside.md"),
        ("destination_path", "/tmp/outside.md"),
        ("destination_path", "plugins/personal-workflow-skills/skills/missing/SKILL.md"),
    ),
)
def test_promoted_reconciliation_evidence_rejects_unsafe_or_missing_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path_field: str,
    bad_path: str,
) -> None:
    evidence = _reconciliation_evidence()
    inventory = evidence["inventory"]
    assert isinstance(inventory, list) and isinstance(inventory[0], dict)
    inventory[0][path_field] = bad_path
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match=r"safe repository-relative path|unsafe or missing"):
        validate_reconciliation_evidence()


@pytest.mark.parametrize("wheel_fact", ("sha256", "size_bytes", "assets", "parity"))
def test_promoted_reconciliation_evidence_rejects_wrong_wheel_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wheel_fact: str,
) -> None:
    evidence = _reconciliation_evidence()
    wheel = evidence["wheel"]
    assert isinstance(wheel, dict)
    wrong_values: dict[str, object] = {
        "sha256": "0" * 64,
        "size_bytes": int(wheel["size_bytes"]) + 1,
        "assets": [],
        "parity": False,
    }
    wheel[wheel_fact] = wrong_values[wheel_fact]
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match=r"wheel proof|wheel digest|wheel size|wheel asset"):
        validate_reconciliation_evidence()


def test_promoted_reconciliation_evidence_rejects_missing_retained_wheel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence = _reconciliation_evidence()
    wheel = evidence["wheel"]
    assert isinstance(wheel, dict)
    missing_wheel = Path("dist/missing-retained-wheel.whl")
    wheel["path"] = missing_wheel.as_posix()
    monkeypatch.setattr(_VALIDATOR, "RETAINED_RECONCILIATION_WHEEL", missing_wheel)
    _write_reconciliation_evidence(monkeypatch, tmp_path, evidence)
    with pytest.raises(ValueError, match="retained wheel is missing or indirect"):
        validate_reconciliation_evidence()


def test_partition_manifest_has_exact_one_membership() -> None:
    partitions = validate_partition_manifest()
    assert set(partitions) == set(PARTITION_NAMES)
    assert set().union(*partitions.values()) == {
        str(path.relative_to(ROOT)).replace("\\", "/") for path in (ROOT / "tests").rglob("test_*.py")
    }
    assert len([path for values in partitions.values() for path in values]) == len(
        {path for values in partitions.values() for path in values}
    )
    assert load_partition_manifest() == partitions


def test_partition_manifest_rejects_stale_or_duplicate_entries() -> None:
    partitions = load_partition_manifest()
    all_paths = [path for values in partitions.values() for path in values]
    assert len(all_paths) == len(set(all_paths))
    assert all((ROOT / path).is_file() for path in all_paths)


def test_architecture_map_rejects_executor_invented_topology() -> None:
    fixture = json.loads((ROOT / "tests" / "fixtures" / "prompt-input" / "plan-work.json").read_text(encoding="utf-8"))
    validate_architecture_map_fixture(fixture["architecture_map"])
    invalid = json.loads(json.dumps(fixture["architecture_map"]))
    invalid["paths"][0]["owner"] = "executor"
    with pytest.raises(ValueError, match="delegate topology"):
        validate_architecture_map_fixture(invalid)


def test_milestone_graph_records_ready_disjoint_vertical_lanes() -> None:
    fixture = json.loads((ROOT / "tests" / "fixtures" / "prompt-input" / "plan-work.json").read_text(encoding="utf-8"))
    graph = fixture["architecture_map"]["milestone_graph"]
    validate_milestone_graph_fixture(graph)
    assert graph["current_readiness"]["workflow-asset-lane"] == "ready"
    assert graph["ready_parallel_groups"] == [["workflow-asset-lane", "runtime-proof-lane"]]
    assert {edge["reason"] for edge in graph["serial_edges"]} == {"shared schema", "state authority"}


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda graph: graph["serial_edges"][0].update(reason="implementation convenience"), "serial edge reason"),
        (lambda graph: graph["milestones"][2]["mutable_surfaces"].append("workflow templates"), "disjoint"),
        (lambda graph: graph["milestones"][2].update(owner="workflow-assets-owner"), "disjoint single-owner"),
        (lambda graph: graph.update(nested_controller_default=True), "nested"),
        (lambda graph: graph["milestones"][0]["dependencies"].append("runtime-proof-lane"), "acyclic"),
        (
            lambda graph: (
                graph["milestones"][0].update(readiness="pending"),
                graph["current_readiness"].update({"shared-authority-foundation": "pending"}),
            ),
            "non-completed dependency",
        ),
    ),
)
def test_milestone_graph_rejects_unsafe_fanout_contract(mutation, message: str) -> None:
    fixture = json.loads((ROOT / "tests" / "fixtures" / "prompt-input" / "plan-work.json").read_text(encoding="utf-8"))
    graph = fixture["architecture_map"]["milestone_graph"]
    mutation(graph)
    with pytest.raises(ValueError, match=message):
        validate_milestone_graph_fixture(graph)


def test_planning_templates_enforce_architecture_first_graph_policy() -> None:
    _VALIDATOR.validate_milestone_graph_contract()


def test_workflow_sources_enforce_commit_addressed_lane_integration_policy() -> None:
    _VALIDATOR.validate_git_lane_integration_contract()


def test_validator_rejects_legacy_handoff_as_normal_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    template = (ROOT / "templates" / "AGENTS.md").read_text(encoding="utf-8")
    conflicting = template.replace(
        "the planner does not create a peer or serialize a sidecar",
        "the planner does not create a peer; uses `$codex-thread-handoff` to create a fresh peer execution thread; or serialize a sidecar",
    )
    candidate = tmp_path / "AGENTS.md"
    candidate.write_text(conflicting, encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "GLOBAL_AGENTS_PATH", candidate)
    with pytest.raises(ValueError, match="legacy handoff cannot be the normal execution route"):
        _VALIDATOR.validate_global_agents_template()
