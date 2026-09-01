from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from typer.testing import CliRunner

from codex_flow.app_native import AppNativeState, AppNativeTaskAction, HostIdentity, HostReceipt
from codex_flow.cli import app
from codex_flow.contracts import (
    ModelFacingResult,
    ModelResultStatus,
    ModelValidation,
    format_model_facing_result_prompt,
    model_facing_result_schema,
)
from codex_flow.controller import Controller, ControllerError, capsule_json
from codex_flow.domain import (
    AcceptanceMode,
    DispatchId,
    ExecutionCapsule,
    ExecutionStatus,
    MilestoneId,
    ReasoningEffort,
    RunId,
    ThreadIdentity,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
)
from codex_flow.ledger import CorruptSchemaError, StaleWriter


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _repository(root: Path) -> tuple[str, str]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "codex-flow@example.test")
    _git(root, "config", "user.name", "Codex Flow App Test")
    (root / "result.txt").write_text("before\n", encoding="utf-8")
    (root / "protected.txt").write_text("protected\n", encoding="utf-8")
    _git(root, "add", "result.txt", "protected.txt")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD"), _git(root, "branch", "--show-current")


def _capsule(root: Path, base: str, branch: str) -> ExecutionCapsule:
    return ExecutionCapsule(
        2,
        RunId("app-run"),
        MilestoneId("app-milestone"),
        root,
        WorkspaceMode.CURRENT_CHECKOUT,
        root,
        branch,
        base,
        "app-native",
        ("result.txt",),
        ("protected.txt",),
        ValidationSpec(("git", "diff", "--check"), 10),
        "gpt-5.6-sol",
        ReasoningEffort.HIGH,
        "Write result.txt and return exactly one ModelFacingResult.",
        model_facing_result_schema(),
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )


def _result() -> ModelFacingResult:
    return ModelFacingResult(
        1,
        ModelResultStatus.COMPLETED,
        "visible App worker completed",
        ("result.txt",),
        (ModelValidation("focused", True, "result.txt updated"),),
        "completed",
        "planning owner may inspect the visible thread",
    )


def _controller(root: Path, calls: dict[str, int]) -> Controller:
    def forbidden_adapter(_config: object) -> object:
        calls["sdk"] += 1
        raise AssertionError("App-native prepare must not construct an SDK adapter")

    return Controller(root, _trusted_test_adapter_factory=forbidden_adapter)  # type: ignore[arg-type]


def test_prepare_is_sdk_free_closed_and_idempotent() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        calls = {"sdk": 0}
        controller = _controller(root, calls)
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)

        first = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        second = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)

        assert first == second
        assert calls == {"sdk": 0}
        assert first.state is AppNativeState.PREPARED
        assert first.action.model == capsule.model
        assert first.action.reasoning_effort is capsule.reasoning_effort
        assert first.action.workspace_path == root.resolve()
        assert first.action.prompt == format_model_facing_result_prompt(capsule.prompt)
        assert first.action.output_schema == model_facing_result_schema()
        assert first.action.non_blocking is True
        assert controller.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.STARTING
        assert len(controller.ledger.snapshot(capsule.run_id).dispatches) == 1
        controller.close()


def test_bind_is_exact_idempotent_and_rejects_invented_identity() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        receipt = HostReceipt.from_native_result(prepared.action, identity)

        bound = controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=receipt,
        )
        repeated = controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=receipt,
        )

        assert bound == repeated
        assert bound.state is AppNativeState.BOUND
        assert bound.identity == identity
        assert controller.status(capsule.run_id, capsule.milestone_id).status is ExecutionStatus.THREAD_STARTED
        with pytest.raises(StaleWriter, match="capability"):
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token="0" * 64,
                receipt=receipt,
            )
        with pytest.raises(StaleWriter, match="different host identity"):
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=HostReceipt.from_native_result(
                    prepared.action, HostIdentity("invented-host", ThreadIdentity("invented-thread"))
                ),
            )
        controller.close()


def test_terminal_result_is_exact_durable_and_rejects_conflicts() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=HostReceipt.from_native_result(prepared.action, identity),
        )
        (root / "result.txt").write_text("from visible app\n", encoding="utf-8")

        terminal = controller.complete_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            identity=identity,
            result=_result(),
        )
        repeated = controller.complete_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            identity=identity,
            result=_result(),
        )

        assert terminal == repeated
        assert terminal.state is AppNativeState.COMPLETED
        assert terminal.result == _result()
        assert terminal.recovery == "terminal"
        execution = controller.status(capsule.run_id, capsule.milestone_id)
        assert execution.status is ExecutionStatus.COMPLETED
        assert execution.result == _result().to_json()
        assert execution.validation is not None
        assert execution.protected_after_sha256 is not None
        assert terminal.git_authority_after_sha256 is not None
        with pytest.raises(StaleWriter, match="different terminal facts"):
            controller.ledger.record_app_native_result(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                identity=identity,
                result=_result(),
                validation=replace(
                    execution.validation,
                    duration_seconds=execution.validation.duration_seconds + 1,
                ),
                protected_after_sha256=execution.protected_after_sha256,
                git_authority_after_sha256=terminal.git_authority_after_sha256,
                workspace_terminal_head_sha=terminal.workspace_terminal_head_sha,
                workspace_terminal=terminal.workspace_terminal,
            )
        controller.close()

        reopened = _controller(root, {"sdk": 0})
        assert reopened.app_native_status(prepared.action.dispatch_id) == terminal
        conflicting = ModelFacingResult(
            1,
            ModelResultStatus.FAILED,
            "conflict",
            (),
            (ModelValidation("focused", False, "conflict"),),
            "failed",
            "none",
        )
        with pytest.raises(ControllerError, match="different terminal result"):
            reopened.complete_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                identity=identity,
                result=conflicting,
            )
        reopened.close()


def test_result_rejects_wrong_host_and_malformed_model_result() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=HostReceipt.from_native_result(prepared.action, identity),
        )
        with pytest.raises(ControllerError, match="bound host identity"):
            controller.complete_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                identity=HostIdentity("other-host", identity.thread_id),
                result=_result(),
            )
        malformed = _result().to_json()
        malformed["invented"] = True
        with pytest.raises(ValueError, match="keys must be exactly"):
            ModelFacingResult.from_json(malformed)
        controller.close()


def test_protected_out_of_scope_or_controller_state_mutation_fails_terminal_integrity() -> None:
    for changed_path in ("protected.txt", "outside.txt", ".codex-flow/invented.txt"):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            base, branch = _repository(root)
            controller = _controller(root, {"sdk": 0})
            capsule = _capsule(root, base, branch)
            controller.plan(capsule)
            prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
            identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=HostReceipt.from_native_result(prepared.action, identity),
            )
            (root / changed_path).write_text("unauthorized\n", encoding="utf-8")

            terminal = controller.complete_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                identity=identity,
                result=_result(),
            )

            assert terminal.state is AppNativeState.FAILED
            execution = controller.status(capsule.run_id, capsule.milestone_id)
            assert execution.status is ExecutionStatus.FAILED
            assert execution.validation is not None
            assert execution.validation.error_code is not None
            controller.close()


def test_action_json_is_closed() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        action = controller.prepare_app_native(capsule.run_id, capsule.milestone_id).action
        payload = action.to_json()
        payload["extra"] = True
        with pytest.raises(ValueError, match="keys must be exactly"):
            type(action).from_json(json.loads(json.dumps(payload)))
        controller.close()


def test_first_bind_requires_exact_host_receipt_and_rejects_mismatch() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        receipt = HostReceipt.from_native_result(prepared.action, identity)

        with pytest.raises(StaleWriter, match="proof"):
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=replace(receipt, proof="0" * 64),
            )
        with pytest.raises(StaleWriter, match="prepared action"):
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=replace(receipt, bind_challenge="1" * 64),
            )
        assert controller.app_native_status(prepared.action.dispatch_id).state is AppNativeState.PREPARED
        controller.close()


@pytest.mark.parametrize("bind_first", (False, True))
def test_cancel_atomically_terminalizes_app_dispatch_and_rejects_late_callbacks(bind_first: bool) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        receipt = HostReceipt.from_native_result(prepared.action, identity)
        if bind_first:
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=receipt,
            )

        cancelled = controller.cancel(capsule.run_id, capsule.milestone_id)
        app_status = controller.app_native_status(prepared.action.dispatch_id)

        assert cancelled.status is ExecutionStatus.CANCELLED
        assert app_status.state is AppNativeState.CANCELLED
        assert app_status.recovery == "terminal"
        with pytest.raises(StaleWriter, match="cancelled"):
            controller.bind_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                receipt=receipt,
            )
        with pytest.raises((ControllerError, StaleWriter), match=r"bound|terminal|cancel"):
            controller.complete_app_native(
                prepared.action.dispatch_id,
                claim_token=prepared.action.claim_token,
                identity=identity,
                result=_result(),
            )
        controller.close()
        reopened = _controller(root, {"sdk": 0})
        assert reopened.app_native_status(prepared.action.dispatch_id).state is AppNativeState.CANCELLED
        reopened.close()


def test_terminal_schema_reopen_rejects_app_execution_thread_corruption() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        controller = _controller(root, {"sdk": 0})
        capsule = _capsule(root, base, branch)
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=HostReceipt.from_native_result(prepared.action, identity),
        )
        (root / "result.txt").write_text("done\n", encoding="utf-8")
        controller.complete_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            identity=identity,
            result=_result(),
        )
        controller.close()

        connection = sqlite3.connect(root / ".codex-flow" / "workflow.db")
        connection.execute(
            "UPDATE executions SET thread_id = 'counterfeit-thread' WHERE run_id = ? AND milestone_id = ?",
            (str(capsule.run_id), str(capsule.milestone_id)),
        )
        connection.commit()
        connection.close()
        with pytest.raises(CorruptSchemaError, match="host/thread identity"):
            _controller(root, {"sdk": 0})


def test_thread_identity_is_bounded_before_sqlite_boundary() -> None:
    with pytest.raises(ValueError, match="bounded"):
        ThreadIdentity("x" * 513)
    with pytest.raises(ValueError, match="bounded"):
        HostIdentity("local-app", "x" * 513)  # type: ignore[arg-type]


def test_ignored_mutable_file_directory_and_symlink_finish_durably() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        (root / ".git" / "info" / "exclude").write_text("ignored-file\nignored-dir/\nignored-link\n")
        (root / "ignored-file").write_text("build\n")
        (root / "ignored-dir").mkdir()
        (root / "ignored-dir" / "cache").write_text("cache\n")
        (root / "ignored-link").symlink_to(Path(directory) / "outside")
        capsule = replace(
            _capsule(root, base, branch),
            mutable_paths=("ignored-file", "ignored-dir", "ignored-link"),
        )
        controller = _controller(root, {"sdk": 0})
        controller.plan(capsule)
        prepared = controller.prepare_app_native(capsule.run_id, capsule.milestone_id)
        identity = HostIdentity("local-app", ThreadIdentity("visible-thread"))
        controller.bind_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            receipt=HostReceipt.from_native_result(prepared.action, identity),
        )
        terminal = controller.complete_app_native(
            prepared.action.dispatch_id,
            claim_token=prepared.action.claim_token,
            identity=identity,
            result=replace(_result(), next_action=None),
        )
        assert terminal.state is AppNativeState.COMPLETED
        assert terminal.workspace_terminal == ()
        controller.close()
        reopened = _controller(root, {"sdk": 0})
        assert reopened.status(capsule.run_id, capsule.milestone_id).status is ExecutionStatus.COMPLETED
        reopened.close()


def test_prepare_cross_checks_action_workspace_against_execution_and_lease() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        other = Path(directory) / "other"
        base, branch = _repository(root)
        _repository(other)
        capsule = _capsule(root, base, branch)
        controller = _controller(root, {"sdk": 0})
        controller.plan(capsule)
        controller.ledger.acquire_workspace_lease(capsule)
        action = AppNativeTaskAction(
            1,
            DispatchId.from_parts(capsule.run_id, capsule.milestone_id, "executor", 1),
            "a" * 64,
            "b" * 64,
            capsule.model,
            capsule.reasoning_effort,
            other,
            capsule.prompt,
            capsule.output_schema,
        )

        with pytest.raises(StaleWriter, match="workspace does not match"):
            controller.ledger.prepare_app_native_dispatch(
                capsule.run_id,
                capsule.milestone_id,
                action,
                workspace_baseline_head_sha=base,
                workspace_baseline=(),
                git_authority_before_sha256="c" * 64,
                controller_state_sha256="d" * 64,
            )
        controller.close()


def test_cli_routes_app_mode_through_prepare_bind_result_and_status() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        base, branch = _repository(root)
        capsule = _capsule(root, base, branch)
        capsule_path = Path(directory) / "capsule.json"
        capsule_path.write_text(json.dumps(capsule_json(capsule), sort_keys=True) + "\n", encoding="utf-8")
        runner = CliRunner()

        prepared = runner.invoke(
            app,
            [
                "control",
                "--hosting",
                "app-native",
                "--capsule",
                str(capsule_path),
                "--state-root",
                str(root),
                "--json",
            ],
        )
        assert prepared.exit_code == 0, prepared.stdout
        prepared_json = json.loads(prepared.stdout)
        action = prepared_json["action"]
        typed_action = AppNativeTaskAction.from_json(action)
        receipt_path = Path(directory) / "receipt.json"
        receipt_path.write_text(
            json.dumps(
                HostReceipt.from_native_result(
                    typed_action, HostIdentity("local-app", ThreadIdentity("visible-cli-thread"))
                ).to_json(),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        bind_common = [
            "--dispatch-id",
            action["dispatch_id"],
            "--claim-token",
            action["claim_token"],
            "--receipt",
            str(receipt_path),
            "--state-root",
            str(root),
            "--json",
        ]
        result_common = [
            "--dispatch-id",
            action["dispatch_id"],
            "--claim-token",
            action["claim_token"],
            "--thread-id",
            "visible-cli-thread",
            "--host-id",
            "local-app",
            "--state-root",
            str(root),
            "--json",
        ]
        bound = runner.invoke(app, ["app-bind", *bind_common])
        assert bound.exit_code == 0, bound.stdout
        assert json.loads(bound.stdout)["state"] == "bound"

        (root / "result.txt").write_text("from visible CLI task\n", encoding="utf-8")
        result_path = Path(directory) / "result.json"
        result_path.write_text(json.dumps(_result().to_json(), sort_keys=True) + "\n", encoding="utf-8")
        ingested = runner.invoke(app, ["app-result", *result_common, "--result", str(result_path)])
        assert ingested.exit_code == 0, ingested.stdout
        assert json.loads(ingested.stdout)["state"] == "completed"

        status = runner.invoke(
            app,
            [
                "app-status",
                "--dispatch-id",
                action["dispatch_id"],
                "--state-root",
                str(root),
                "--json",
            ],
        )
        assert status.exit_code == 0, status.stdout
        assert json.loads(status.stdout)["recovery"] == "terminal"
