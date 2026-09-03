from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from codex_flow.descriptive_naming import (
    DescriptiveNameError,
    validate_changed_names,
    validate_identifier_name,
    validate_path_name,
)

ROOT = Path(__file__).resolve().parents[1]
_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "workflow_asset_validator_for_naming", ROOT / "scripts" / "validate.py"
)
assert _VALIDATOR_SPEC is not None and _VALIDATOR_SPEC.loader is not None
_VALIDATOR = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR)


@pytest.mark.parametrize(
    "value",
    (
        "tests/test_h6_queue.py",
        "src/codex_flow/s1_worker.py",
        "src/milestone_2.py",
        "src/task-7-runner.py",
        "src/thread_3.py",
        "src/model-4-worker.py",
    ),
)
def test_coupled_paths_are_rejected(value: str) -> None:
    with pytest.raises(DescriptiveNameError):
        validate_path_name(value)


@pytest.mark.parametrize(
    "value", ("has_h6", "has_h6e", "s2_worker", "task_3_result", "thread_4_state", "model_5_route")
)
def test_coupled_identifiers_are_rejected(value: str) -> None:
    with pytest.raises(DescriptiveNameError):
        validate_identifier_name(value)


def test_capability_names_are_accepted() -> None:
    validate_path_name("tests/test_harness_recovery.py")
    validate_path_name("src/codex_flow/model_facing_projection.py")
    validate_identifier_name("has_dispatch_queue")


def test_protocol_and_historical_exceptions_are_explicit() -> None:
    validate_identifier_name("model-42", allow_protocol=True)
    validate_path_name(
        "docs/reviews/evidence/h6-e-detached-supervisor.json",
        immutable_paths=("docs/reviews/evidence/h6-e-detached-supervisor.json",),
    )
    validate_path_name(
        ".codex-flow/runs/model-42/milestones/h6.json",
        protocol_paths=(".codex-flow/runs/model-42/milestones/h6.json",),
    )
    assert validate_changed_names(
        ("docs/reviews/evidence/h6-e-detached-supervisor.json", "src/codex_flow/ledger.py"),
        ("model-42", "has_h6"),
        immutable_paths=("docs/reviews/evidence/h6-e-detached-supervisor.json",),
        protocol_names=("model-42",),
    ) == ("has_h6: temporary milestone/task/thread/model sequence coupling",)


def test_evidence_exceptions_are_exact_not_directory_wide() -> None:
    accepted = "docs/reviews/evidence/h6-e-detached-supervisor.json"
    mutable = "docs/reviews/evidence/task-7-result.json"
    assert validate_changed_names((accepted,), immutable_paths=(accepted,)) == ()
    assert validate_changed_names((mutable,), immutable_paths=(accepted,))


def test_changed_name_validation_reports_each_bad_surface() -> None:
    violations = validate_changed_names(("src/h3_worker.py",), ("task_9_state",))
    assert len(violations) == 2


@pytest.mark.parametrize(
    "value",
    ("WorkflowClass_h6", "build_task_7", "test_thread_3_result", "fixture_model_4"),
)
def test_identifier_bypass_adversaries_are_rejected(value: str) -> None:
    with pytest.raises(DescriptiveNameError):
        validate_identifier_name(value)


def _write_structured_fixture(root: Path, relative: str, value: object) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_top_level_gate_rejects_structured_identifier_bypasses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/prompt-input/plan-work.json").read_text(encoding="utf-8"))
    fixture["architecture_map"]["primary_identifiers"][0] = "capsule_h6_projection"
    _write_structured_fixture(tmp_path, "tests/fixtures/prompt-input/plan-work.json", fixture)
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="primary_identifiers"):
        _VALIDATOR._validate_structured_content(("tests/fixtures/prompt-input/plan-work.json",))


@pytest.mark.parametrize(
    ("relative", "mutate", "message"),
    (
        (
            "schemas/example.schema.json",
            lambda value: value.update(title="Milestone_3_schema"),
            "schema title",
        ),
        (
            "docs/reviews/evidence/workflow-skill-contracts.json",
            lambda value: value.update(milestone_3_result={"summary": "h6"}),
            "mutable evidence key",
        ),
        (
            "docs/reviews/evidence/workflow-skill-contracts.json",
            lambda value: value.update(generated_filename="result_task_2.json"),
            "generated_filename",
        ),
    ),
)
def test_top_level_gate_rejects_schema_evidence_and_filename_bypasses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative: str,
    mutate,
    message: str,
) -> None:
    if relative.startswith("schemas/"):
        value: object = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://personal-workflow-skills.invalid/schemas/example.schema.json",
            "title": "Example schema",
            "type": "object",
            "additionalProperties": False,
        }
    else:
        value = {"summary": "Historical h6 text is prose, not an identifier."}
    assert isinstance(value, dict)
    mutate(value)
    _write_structured_fixture(tmp_path, relative, value)
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)
    with pytest.raises(ValueError, match=message):
        _VALIDATOR._validate_structured_content((relative,))


def test_structured_gate_does_not_scan_arbitrary_prose(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    value = {"summary": "This explanatory text mentions h6_task_9 but is not a durable identifier."}
    relative = "docs/reviews/evidence/workflow-skill-contracts.json"
    _write_structured_fixture(tmp_path, relative, value)
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)
    _VALIDATOR._validate_structured_content((relative,))


def test_changed_python_ast_is_scanned_without_a_filename_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    relative = "src/codex_flow/arbitrary_module.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("def build_task_7():\n    return None\n", encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="Python identifier"):
        _VALIDATOR._validate_structured_content((relative,))


def test_top_level_gate_scans_the_complete_git_changed_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    relative = "src/codex_flow/unlisted_module.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("def build_task_7():\n    return None\n", encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)
    monkeypatch.setattr(_VALIDATOR, "changed_paths_from_git", lambda _root: (relative,))

    with pytest.raises(ValueError, match="Python identifier"):
        _VALIDATOR.validate_descriptive_names()


def test_changed_python_variables_are_name_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    relative = "src/codex_flow/arbitrary_module.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("task_7_state = 1\n", encoding="utf-8")
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="task_7_state"):
        _VALIDATOR._validate_structured_content((relative,))


def test_python_protocol_identifier_exceptions_are_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    relative = "src/codex_flow/arbitrary_module.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        "def _encode_h4_event_data():\n    return None\n\ndef _encode_h4_other_data():\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="_encode_h4_other_data"):
        _VALIDATOR._validate_structured_content((relative,))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda graph: graph["current_readiness"].update({"task-7": "ready"}),
        lambda graph: graph["serial_edges"][0].update({"from": "thread-4"}),
        lambda graph: graph["ready_parallel_groups"][0].append("model-3"),
        lambda graph: graph["critical_path"].append("milestone-2"),
        lambda graph: graph.update({"fan_out_after": "s2"}),
    ),
)
def test_every_graph_identity_projection_is_name_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation
) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/prompt-input/plan-work.json").read_text(encoding="utf-8"))
    mutation(fixture["architecture_map"]["milestone_graph"])
    relative = "tests/fixtures/prompt-input/plan-work.json"
    _write_structured_fixture(tmp_path, relative, fixture)
    monkeypatch.setattr(_VALIDATOR, "ROOT", tmp_path)

    with pytest.raises(ValueError):
        _VALIDATOR._validate_structured_content((relative,))
