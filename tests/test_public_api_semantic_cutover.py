from __future__ import annotations

import importlib
import re

import pytest
from typer.testing import CliRunner

from codex_flow.cli import app


def test_removed_public_modules_are_not_importable() -> None:
    for module_name in ("h4_pilot", "h5_pilot", "h6_pilot", "sentinel", "controller_sentinel"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"codex_flow.{module_name}")


def test_cli_groups_diagnostics_and_hides_compatibility_aliases() -> None:
    runner = CliRunner()
    root_result = runner.invoke(app, ["--help"])
    assert root_result.exit_code == 0
    help_text = root_result.stdout
    for command in ("tui", "control", "status", "cancel", "supervisor", "live", "schema", "diagnostics"):
        assert command in help_text
    for command in (
        "plan",
        "start",
        "resume",
        "app-bind",
        "app-result",
        "app-status",
        "worker-submit",
        "visible-worker-capsule",
        "controller-decision",
        "schema-compatibility",
        "worker-sentinel",
        "sdk-compatibility-sentinel",
        "controller-recovery-sentinel",
        "live-control-sentinel",
        "review-pilot",
        "multi-authority-review-pilot",
        "workflow-control-pilot",
        "production-pilots",
    ):
        assert re.search(rf"(?m)^│ {re.escape(command)}\s", help_text) is None

    diagnostics = runner.invoke(app, ["diagnostics", "--help"])
    assert diagnostics.exit_code == 0
    for command in (
        "schema-compatibility",
        "worker-sentinel",
        "sdk-compatibility-sentinel",
        "controller-recovery-sentinel",
        "live-control-sentinel",
        "review-pilot",
        "multi-authority-review-pilot",
        "workflow-control-pilot",
        "production-pilots",
    ):
        assert command in diagnostics.stdout

    for alias in (
        "schema-compatibility",
        "worker-sentinel",
        "sdk-compatibility-sentinel",
        "controller-recovery-sentinel",
        "live-control-sentinel",
        "review-pilot",
        "multi-authority-review-pilot",
        "workflow-control-pilot",
        "production-pilots",
    ):
        alias_result = runner.invoke(app, [alias, "--help"])
        assert alias_result.exit_code == 0, alias_result.output

    for command in (
        "plan",
        "start",
        "resume",
        "app-bind",
        "app-result",
        "app-status",
        "worker-submit",
        "visible-worker-capsule",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output

    assert runner.invoke(app, ["controller-decision", "--help"]).exit_code == 0


def test_semantic_public_modules_expose_canonical_entrypoints() -> None:
    review = importlib.import_module("codex_flow.review_pilots")
    workflow = importlib.import_module("codex_flow.workflow_control_pilot")
    production = importlib.import_module("codex_flow.production_pilots")
    sdk = importlib.import_module("codex_flow.sdk_compatibility_sentinel")
    recovery = importlib.import_module("codex_flow.controller_recovery_sentinel")
    assert callable(review.run_review_pilot)
    assert callable(review.run_multi_authority_review_pilot)
    assert callable(workflow.run_workflow_control_pilot)
    assert callable(production.run_production_pilots)
    assert callable(production.build_visible_worker_capsule)
    assert callable(sdk.run_sdk_compatibility_sentinel)
    assert callable(recovery.run_controller_recovery_sentinel)
