from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from typer.testing import CliRunner

import codex_flow.cli as cli_module
from codex_flow.backends.codex_sdk import CodexSdkConfig
from codex_flow.config import load_workflow_config
from codex_flow.contracts import (
    ModelAuthority,
    ModelFacingCapsule,
    ModelFacingResult,
    ModelResultStatus,
    model_facing_result_schema,
)
from codex_flow.controller import Controller, capsule_json
from codex_flow.domain import (
    AcceptanceMode,
    ExecutionCapsule,
    ExecutionStatus,
    LifecycleEvent,
    MilestoneId,
    NativePermissionMode,
    ReasoningEffort,
    RoleId,
    RunId,
    ThreadIdentity,
    TurnObservation,
    ValidationSpec,
    WorkspaceMode,
)
from codex_flow.projection import ProjectionError, load_control_capsule, project_model_facing_capsule


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repository(root: Path) -> tuple[str, str]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "codex-flow@example.test")
    _git(root, "config", "user.name", "Codex Flow Projection Test")
    (root / "result.txt").write_text("before\n", encoding="utf-8")
    (root / "protected.txt").write_text("protected\n", encoding="utf-8")
    _git(root, "add", "result.txt", "protected.txt")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD"), _git(root, "branch", "--show-current")


def _model_capsule(*, role: str = "architecture-reviewer") -> ModelFacingCapsule:
    return ModelFacingCapsule(
        1,
        "write one bounded result",
        ("implement the result",),
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        ("result.txt contains the requested value", "protected.txt is unchanged"),
        ("result.txt",),
        ("protected.txt",),
        (
            ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
            ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId(role)),
        ),
        "Write result.txt with exactly `projected\\n` and return the required result JSON.",
    )


class _Adapter:
    def __init__(self, config: CodexSdkConfig, service: dict[str, int]) -> None:
        self.config = config
        self.service = service

    def start_thread(self) -> ThreadIdentity:
        self.service["starts"] += 1
        return ThreadIdentity("projection-thread")

    def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity:
        return thread

    def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
        self.service["turns"] += 1
        assert self.config.cwd is not None
        (self.config.cwd / "result.txt").write_text("projected\n", encoding="utf-8")
        output = {
            "schema_version": 1,
            "status": ModelResultStatus.COMPLETED.value,
            "summary": "projected execution completed",
            "changed_surfaces": ["result.txt"],
            "validations": [{"name": "focused", "passed": True, "evidence": "ok"}],
            "durable_status": "completed",
            "next_action": "none",
        }
        return TurnObservation(
            thread,
            "projection-turn",
            "completed",
            json.dumps(output, sort_keys=True),
            output,
            (
                LifecycleEvent(0, "turn/started", "projection-turn"),
                LifecycleEvent(1, "turn/completed", "projection-turn"),
            ),
        )

    def close(self) -> None:
        self.service["closes"] += 1


def test_model_facing_control_projects_and_reuses_one_durable_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        _repository(root)
        capsule_path = Path(directory) / "model.json"
        capsule_path.write_text(json.dumps(_model_capsule().to_json(), sort_keys=True) + "\n", encoding="utf-8")
        service = {"starts": 0, "turns": 0, "closes": 0}

        def make_controller(state_root: Path) -> Controller:
            return Controller(root, _trusted_test_adapter_factory=lambda config: _Adapter(config, service))

        monkeypatch.setattr(cli_module, "_controller", make_controller)
        runner = CliRunner()
        command = ["control", "--capsule", str(capsule_path), "--state-root", str(root), "--json"]
        first = runner.invoke(cli_module.app, command)
        second = runner.invoke(cli_module.app, command)

        assert first.exit_code == 0, first.stdout
        assert second.exit_code == 0, second.stdout
        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        assert first_payload["status"] == ExecutionStatus.COMPLETED.value
        assert second_payload["status"] == ExecutionStatus.COMPLETED.value
        assert (first_payload["run_id"], first_payload["milestone_id"]) == (
            second_payload["run_id"],
            second_payload["milestone_id"],
        )
        assert service["starts"] == 1
        assert service["turns"] == 1
        assert first_payload["result"]["schema_version"] == 1
        assert first_payload["result"]["status"] == ModelResultStatus.COMPLETED.value
        assert ModelFacingResult.from_json(first_payload["result"]).status is ModelResultStatus.COMPLETED


def test_projection_identity_and_execution_facts_are_stable() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        _repository(root)
        capsule = _model_capsule()
        first = project_model_facing_capsule(capsule, state_root=root)
        second = project_model_facing_capsule(capsule, state_root=root)

        assert first == second
        assert first.workspace_mode is WorkspaceMode.CURRENT_CHECKOUT
        assert first.workspace_path == root.resolve()
        assert first.repository_root == root.resolve()
        assert first.branch == _git(root, "branch", "--show-current")
        assert first.base_sha == _git(root, "rev-parse", "HEAD")
        executor = load_workflow_config(Path(__file__).parents[1] / "workflow.toml").route("executor")
        assert first.model == executor.model
        assert first.reasoning_effort is executor.reasoning_effort
        assert first.permission_mode is NativePermissionMode.INHERIT_NATIVE
        assert first.validation == ValidationSpec(("git", "diff", "--check"), 60.0)
        assert first.output_schema == model_facing_result_schema()


def test_projection_preserves_selected_linked_worktree_topology() -> None:
    with TemporaryDirectory() as directory:
        primary = Path(directory) / "repo"
        base, _branch = _repository(primary)
        linked = Path(directory) / "repo.worktrees" / "linked-lane"
        linked.parent.mkdir()
        _git(primary, "worktree", "add", "-qb", "agent/linked-lane", str(linked), base)

        capsule = project_model_facing_capsule(_model_capsule(), state_root=linked)

        assert capsule.repository_root == primary.resolve()
        assert capsule.workspace_mode is WorkspaceMode.EXISTING_WORKTREE
        assert capsule.workspace_path == linked.resolve()
        assert capsule.branch == "agent/linked-lane"
        assert capsule.base_sha == base
        assert capsule.lane == "linked-lane"
        controller = Controller(linked, _trusted_test_adapter_factory=lambda _config: None)  # type: ignore[arg-type]
        planned = controller.plan(capsule)
        prepared = controller.prepare_app_native(planned.run_id, planned.milestone_id)
        assert prepared.action.workspace_path == linked.resolve()
        lease = controller.ledger.acquire_workspace_lease(capsule)
        assert lease.repository_root == primary.resolve()
        assert lease.mode is WorkspaceMode.EXISTING_WORKTREE
        assert lease.lane == "linked-lane"
        controller.close()


def test_nullable_next_action_matches_parser_schema_and_model_contract() -> None:
    result = ModelFacingResult(1, ModelResultStatus.COMPLETED, "done", (), (), "completed", None)

    assert ModelFacingResult.from_json(result.to_json()) == result
    assert model_facing_result_schema()["properties"]["next_action"] == {"type": ["string", "null"]}


def test_authority_mismatch_and_rejected_shapes_leave_no_controller_state() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        _repository(root)
        bad = Path(directory) / "bad.json"
        bad_payload = _model_capsule().to_json()
        assert isinstance(bad_payload["authorities"], list)
        bad_payload["authorities"][1] = {"mode": "architecture", "role": "visual-reviewer"}
        bad.write_text(json.dumps(bad_payload) + "\n", encoding="utf-8")
        with pytest.raises(ProjectionError, match=r"authority|malformed"):
            load_control_capsule(bad, state_root=root)
        assert not (root / ".codex-flow").exists()
        rejected = CliRunner().invoke(
            cli_module.app,
            ["control", "--capsule", str(bad), "--state-root", str(root), "--json"],
        )
        assert rejected.exit_code == 2
        assert not (root / ".codex-flow").exists()

        malformed = Path(directory) / "malformed.json"
        malformed.write_text(json.dumps({**_model_capsule().to_json(), "extra": True}) + "\n", encoding="utf-8")
        with pytest.raises(ProjectionError, match=r"keys|mixed|ambiguous"):
            load_control_capsule(malformed, state_root=root)
        assert not (root / ".codex-flow").exists()

        mixed = Path(directory) / "mixed.json"
        mixed.write_text(
            json.dumps({**_model_capsule().to_json(), "capsule_version": 2, "run_id": "run"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ProjectionError, match=r"mixed|ambiguous|keys"):
            load_control_capsule(mixed, state_root=root)
        assert not (root / ".codex-flow").exists()


def test_internal_execution_capsule_remains_an_explicit_compatible_input() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        internal = ExecutionCapsule(
            2,
            RunId("internal-run"),
            MilestoneId("internal-milestone"),
            root,
            WorkspaceMode.CURRENT_CHECKOUT,
            root,
            branch,
            base,
            "internal",
            ("result.txt",),
            ("protected.txt",),
            ValidationSpec(("git", "diff", "--check"), 5),
            "gpt-test",
            ReasoningEffort.MEDIUM,
            "internal prompt",
            {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
                "additionalProperties": False,
            },
        )
        input_path = Path(directory) / "internal.json"
        input_path.write_text(json.dumps(capsule_json(internal), sort_keys=True) + "\n", encoding="utf-8")
        loaded, _digest = load_control_capsule(input_path, state_root=root)
        assert loaded == internal
        assert not (root / ".codex-flow").exists()


def test_built_wheel_contains_the_canonical_workflow_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(tmp_path)),
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wheels = sorted(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert archive.read("codex_flow/workflow.toml") == (Path(__file__).parents[1] / "workflow.toml").read_bytes()
