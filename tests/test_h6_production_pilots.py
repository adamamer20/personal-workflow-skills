from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_flow.cli import app
from codex_flow.h6_pilot import (
    CompatibilityClassification,
    LegacyRetirementDecision,
    LegacyRetirementDecisionRecord,
    _initialize_repository,
    _pilot_specs,
    _tree_digest,
    make_legacy_decision,
    write_h6_evidence,
)
from codex_flow.ledger import (
    _APP_NATIVE_DISPATCHES_DRAFT_V9_DDL,
    _V8_TABLE_DDL,
    CURRENT_SCHEMA_VERSION,
    Ledger,
    ledger_schema_compatibility,
)


def test_retirement_decision_defaults_to_retention_without_full_parity() -> None:
    decision = make_legacy_decision(pilots_passed=True, required_behavioral_parity_proven=False)

    assert decision.decision is LegacyRetirementDecision.RETAIN_LEGACY
    assert decision.closed is True
    assert decision.production_reachability_proven is True
    assert decision.required_behavioral_parity_proven is False
    assert decision.cleanup_authorized is False


def test_disablement_decision_fails_closed_without_reachability_and_parity() -> None:
    with pytest.raises(ValueError, match="reachability and behavioral parity"):
        LegacyRetirementDecisionRecord(
            1,
            LegacyRetirementDecision.DISABLE_HOOKS_KEEP_MANUAL,
            True,
            True,
            False,
            ("pilot",),
            ("parity-open",),
        )


def test_ready_decision_is_evidence_only_even_after_full_parity() -> None:
    decision = make_legacy_decision(pilots_passed=True, required_behavioral_parity_proven=True)

    assert decision.decision is LegacyRetirementDecision.READY_FOR_SEPARATE_CLEANUP
    assert decision.cleanup_authorized is False


def test_fixed_capsules_cover_medium_and_large_objective_architecture_work() -> None:
    medium, large = _pilot_specs()

    assert medium.scale == "medium"
    assert large.scale == "large"
    assert medium.run_id != large.run_id
    assert medium.expected_changed_paths == ("src/pilot_text/slug.py",)
    assert len(large.expected_changed_paths) == 3
    assert len(medium.prompt.encode("utf-8")) < len(large.prompt.encode("utf-8")) < 12_000


@pytest.mark.parametrize("index", (0, 1))
def test_disposable_pilot_repositories_start_real_and_validation_fails_before_execution(
    tmp_path: Path, index: int
) -> None:
    spec = _pilot_specs()[index]
    root = tmp_path / spec.scale

    base = _initialize_repository(root, spec)
    validation = subprocess.run(spec.validation_argv, cwd=root, text=True, capture_output=True, check=False)

    assert len(base) == 40
    assert validation.returncode != 0
    assert subprocess.run(("git", "status", "--short"), cwd=root, capture_output=True, check=True).stdout == b""


def test_tree_digest_ignores_controller_state_but_observes_repository_outcome(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "source.py").write_text("before\n")
    before = _tree_digest(root)
    (root / ".codex-flow").mkdir()
    (root / ".codex-flow/workflow.db").write_bytes(b"state")

    assert _tree_digest(root) == before
    (root / "source.py").write_text("after\n")
    assert _tree_digest(root) != before


def test_h6_cli_requires_explicit_real_sdk_authority(tmp_path: Path) -> None:
    config = tmp_path / "workflow.toml"
    config.write_text("placeholder\n")
    result = CliRunner().invoke(
        app,
        ["h6-pilot", "--config", str(config), "--output", str(tmp_path / "evidence.json")],
        env={"CODEX_FLOW_REAL_SDK": "0"},
    )

    assert result.exit_code == 2
    assert "refusing real SDK start" in result.stdout


def test_retained_h6_evidence_is_closed_json_and_uses_exact_capability_labels(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    evidence = {
        "status": "passed",
        "compatibility": {
            name: {"status": classification.value}
            for name, classification in {
                "local_sdk": CompatibilityClassification.PROVEN,
                "desktop": CompatibilityClassification.NOT_EXPOSED,
                "idle": CompatibilityClassification.NOT_RUN,
                "remote": CompatibilityClassification.UNSUPPORTED,
            }.items()
        },
    }

    write_h6_evidence(output, evidence)
    assert json.loads(output.read_text()) == evidence
    assert output.read_bytes().endswith(b"\n")


def test_visible_pilot_generator_plans_canonical_capsule_reachable_by_control(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "codex-flow@example.test"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Codex Flow Pilot Test"), cwd=root, check=True)
    (root / "AGENTS.md").write_text("protected\n")
    (root / "workflow.toml").write_text((Path(__file__).parents[1] / "workflow.toml").read_text())
    reviews = root / "docs" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "peer-thread-workflow.md").write_text("protected plan\n")
    (reviews / "codex-controller-compatibility.md").write_text("compatibility\n")
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=root, check=True)
    runner = CliRunner()

    generated = runner.invoke(
        app,
        ["h6-app-pilot-capsule", "--parent-run-id", "parent-run", "--state-root", str(root)],
    )

    assert generated.exit_code == 0, generated.stdout
    capsule_path = Path(generated.stdout.strip())
    assert capsule_path == root / ".codex-flow" / "capsules" / "parent-run" / "h6-c-visible-app-pilot.json"
    assert b"\n  " not in capsule_path.read_bytes()
    prepared = runner.invoke(
        app,
        ["control", "--hosting", "app-native", "--capsule", str(capsule_path), "--state-root", str(root), "--json"],
    )
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.stdout)["state"] == "prepared"


def test_schema_v8_compatibility_probe_is_read_only_before_candidate_migration(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    connection = sqlite3.connect(path)
    for ddl in _V8_TABLE_DDL.values():
        connection.execute(ddl)
    connection.executemany(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", "8"),
            ("migration_marker", "complete"),
            ("schema_identity", "codex_flow_h3_terminal_workspace_v8"),
        ),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    compatibility = ledger_schema_compatibility(path)

    assert compatibility == {
        "candidate_schema_version": int(CURRENT_SCHEMA_VERSION),
        "ledger_schema_version": 8,
        "compatible": True,
        "migration_required": True,
    }
    assert path.read_bytes() == before


def test_empty_source_draft_v9_is_detected_read_only_then_upgraded_by_candidate(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    connection = sqlite3.connect(path)
    for ddl in (*_V8_TABLE_DDL.values(), _APP_NATIVE_DISPATCHES_DRAFT_V9_DDL):
        connection.execute(ddl)
    connection.executemany(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", "9"),
            ("migration_marker", "complete"),
            ("schema_identity", "codex_flow_h6_app_native_v9"),
        ),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    assert ledger_schema_compatibility(path)["migration_required"] is True
    assert path.read_bytes() == before
    ledger = Ledger(path)
    assert ledger_schema_compatibility(path)["migration_required"] is False
    ledger.close()
