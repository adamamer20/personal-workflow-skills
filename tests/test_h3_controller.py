from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from typer.testing import CliRunner

from codex_flow.backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig, SdkSandboxPolicy
from codex_flow.cli import app
from codex_flow.controller import (
    Controller,
    ControllerError,
    ResumeRequired,
    UncertainPreIdentity,
    UncertainTurn,
    _run_validation,
    capsule_from_json,
    capsule_json,
)
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
    ValidationFailureCode,
    ValidationSpec,
    WorkspaceMode,
)
from codex_flow.ledger import _V2_TABLE_DDL, CURRENT_SCHEMA_VERSION, CorruptSchemaError, Ledger, WorkspaceLeaseConflict
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


def _test_sandbox_policy(root: Path) -> SdkSandboxPolicy:
    workspace = root / "workspace"
    workspace.mkdir()
    protected = workspace / "protected.txt"
    protected.write_text("protected\n")
    runtime = root / "runtime"
    auth = root / "auth.json"
    auth.write_text("{}\n")
    return SdkSandboxPolicy(
        workspace,
        (protected,),
        runtime / "home",
        auth,
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

        controller = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=fault)
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

        first = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        durable = first.status("run", "m1")
        assert durable.thread_id == ThreadIdentity("thread-stable")
        assert durable.turn_id is None
        with pytest.raises(ResumeRequired):
            first.start("run", "m1")
        first.close()

        second = Controller(repository, _trusted_test_adapter_factory=_factory(service))
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

        first = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="projection failed"):
            first.start("run", "m1")
        assert first.status("run", "m1").status is ExecutionStatus.COMPLETED
        first.close()

        second = Controller(repository, _trusted_test_adapter_factory=_factory(service))
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

        first = Controller(repository, _trusted_test_adapter_factory=factory)
        first.plan(_capsule(repository, repository, base, branch))
        second = Controller(repository, _trusted_test_adapter_factory=factory)
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
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: FailingAdapter(config, service)
        )
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

        first = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(UncertainPreIdentity):
            first.start("run", "m1")
        assert service["starts"] == 0
        first.close()

        second = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        with pytest.raises(UncertainPreIdentity):
            second.resume("run", "m1")
        assert service["starts"] == 0
        second.close()


def test_validation_timeout_is_a_durable_failed_result() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
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
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: EscapingAdapter(config, service)
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_production_sdk_mount_boundary_denies_external_protected_controller_and_git_writes() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        outside = repository.parent / "external.txt"
        outside.write_text("external before\n")
        external_link = repository / "external-link.txt"
        external_link.symlink_to(outside)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsule = _capsule(repository, repository, base, branch)
        controller.plan(capsule)
        policy, policy_sha256 = controller._sandbox_policy(capsule)
        policy.prepare()
        assert len(policy_sha256) == 64
        script = """
from pathlib import Path
import sys
workspace, outside, external_link, protected, controller_state, git_config = map(Path, sys.argv[1:])
for label, target in (
    ('external', outside),
    ('external_symlink', external_link),
    ('protected', protected),
    ('controller', controller_state),
    ('git_config', git_config),
):
    try:
        target.write_text(label + ' escaped\\n')
    except OSError:
        print(label + '=denied')
    else:
        print(label + '=allowed')
try:
    (workspace / 'external-hardlink.txt').hardlink_to(outside)
except OSError:
    print('external_hardlink=denied')
else:
    print('external_hardlink=allowed')
(workspace / 'result.txt').write_text('allowed\\n')
"""
        command = policy.wrap_command(
            (
                sys.executable,
                "-c",
                script,
                str(repository),
                str(outside),
                str(external_link),
                str(repository / "protected.txt"),
                str(repository / ".codex-flow" / "executor.txt"),
                str(repository / ".git" / "config"),
            )
        )
        completed = subprocess.run(command, text=True, capture_output=True, check=True, timeout=10)
        assert set(completed.stdout.splitlines()) == {
            "external=denied",
            "external_symlink=denied",
            "external_hardlink=denied",
            "protected=denied",
            "controller=denied",
            "git_config=denied",
        }
        assert (repository / "result.txt").read_text() == "allowed\n"
        assert outside.read_text() == "external before\n"
        assert (repository / "protected.txt").read_text() == "protected\n"
        assert not (repository / ".codex-flow" / "executor.txt").exists()
        controller.close()


def test_managed_worktree_is_semantic_and_reused_across_milestones() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        workspace = repository.parent / "repo.worktrees" / "pilot"
        service = _service()
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(service))
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
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
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
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
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
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
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
        policy = _test_sandbox_policy(Path(directory))
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=policy.workspace,
                sandbox_policy=policy,
            ),
            client_factory=lambda: client,
            sdk=Sdk(),
        )
        assert adapter.resume_thread(ThreadIdentity("thread-1")) == ThreadIdentity("thread-1")
        assert client.calls[0][1]["sandbox"] == "write"
        assert client.calls[0][1]["cwd"] == str(policy.workspace)
        assert client.calls[0][1]["config"] == policy.thread_config


def test_production_adapter_uses_only_the_sealed_sdk_child_invocation() -> None:
    captured: dict[str, object] = {}

    class Thread:
        id = "thread-production"

    class Client:
        metadata = type("Metadata", (), {})()

        def thread_start(self, **kwargs: object) -> Thread:
            captured["thread_start"] = kwargs
            return Thread()

        def close(self) -> None:
            pass

    class Sdk:
        Sandbox = type("Sandbox", (), {"workspace_write": "write"})
        ApprovalMode = type("Approval", (), {"deny_all": "deny"})
        ReasoningEffort = type("Effort", (), {"medium": "medium"})
        SkillInput = None
        version = "test"

        @staticmethod
        def CodexConfig(**kwargs: object) -> object:
            captured["sdk_config"] = kwargs
            return kwargs

        @staticmethod
        def Codex(*, config: object) -> Client:
            captured["codex_config"] = config
            return Client()

    with TemporaryDirectory() as directory:
        policy = _test_sandbox_policy(Path(directory))
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=policy.workspace,
                sandbox_policy=policy,
            ),
            sdk=Sdk(),
        )
        assert adapter.start_thread() == ThreadIdentity("thread-production")
        sdk_config = captured["sdk_config"]
        assert isinstance(sdk_config, dict)
        launch = sdk_config["launch_args_override"]
        assert isinstance(launch, tuple)
        assert launch[0] == "/usr/bin/bwrap"
        assert "--ro-bind" in launch and "--bind" in launch
        assert "sandbox_workspace_write.writable_roots=[]" in launch
        assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in launch
        assert "sandbox_workspace_write.exclude_slash_tmp=true" in launch
        assert sdk_config["env"] == policy.environment
        assert policy.environment["TMPDIR"] == str(policy.workspace)
        thread_start = captured["thread_start"]
        assert isinstance(thread_start, dict)
        assert thread_start["config"] == policy.thread_config
        assert thread_start["sandbox"] == "write"
        adapter.close()


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
        assert ledger.schema_identity == "codex_flow_h3_integrity_v4"
        assert "checkpoint" in ledger.schema_columns("executions")
        ledger.close()


def test_ignored_out_of_scope_file_is_rejected() -> None:
    class IgnoringAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / ".git" / "info" / "exclude").write_text("ignored.txt\n")
            (self.config.cwd / "ignored.txt").write_text("outside\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: IgnoringAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_validation_launch_oserror_is_typed_and_durable() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsule = replace(
            _capsule(repository, repository, base, branch), validation=ValidationSpec(("missing-validation",), 1)
        )
        controller.plan(capsule)
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "validation_executable_unavailable"}
        assert terminal.validation is not None
        assert terminal.validation.error_code is ValidationFailureCode.EXECUTABLE_UNAVAILABLE
        assert "missing-validation" not in json.dumps(terminal.result)
        controller.close()


def test_failed_structured_terminal_outcome_cannot_become_completed() -> None:
    class FailingOutcomeAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            return replace(observation, structured_output={"status": "failed"})

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: FailingOutcomeAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "sdk_terminal_outcome_failed"}
        controller.close()


def test_identity_and_running_transition_are_one_causal_commit() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected")

        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()), fault_injector=crash)
        controller.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected"):
            controller.start("run", "m1")
        record = controller.status("run", "m1")
        assert record.status is ExecutionStatus.THREAD_STARTED
        events = controller.ledger.events("run", "m1")
        assert [(event.from_state.value, event.to_state.value) for event in events] == [
            ("PLANNED", "STARTING"),
            ("STARTING", "RUNNING"),
        ]
        controller.close()


def test_uncertain_turn_checkpoint_never_replays_external_turn() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "before_sdk_turn":
                raise RuntimeError("stop")

        first = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="stop"):
            first.start("run", "m1")
        first.close()
        second = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        with pytest.raises(UncertainTurn):
            second.resume("run", "m1")
        assert service["turns"] == 0
        second.close()


def test_committed_out_of_scope_mutation_fails_closed() -> None:
    class CommittingAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / "outside.txt").write_text("committed\n")
            _git(self.config.cwd, "add", "outside.txt")
            _git(self.config.cwd, "commit", "-qm", "out of scope")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: CommittingAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_branch_change_fails_closed_even_when_paths_are_owned() -> None:
    class BranchChangingAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            _git(self.config.cwd, "branch", "-m", "agent/changed")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: BranchChangingAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "branch_or_history_changed"}
        controller.close()


def test_existing_worktree_subdirectory_is_rejected() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        workspace = repository.parent / "repo.worktrees" / "existing"
        workspace.parent.mkdir()
        _git(repository, "worktree", "add", "-b", "agent/existing", str(workspace), base)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsule = _capsule(
            repository,
            workspace / "nested",
            base,
            "agent/existing",
            mode=WorkspaceMode.EXISTING_WORKTREE,
            lane="existing",
        )
        with pytest.raises(WorkspaceConflict, match="physical Git toplevel"):
            controller.plan(capsule)
        controller.close()


def test_nonterminal_workspace_owner_blocks_parallel_milestone() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        first = _capsule(repository, repository, base, branch, milestone="one")
        second = _capsule(repository, repository, base, branch, milestone="two")
        controller.plan(first)
        controller.plan(second)
        controller.ledger.claim_dispatch("run", "one", "executor", 1)
        controller.ledger.acquire_workspace_lease(first)
        with pytest.raises(WorkspaceLeaseConflict, match="nonterminal execution owner"):
            controller.ledger.acquire_workspace_lease(second)
        controller.close()


def test_capsule_json_rejects_stringified_numeric_fields() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        value = capsule_json(_capsule(repository, repository, base, branch))
        value["capsule_version"] = "1"
        with pytest.raises(ValueError, match="capsule_version"):
            capsule_from_json(value)
        value = capsule_json(_capsule(repository, repository, base, branch))
        assert isinstance(value["validation"], dict)
        value["validation"]["timeout_seconds"] = "5"
        with pytest.raises(ValueError, match="timeout_seconds"):
            capsule_from_json(value)


def test_commit_then_revert_out_of_scope_history_fails_closed() -> None:
    class RevertingAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            outside = self.config.cwd / "outside.txt"
            outside.write_text("transient\n")
            _git(self.config.cwd, "add", "outside.txt")
            _git(self.config.cwd, "commit", "-qm", "out of scope")
            _git(self.config.cwd, "rm", "-q", "outside.txt")
            _git(self.config.cwd, "commit", "-qm", "revert out of scope")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: RevertingAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_commit_then_hard_reset_is_detected_by_durable_git_authority() -> None:
    class ResettingAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            original = _git(self.config.cwd, "rev-parse", "HEAD")
            (self.config.cwd / "outside.txt").write_text("transient\n")
            _git(self.config.cwd, "add", "outside.txt")
            _git(self.config.cwd, "commit", "-qm", "transient out of scope")
            _git(self.config.cwd, "reset", "--hard", original)
            (self.config.cwd / "result.txt").write_text("edited by sdk\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: ResettingAdapter(config, _service()),
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        integrity = controller.ledger.get_execution_integrity("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "git_authority_changed"}
        assert integrity.git_authority_before_sha256 != integrity.git_authority_after_sha256
        assert _git(repository, "rev-parse", "HEAD") == base
        controller.close()


def test_git_config_and_index_mutations_are_git_authority_failures() -> None:
    class GitMetadataAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            _git(self.config.cwd, "config", "controller.escape", "true")
            _git(self.config.cwd, "add", "result.txt")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: GitMetadataAdapter(config, _service()),
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "git_authority_changed"}
        controller.close()


def test_terminal_execution_cannot_reopen_without_its_integrity_authority_row() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        controller.plan(_capsule(repository, repository, base, branch))
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        controller.close()
        database = repository / ".codex-flow" / "workflow.db"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM execution_integrity WHERE run_id = 'run' AND milestone_id = 'm1'")
        connection.commit()
        connection.close()
        with pytest.raises(CorruptSchemaError, match="lacks its integrity authority row"):
            Ledger(database)


def test_mutable_symlink_external_write_is_rejected() -> None:
    class SymlinkAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            outside = self.config.cwd.parent / "external.txt"
            outside.write_text("outside\n")
            result = self.config.cwd / "result.txt"
            result.unlink()
            result.symlink_to(outside)
            outside.write_text("executor escaped\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        external = repository.parent / "external.txt"
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: SymlinkAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        assert external.read_text() == "executor escaped\n"
        controller.close()


def test_mutable_hardlink_external_write_is_rejected() -> None:
    class HardlinkAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            external = self.config.cwd.parent / "external-hardlink.txt"
            external.write_text("outside\n")
            result = self.config.cwd / "result.txt"
            result.unlink()
            result.hardlink_to(external)
            external.write_text("executor escaped through hardlink\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: HardlinkAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_executor_mutation_of_controller_state_is_terminal_failure() -> None:
    class InternalAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / ".codex-flow" / "executor.txt").write_text("forbidden\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository, _trusted_test_adapter_factory=lambda config: InternalAdapter(config, _service())
        )
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "controller_state_mutated"}
        controller.close()


def test_protected_digest_failure_is_durable_terminal_integrity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import codex_flow.controller as controller_module

    calls = 0
    original_digest = controller_module.protected_paths_digest

    def fail_digest(*_args: Any, **_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("secret digest failure")
        return original_digest(*_args, **_kwargs)

    monkeypatch.setattr(controller_module, "protected_paths_digest", fail_digest)
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "protected_paths_integrity_failure"}
        assert terminal.validation is not None
        assert terminal.validation.error_code is ValidationFailureCode.INTEGRITY_FAILURE
        assert controller.ledger.current_state("run", "m1").value == "FAILED"
        assert controller.ledger.events("run", "m1")[-1].to_state.value == "FAILED"
        controller.close()


def test_nonzero_validation_exit_is_truthful_and_typed() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsule = replace(_capsule(repository, repository, base, branch), validation=ValidationSpec(("false",), 1))
        controller.plan(capsule)
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "validation_failed"}
        assert terminal.validation is not None
        assert terminal.validation.error_code is ValidationFailureCode.NONZERO_EXIT
        controller.close()


def test_validation_argv_nul_is_rejected_and_launch_value_error_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="argv"):
        ValidationSpec(("python3", "bad\x00arg"), 1)

    import codex_flow.controller as controller_module

    def fail_launch(*_args: Any, **_kwargs: Any) -> object:
        raise ValueError("embedded null")

    monkeypatch.setattr(controller_module.subprocess, "run", fail_launch)
    observation = _run_validation(ValidationSpec(("true",), 1), Path("/tmp"))
    assert observation.error_code is ValidationFailureCode.EXECUTABLE_UNAVAILABLE
    assert observation.exit_code == -127


def test_controller_binding_failure_leaves_no_outside_root_state() -> None:
    with TemporaryDirectory() as directory:
        outside = Path(directory) / "outside"
        outside.mkdir()
        with pytest.raises(ControllerError):
            Controller(outside)
        assert not (outside / ".codex-flow" / "workflow.db").exists()
