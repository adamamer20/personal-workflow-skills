from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_flow.cli import app
from codex_flow.contracts import (
    ModelAuthority,
    ModelFacingCapsule,
    ModelFacingResult,
    ModelResultStatus,
    ModelValidation,
    model_facing_capsule_schema,
    model_facing_result_schema,
)
from codex_flow.domain import AcceptanceMode, RoleId, validate_output_schema

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "prompt-input"


def test_model_facing_capsule_and_result_are_typed_round_trips() -> None:
    capsule = ModelFacingCapsule(
        1,
        "package the controller path",
        ("define contract", "wire entrypoint"),
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        ("CLI reaches controller", "protected surfaces stay unchanged"),
        ("src/codex_flow",),
        ("plugins/personal-workflow-skills/skills/codex-thread-handoff",),
        (
            ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
            ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
        ),
        "Use the controller and return one durable result.",
    )
    assert ModelFacingCapsule.from_json(capsule.to_json()) == capsule
    result = ModelFacingResult(
        1,
        ModelResultStatus.COMPLETED,
        "controller path reached",
        ("src/codex_flow/contracts.py",),
        (ModelValidation("focused tests", True, "digest:abc"),),
        "completed",
    )
    assert ModelFacingResult.from_json(result.to_json()) == result


def test_model_facing_capsule_rejects_non_completion_recovery() -> None:
    try:
        ModelFacingCapsule(
            1,
            "bounded objective",
            ("implement",),
            (AcceptanceMode.OBJECTIVE,),
            ("tests pass",),
            ("src",),
            ("protected",),
            (ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),),
            "return one result",
            recovery_policy="retry_forever",
        )
    except ValueError as exc:
        assert "completion_biased" in str(exc)
    else:  # pragma: no cover - assertion captures contract drift
        raise AssertionError("non-completion recovery policy must be rejected")


def test_model_facing_schemas_are_closed_and_validated() -> None:
    validate_output_schema(model_facing_capsule_schema())
    validate_output_schema(model_facing_result_schema())


def test_cli_exposes_controller_control_and_model_schemas() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "control" in help_result.stdout
    assert "schema" in help_result.stdout
    schema_result = runner.invoke(app, ["schema", "--kind", "all"])
    assert schema_result.exit_code == 0
    payload = json.loads(schema_result.stdout)
    assert set(payload) == {"capsule", "result"}


def test_prompt_fixtures_keep_one_intended_skill_and_budget() -> None:
    for skill in ("plan-work", "execute-milestone", "workflow-control"):
        fixture = json.loads((FIXTURES / f"{skill}.json").read_text(encoding="utf-8"))
        assert fixture["input_text"].count(f"${skill}") == 1
        skill_text = (ROOT / "plugins/personal-workflow-skills/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert len(skill_text.encode()) <= json.loads((FIXTURES / "budget.json").read_text())["budget_bytes"][skill]


def test_explicit_legacy_route_remains_reachable_without_mixing() -> None:
    handoff = (ROOT / "plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md").read_text(
        encoding="utf-8"
    )
    control = (ROOT / "plugins/personal-workflow-skills/skills/workflow-control/SKILL.md").read_text(encoding="utf-8")
    assert "Explicit legacy route" in handoff
    assert "$codex-thread-handoff" in handoff
    assert "never invoke both routes" in handoff
    assert "codex-flow control" in control
