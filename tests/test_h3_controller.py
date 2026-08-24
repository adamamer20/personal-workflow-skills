from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from typer.testing import CliRunner

from codex_flow.backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from codex_flow.cli import app
from codex_flow.controller import Controller, ResumeRequired, UncertainPreIdentity, capsule_json
from codex_flow.domain import (
    ControllerCheckpoint,
    ExecutionCapsule,
    ExecutionStatus,
    LifecycleEvent,
    MilestoneId,
    ReasoningEffort,
    RunId,
    Sandbox,
    ThreadIdentity,
    TurnObservation,
    ValidationSpec,
    WorkspaceMode,
)
from codex_flow.ledger import _V2_TABLE_DDL, CURRENT_SCHEMA_VERSION, Ledger
from codex_flow.worktrees import WorkspaceConflict


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=path, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _repository(root: Path) -> tuple[str, str]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "codex-flow@example.test")
    _git(root, "config", "user.name", "Codex Flow Test")
    (root / "protected.txt").write_text("protected\n")
    (root / "source.txt").write_text("before\n")
    _git(root, "add", "protected.txt", "source.txt")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD"), _git(root, "branch", "--show-current")


def _capsule(
    repository: Path,
    workspace: Path,
    base: str,
    branch: str,
    *,
    run: str = "run",
    milestone: str = "m1",
    mode: WorkspaceMode = WorkspaceMode.CURRENT_CHECKOUT,
    lane: str = "program",
) -> ExecutionCapsule:
    return ExecutionCapsule(
        1,
        RunId(run),
        MilestoneId(milestone),
        repository,
        mode,
        workspace,
        branch,
        base,
        lane,
        ("result.txt",),
        ("protected.txt",),
        ValidationSpec(("git", "diff", "--check"), 5),
        "gpt-test",
        ReasoningEffort.MEDIUM,
        "write result.txt and return the required JSON",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )


class FakeAdapter:
    def __init__(self, config: CodexSdkConfig, service: dict[str, Any]) -> None:
        self.config = config
        self.service = service

    def start_thread(self) -> ThreadIdentity:
        self.service["starts"] += 1
        return ThreadIdentity("thread-stable")

    def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity:
        self.service["resumes"] += 1
        return thread

    def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
        self.service["turns"] += 1
        assert self.config.cwd is not None
        (self.config.cwd / "result.txt").write_text("edited by sdk\n")
        return TurnObservation(
            thread,
            "turn-stable",
            "completed",
            json.dumps({"status": "done"}),
            {"status": "done"},
            (
                LifecycleEvent(0, "turn/started", "turn-stable"),
                LifecycleEvent(1, "turn/completed", "turn-stable"),
            ),
        )

    def close(self) -> None:
        self.service["closes"] += 1


def _factory(service: dict[str, Any]):
    return lambda config: FakeAdapter(config, service)


def _service() -> dict[str, int]:
    return {"starts": 0, "resumes": 0, "turns": 0, "closes": 0}


def test_current_checkout_start_persists_result_before_projection() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        projection_seen: list[ExecutionStatus] = []

        controller: Controller

        def fault(stage: str) -> None:
            if stage == "before_projection":
                projection_seen.append(controller.status("run", "m1").status)

        controller = Controller(repository, adapter_factory=_factory(service), fault_injector=fault)
        capsule = _capsule(repository, repository, base, branch)
        planned = controller.plan(capsule)
        assert planned.status is ExecutionStatus.PLANNED

        terminal = controller.start("run", "m1")

        assert terminal.status is ExecutionStatus.COMPLETED
        assert terminal.checkpoint is ControllerCheckpoint.RESULT_DURABLE
        assert terminal.result == {"status": "done"}
        assert terminal.validation is not None and terminal.validation.exit_code == 0
        assert projection_seen == [ExecutionStatus.COMPLETED]
        assert service == {"starts": 1, "resumes": 0, "turns": 1, "closes": 1}
        assert len(controller.ledger.sdk_lifecycle_events("run", "m1")) == 2
        assert (repository / ".codex-flow/runs/run/execution.json").is_file()
        controller.close()


def test_post_identity_crash_requires_fresh_process_resume_without_duplicates() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected crash")

        first = Controller(repository, adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        durable = first.status("run", "m1")
        assert durable.thread_id == ThreadIdentity("thread-stable")
        assert durable.turn_id is None
        with pytest.raises(ResumeRequired):
            first.start("run", "m1")
        first.close()

        second = Controller(repository, adapter_factory=_factory(service))
        terminal = second.resume("run", "m1")
        assert terminal.status is ExecutionStatus.COMPLETED
        assert service["starts"] == 1
        assert service["resumes"] == 1
        assert service["turns"] == 1
        assert len(second.ledger.snapshot("run").dispatches) == 1
        assert second.ledger.get_workspace_lease(repository).owner_run_id == RunId("run")
        second.close()


def test_projection_failure_cannot_undo_terminal_result() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "before_projection":
                raise RuntimeError("projection failed")

        first = Controller(repository, adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="projection failed"):
            first.start("run", "m1")
        assert first.status("run", "m1").status is ExecutionStatus.COMPLETED
        first.close()

        second = Controller(repository, adapter_factory=_factory(service))
        terminal = second.resume("run", "m1")
        assert terminal.status is ExecutionStatus.COMPLETED
        assert service["starts"] == 1 and service["turns"] == 1 and service["resumes"] == 0
        assert (repository / ".codex-flow/runs/run/execution.json").is_file()
        second.close()


def test_concurrent_start_creates_one_sdk_thread() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        entered = threading.Event()
        release = threading.Event()

        class SlowAdapter(FakeAdapter):
            def start_thread(self) -> ThreadIdentity:
                self.service["starts"] += 1
                entered.set()
                assert release.wait(5)
                return ThreadIdentity("thread-stable")

        def factory(config: CodexSdkConfig) -> SlowAdapter:
            return SlowAdapter(config, service)

        first = Controller(repository, adapter_factory=factory)
        first.plan(_capsule(repository, repository, base, branch))
        second = Controller(repository, adapter_factory=factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first.start, "run", "m1")
            assert entered.wait(2)
            second_future = pool.submit(second.start, "run", "m1")
            time.sleep(0.1)
            assert service["starts"] == 1
            release.set()
            assert first_future.result(timeout=5).status is ExecutionStatus.COMPLETED
            assert second_future.result(timeout=5).status is ExecutionStatus.COMPLETED
        assert service["starts"] == 1
        first.close()
        second.close()


def test_pre_identity_failure_is_durable_and_never_retried() -> None:
    class FailingAdapter(FakeAdapter):
        def start_thread(self) -> ThreadIdentity:
            self.service["starts"] += 1
            raise OSError("no runtime")

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        controller = Controller(repository, adapter_factory=lambda config: FailingAdapter(config, service))
        controller.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(UncertainPreIdentity):
            controller.start("run", "m1")
        assert controller.status("run", "m1").status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY
        with pytest.raises(UncertainPreIdentity):
            controller.resume("run", "m1")
        assert service["starts"] == 1
        controller.close()


def test_durable_pre_call_checkpoint_prevents_restart_after_controller_crash() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "before_sdk_thread_start":
                raise RuntimeError("stop before external call")

        first = Controller(repository, adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(UncertainPreIdentity):
            first.start("run", "m1")
        assert service["starts"] == 0
        first.close()

        second = Controller(repository, adapter_factory=_factory(service))
        with pytest.raises(UncertainPreIdentity):
            second.resume("run", "m1")
        assert service["starts"] == 0
        second.close()


def test_validation_timeout_is_a_durable_failed_result() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, adapter_factory=_factory(_service()))
        capsule = replace(
            _capsule(repository, repository, base, branch),
            validation=ValidationSpec(("python3", "-c", "import time; time.sleep(1)"), 0.05),
        )
        controller.plan(capsule)
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.validation is not None and terminal.validation.timed_out
        controller.close()


def test_sdk_change_outside_mutable_paths_fails_closed() -> None:
    class EscapingAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / "outside.txt").write_text("not owned\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        controller = Controller(repository, adapter_factory=lambda config: EscapingAdapter(config, service))
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_managed_worktree_is_semantic_and_reused_across_milestones() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        workspace = repository.parent / "repo.worktrees" / "pilot"
        service = _service()
        controller = Controller(repository, adapter_factory=_factory(service))
        first = _capsule(
            repository,
            workspace,
            base,
            "agent/pilot",
            run="pilot-run",
            milestone="one",
            mode=WorkspaceMode.MANAGED_WORKTREE,
            lane="pilot",
        )
        controller.plan(first)
        controller.start("pilot-run", "one")
        assert workspace.is_dir()
        assert _git(workspace, "branch", "--show-current") == "agent/pilot"

        second = _capsule(
            repository,
            workspace,
            base,
            "agent/pilot",
            run="pilot-run",
            milestone="two",
            mode=WorkspaceMode.MANAGED_WORKTREE,
            lane="pilot",
        )
        controller.plan(second)
        controller.start("pilot-run", "two")
        assert controller.ledger.get_workspace_lease(workspace).owner_run_id == RunId("pilot-run")
        assert len(_git(repository, "worktree", "list", "--porcelain").split("worktree ")) == 3
        controller.close()


def test_existing_worktree_is_reused_without_controller_git_creation() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        workspace = repository.parent / "repo.worktrees" / "existing"
        workspace.parent.mkdir()
        _git(repository, "worktree", "add", "-b", "agent/existing", str(workspace), base)
        before = _git(repository, "worktree", "list", "--porcelain")
        controller = Controller(repository, adapter_factory=_factory(_service()))
        capsule = _capsule(
            repository,
            workspace,
            base,
            "agent/existing",
            mode=WorkspaceMode.EXISTING_WORKTREE,
            lane="existing",
        )
        controller.plan(capsule)
        controller.start("run", "m1")
        after = _git(repository, "worktree", "list", "--porcelain")
        assert before == after
        controller.close()


def test_capsule_rejects_path_overlap_and_nonsemantic_managed_path() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        with pytest.raises(ValueError, match="must not overlap"):
            ExecutionCapsule(
                1,
                RunId("run"),
                MilestoneId("m"),
                repository,
                WorkspaceMode.CURRENT_CHECKOUT,
                repository,
                branch,
                base,
                "lane",
                ("src",),
                ("src/protected.py",),
                ValidationSpec(("true",), 1),
                "gpt-test",
                ReasoningEffort.MEDIUM,
                "prompt",
                {"type": "object"},
            )
        controller = Controller(repository, adapter_factory=_factory(_service()))
        wrong = _capsule(
            repository,
            repository.parent / "wrong" / "lane",
            base,
            "agent/lane",
            mode=WorkspaceMode.MANAGED_WORKTREE,
            lane="lane",
        )
        with pytest.raises(WorkspaceConflict, match="semantic sibling"):
            controller.plan(wrong)
        controller.close()


def test_cancel_is_idempotent_and_keeps_workspace() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, adapter_factory=_factory(_service()))
        controller.plan(_capsule(repository, repository, base, branch))
        first = controller.cancel("run", "m1")
        second = controller.cancel("run", "m1")
        assert first.status is ExecutionStatus.CANCELLED
        assert second.status is ExecutionStatus.CANCELLED
        assert repository.is_dir()
        controller.close()


def test_cli_plan_status_and_cancel_emit_stable_json() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        capsule_path = repository / "capsule.json"
        capsule_path.write_text(json.dumps(capsule_json(_capsule(repository, repository, base, branch))))
        runner = CliRunner()

        planned = runner.invoke(
            app,
            ["plan", "--capsule", str(capsule_path), "--state-root", str(repository), "--json"],
        )
        assert planned.exit_code == 0, planned.output
        assert json.loads(planned.stdout)["status"] == "planned"

        status = runner.invoke(
            app,
            ["status", "--run-id", "run", "--milestone-id", "m1", "--state-root", str(repository), "--json"],
        )
        assert status.exit_code == 0, status.output
        assert json.loads(status.stdout)["checkpoint"] == "capsule_planned"

        cancelled = runner.invoke(
            app,
            ["cancel", "--run-id", "run", "--milestone-id", "m1", "--state-root", str(repository), "--json"],
        )
        assert cancelled.exit_code == 0, cancelled.output
        assert json.loads(cancelled.stdout)["status"] == "cancelled"


def test_cli_status_on_absent_state_is_read_only() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        root.mkdir()
        result = CliRunner().invoke(
            app,
            ["status", "--run-id", "run", "--milestone-id", "m1", "--state-root", str(root)],
        )
        assert result.exit_code == 2
        assert not (root / ".codex-flow").exists()


def test_adapter_fresh_process_resume_initializes_client_and_workspace_write() -> None:
    class Thread:
        id = "thread-1"

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def thread_resume(self, thread_id: str, **kwargs: object) -> Thread:
            self.calls.append((thread_id, kwargs))
            return Thread()

        def close(self) -> None:
            pass

    class Sdk:
        Sandbox = type("Sandbox", (), {"workspace_write": "write"})
        ApprovalMode = type("Approval", (), {"deny_all": "deny"})
        ReasoningEffort = type("Effort", (), {"medium": "medium"})
        SkillInput = None
        version = "test"

    with TemporaryDirectory() as directory:
        client = Client()
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=Path(directory),
            ),
            client_factory=lambda: client,
            sdk=Sdk(),
        )
        assert adapter.resume_thread(ThreadIdentity("thread-1")) == ThreadIdentity("thread-1")
        assert client.calls[0][1]["sandbox"] == "write"


def test_v2_ledger_migrates_forward_to_canonical_h3_schema() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "workflow.db"
        connection = sqlite3.connect(path)
        for ddl in _V2_TABLE_DDL.values():
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "2"),
                ("migration_marker", "complete"),
                ("schema_identity", "codex_flow_h2_v2"),
            ),
        )
        connection.commit()
        connection.close()

        ledger = Ledger(path)
        assert ledger.schema_version == CURRENT_SCHEMA_VERSION
        assert ledger.schema_identity == "codex_flow_h3_v3"
        assert "checkpoint" in ledger.schema_columns("executions")
        ledger.close()
