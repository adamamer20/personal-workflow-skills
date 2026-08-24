from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from typer.testing import CliRunner

from codex_flow.backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig, NativeRuntimeConfig
from codex_flow.cli import app
from codex_flow.controller import (
    Controller,
    ControllerError,
    ResumeRequired,
    UncertainPreIdentity,
    UncertainTurn,
    UnsafeResumeCompatibilityChange,
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
    NativePermissionAuthority,
    NativePermissionMode,
    ReasoningEffort,
    RunId,
    ThreadIdentity,
    TurnObservation,
    ValidationFailureCode,
    ValidationObservation,
    ValidationSpec,
    WorkspaceMode,
)
from codex_flow.ledger import (
    _V2_TABLE_DDL,
    _V4_TABLE_DDL,
    _V5_TABLE_DDL,
    _V6_TABLE_DDL,
    CURRENT_SCHEMA_VERSION,
    CorruptSchemaError,
    Ledger,
    RecordNotFound,
    StaleWriter,
    WorkspaceLeaseConflict,
)
from codex_flow.native_profile import NativeProfileError, NativeProfileProjection
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
        2,
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


def _test_native_profile(native_home: Path) -> NativeProfileProjection:
    native_home.mkdir(mode=0o700)
    native_home.chmod(0o700)
    for name in ("memories", "plugins", "skills"):
        (native_home / name).mkdir(mode=0o700)
    catalog = native_home / "models.json"
    catalog.write_text('{"fetched_at":"2026-08-24T00:00:00Z","client_version":"0.147.0","models":[]}\n')
    catalog.chmod(0o600)
    (native_home / "config.toml").write_text(
        f'''model_provider = "codex-lb"
model_catalog_json = "{catalog}"
personality = "pragmatic"
approval_policy = "never"
approvals_reviewer = "user"
sandbox_mode = "danger-full-access"

[model_providers.codex-lb]
name = "openai"
base_url = "http://127.0.0.1:2455/backend-api/codex"
wire_api = "responses"
env_key = "CODEX_LB_API_KEY"
requires_openai_auth = true
supports_websockets = true

[agents]
enabled = true

[features]
memories = true

[hooks]

[marketplaces]

[mcp_servers]

[plugins]

[profiles]

[projects]

[shell_environment_policy]

[skills]
'''
    )
    (native_home / "config.toml").chmod(0o600)
    auth = native_home / "auth.json"
    auth.write_text("{}\n")
    auth.chmod(0o600)
    return NativeProfileProjection.load(native_home, environment={"CODEX_LB_API_KEY": "test-only"})


def _test_native_runtime(root: Path) -> tuple[Path, NativeRuntimeConfig]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    profile = _test_native_profile(root / "native-home")
    return workspace, NativeRuntimeConfig(root / "runtime" / "home", profile)


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


def test_resume_atomically_inherits_native_permission_tightening_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        initial = _test_native_profile(home)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=initial,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        before_integrity = first.ledger.get_execution_integrity("run", "m1")
        first.close()

        config = home / "config.toml"
        config.write_bytes(config.read_bytes().replace(b"danger-full-access", b"read-only"))
        config.chmod(0o600)
        restricted = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        entered = threading.Event()
        release = threading.Event()
        controllers: list[Controller] = []

        class SlowResume(FakeAdapter):
            def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity:
                integrity = controllers[0].ledger.get_execution_integrity("run", "m1")
                assert integrity.effective_permission is not None
                assert integrity.effective_permission.sandbox_mode == "read-only"
                entered.set()
                assert release.wait(5)
                return super().resume_thread(thread)

        def factory(config: CodexSdkConfig) -> SlowResume:
            assert config.effective_permission is not None
            assert config.effective_permission.sandbox_mode == "read-only"
            return SlowResume(config, service)

        controllers.extend(
            [
                Controller(
                    repository,
                    _trusted_test_adapter_factory=factory,
                    _trusted_test_native_profile=restricted,
                ),
                Controller(
                    repository,
                    _trusted_test_adapter_factory=factory,
                    _trusted_test_native_profile=restricted,
                ),
            ]
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_resume = pool.submit(controllers[0].resume, "run", "m1")
            assert entered.wait(2)
            second_resume = pool.submit(controllers[1].resume, "run", "m1")
            time.sleep(0.1)
            assert service["resumes"] == 0
            release.set()
            assert first_resume.result(timeout=5).status is ExecutionStatus.COMPLETED
            assert second_resume.result(timeout=5).status is ExecutionStatus.COMPLETED
        assert service["resumes"] == 1
        after_integrity = controllers[0].ledger.get_execution_integrity("run", "m1")
        assert after_integrity.effective_permission == restricted.effective_authority(
            NativePermissionMode.INHERIT_NATIVE
        )
        assert after_integrity.native_profile_sha256 != before_integrity.native_profile_sha256
        assert after_integrity.native_compatibility_sha256 == before_integrity.native_compatibility_sha256
        for controller in controllers:
            controller.close()


def test_resume_never_broadens_a_prior_native_restriction() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_bytes(config.read_bytes().replace(b"danger-full-access", b"read-only"))
        config.chmod(0o600)
        restricted = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=restricted,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        first.close()

        config.write_bytes(config.read_bytes().replace(b"read-only", b"danger-full-access"))
        config.chmod(0o600)
        broader = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        captured: list[CodexSdkConfig] = []

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            captured.append(config)
            return FakeAdapter(config, service)

        second = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=broader,
        )
        assert second.resume("run", "m1").status is ExecutionStatus.COMPLETED
        assert captured[0].effective_permission is not None
        assert captured[0].effective_permission.sandbox_mode == "read-only"
        second.close()


def test_resume_rejects_compatibility_change_before_adapter_creation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        initial = _test_native_profile(home)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=initial,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        first.close()

        config = home / "config.toml"
        config.write_bytes(config.read_bytes().replace(b"127.0.0.1:2455", b"127.0.0.1:2456"))
        config.chmod(0o600)
        incompatible = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        second = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=incompatible,
        )
        with pytest.raises(UnsafeResumeCompatibilityChange, match="compatibility changed"):
            second.resume("run", "m1")
        assert adapter_creations == 0
        assert service["resumes"] == 0
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


def test_disjoint_sequential_milestones_reuse_baseline_and_recover_before_external_start() -> None:
    class NamedOutputAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            target = str(self.service["target"])
            (self.config.cwd / target).write_text(f"{target}\n")
            return TurnObservation(
                thread,
                f"turn-{target}",
                "completed",
                json.dumps({"status": "done"}),
                {"status": "done"},
                (),
            )

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        workspace = repository.parent / "repo.worktrees" / "sequential"
        service: dict[str, Any] = {**_service(), "target": "first.txt"}
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NamedOutputAdapter(config, service),
        )
        first = replace(
            _capsule(
                repository,
                workspace,
                base,
                "agent/sequential",
                run="sequential-run",
                milestone="one",
                mode=WorkspaceMode.MANAGED_WORKTREE,
                lane="sequential",
            ),
            mutable_paths=("first.txt",),
        )
        controller.plan(first)
        assert controller.start("sequential-run", "one").status is ExecutionStatus.COMPLETED

        second = replace(first, milestone_id=MilestoneId("two"), mutable_paths=("second.txt",))
        controller.plan(second)
        controller.close()

        intruder = workspace / "intruder.txt"
        intruder.write_text("not a prior accepted output\n")
        preflight = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NamedOutputAdapter(config, service),
        )
        with pytest.raises(ControllerError, match="outside mutable paths"):
            preflight.start("sequential-run", "two")
        assert preflight.ledger.current_state("sequential-run", "two").value == "PLANNED"
        assert preflight.status("sequential-run", "two").checkpoint is ControllerCheckpoint.CAPSULE_PLANNED
        preflight.close()
        intruder.unlink()

        def fail_after_baseline(stage: str) -> None:
            if stage == "after_workspace_baseline":
                raise RuntimeError("pre-external preflight stop")

        stopped = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NamedOutputAdapter(config, service),
            fault_injector=fail_after_baseline,
        )
        with pytest.raises(RuntimeError, match="pre-external preflight stop"):
            stopped.start("sequential-run", "two")
        durable = stopped.status("sequential-run", "two")
        assert durable.status is ExecutionStatus.PLANNED
        assert durable.checkpoint is ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE
        assert stopped.ledger.current_state("sequential-run", "two").value == "PLANNED"
        stopped.close()

        service["target"] = "second.txt"
        recovered = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NamedOutputAdapter(config, service),
        )
        terminal = recovered.start("sequential-run", "two")
        assert terminal.status is ExecutionStatus.COMPLETED
        assert (workspace / "first.txt").read_text() == "first.txt\n"
        assert (workspace / "second.txt").read_text() == "second.txt\n"
        assert recovered.ledger.get_workspace_lease(workspace).base_sha == base
        recovered.close()


@pytest.mark.parametrize("mode", (WorkspaceMode.MANAGED_WORKTREE, WorkspaceMode.EXISTING_WORKTREE))
def test_distinct_worktree_codex_flow_write_is_not_exempt_from_mutation_scope(mode: WorkspaceMode) -> None:
    class WorkspaceStateAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / ".codex-flow").mkdir()
            (self.config.cwd / ".codex-flow" / "executor.txt").write_text("not authoritative\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        lane = "managed-state" if mode is WorkspaceMode.MANAGED_WORKTREE else "existing-state"
        workspace = repository.parent / "repo.worktrees" / lane
        if mode is WorkspaceMode.EXISTING_WORKTREE:
            workspace.parent.mkdir()
            _git(repository, "worktree", "add", "-b", f"agent/{lane}", str(workspace), base)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: WorkspaceStateAdapter(config, _service()),
        )
        controller.plan(
            _capsule(
                repository,
                workspace,
                base,
                f"agent/{lane}",
                mode=mode,
                lane=lane,
            )
        )
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
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
                2,
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


def test_nonterminal_controller_fact_mutators_preserve_contractual_idempotency() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        capsule = _capsule(repository, repository, base, branch)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        planned = controller.plan(capsule)
        assert (
            controller.ledger.plan_execution(
                capsule,
                capsule_path=planned.capsule_path,
                capsule_sha256=planned.capsule_sha256,
                protected_before_sha256=planned.protected_before_sha256,
            )
            == planned
        )

        lease = controller.ledger.acquire_workspace_lease(capsule)
        assert controller.ledger.acquire_workspace_lease(capsule) == lease
        authority = NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "read-only", "never")
        profile = controller.ledger.record_native_profile("run", "m1", "0" * 64, "1" * 64, authority, base, ())
        assert controller.ledger.record_native_profile("run", "m1", "0" * 64, "1" * 64, authority, base, ()) == profile
        controller.ledger.claim_dispatch("run", "m1", "executor", 1)
        thread_starting = controller.ledger.record_thread_starting("run", "m1")
        assert controller.ledger.record_thread_starting("run", "m1") == thread_starting
        thread = ThreadIdentity("thread-idempotent")
        thread_started = controller.ledger.record_thread_identity("run", "m1", thread)
        assert controller.ledger.record_thread_identity("run", "m1", thread) == thread_started
        git_before = controller.ledger.record_git_authority_before("run", "m1", "2" * 64)
        assert controller.ledger.record_git_authority_before("run", "m1", "2" * 64) == git_before
        turn_starting = controller.ledger.record_turn_starting("run", "m1")
        assert controller.ledger.record_turn_starting("run", "m1") == turn_starting
        observation = TurnObservation(
            thread,
            "turn-idempotent",
            "completed",
            json.dumps({"status": "done"}),
            {"status": "done"},
            (),
        )
        turn = controller.ledger.record_turn("run", "m1", observation)
        assert controller.ledger.record_turn("run", "m1", observation) == turn
        conflicting = replace(observation, structured_output={"status": "different"})
        with pytest.raises(StaleWriter, match="different durable SDK turn facts"):
            controller.ledger.record_turn("run", "m1", conflicting)
        controller.close()
        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        assert reopened.ledger.record_turn("run", "m1", observation) == turn
        with pytest.raises(StaleWriter, match="different durable SDK turn facts"):
            reopened.ledger.record_turn("run", "m1", conflicting)
        assert planned.status is ExecutionStatus.PLANNED
        reopened.close()


def test_turn_observation_requires_durable_turn_start_before_first_write() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        capsule = _capsule(repository, repository, base, branch)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        controller.plan(capsule)
        controller.ledger.acquire_workspace_lease(capsule)
        authority = NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "read-only", "never")
        controller.ledger.record_native_profile("run", "m1", "0" * 64, "1" * 64, authority, base, ())
        controller.ledger.claim_dispatch("run", "m1", "executor", 1)
        controller.ledger.record_thread_starting("run", "m1")
        thread = ThreadIdentity("thread-causal")
        controller.ledger.record_thread_identity("run", "m1", thread)
        observation = TurnObservation(
            thread,
            "turn-causal",
            "completed",
            json.dumps({"status": "done"}),
            {"status": "done"},
            (),
        )
        with pytest.raises(StaleWriter, match="prior durable turn-start authority"):
            controller.ledger.record_turn("run", "m1", observation)
        assert controller.ledger.sdk_lifecycle_events("run", "m1") == ()
        controller.close()

        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        with pytest.raises(StaleWriter, match="prior durable turn-start authority"):
            reopened.ledger.record_turn("run", "m1", observation)
        reopened.close()


@pytest.mark.parametrize(
    "terminal_status",
    (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED),
)
def test_terminal_execution_rejects_every_public_controller_fact_mutator_after_reopen(
    terminal_status: ExecutionStatus,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        capsule = _capsule(repository, repository, base, branch)
        if terminal_status is ExecutionStatus.FAILED:
            capsule = replace(capsule, validation=ValidationSpec(("false",), 5))
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(_service()),
            _trusted_test_native_profile=_test_native_profile(root / "native-home"),
        )
        controller.plan(capsule)
        terminal = (
            controller.cancel("run", "m1")
            if terminal_status is ExecutionStatus.CANCELLED
            else controller.start("run", "m1")
        )
        assert terminal.status is terminal_status
        try:
            integrity = controller.ledger.get_execution_integrity("run", "m1")
        except KeyError:
            integrity = None
        controller.close()

        authority = (
            integrity.effective_permission
            if integrity is not None and integrity.effective_permission is not None
            else NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "read-only", "never")
        )
        profile_sha256 = integrity.native_profile_sha256 if integrity is not None else "0" * 64
        compatibility_sha256 = integrity.native_compatibility_sha256 if integrity is not None else "1" * 64
        git_before_sha256 = (
            integrity.git_authority_before_sha256
            if integrity is not None and integrity.git_authority_before_sha256 is not None
            else "2" * 64
        )
        git_after_sha256 = (
            integrity.git_authority_after_sha256
            if integrity is not None and integrity.git_authority_after_sha256 is not None
            else "4" * 64
        )
        observation = TurnObservation(
            terminal.thread_id or ThreadIdentity("thread-stale"),
            terminal.turn_id or "turn-stale",
            "completed",
            json.dumps({"status": "done"}),
            {"status": "done"},
            (),
        )
        validation = terminal.validation or ValidationObservation(("true",), 0, "0" * 64, "0" * 64, False, 0.01)
        result = terminal.result or {"status": "done"}
        protected_after_sha256 = terminal.protected_after_sha256 or "3" * 64
        database_path = repository / ".codex-flow" / "workflow.db"

        def rows(current: Controller) -> tuple[Any, ...]:
            try:
                current_integrity = current.ledger.get_execution_integrity("run", "m1")
            except KeyError:
                current_integrity = None
            try:
                lease = current.ledger.get_workspace_lease(repository)
            except RecordNotFound:
                lease = None
            return (
                current.status("run", "m1"),
                current_integrity,
                lease,
                current.ledger.sdk_lifecycle_events("run", "m1"),
                current.ledger.events("run", "m1"),
                current.ledger.snapshot("run"),
            )

        mutator_names = (
            "record_native_profile",
            "rebind_native_profile_for_resume",
            "record_git_authority_before",
            "plan_execution",
            "acquire_workspace_lease",
            "record_thread_identity",
            "record_thread_starting",
            "record_pre_identity_uncertainty",
            "record_turn_starting",
            "record_turn",
            "record_terminal_execution",
            "cancel_execution",
        )

        def mutate(current: Controller, mutator_name: str) -> None:
            if mutator_name == "record_native_profile":
                current.ledger.record_native_profile(
                    "run",
                    "m1",
                    profile_sha256,
                    compatibility_sha256,
                    authority,
                    integrity.workspace_baseline_head_sha if integrity is not None else base,
                    integrity.workspace_baseline if integrity is not None and integrity.workspace_baseline else (),
                )
            elif mutator_name == "rebind_native_profile_for_resume":
                current.ledger.rebind_native_profile_for_resume(
                    "run", "m1", profile_sha256, compatibility_sha256, authority
                )
            elif mutator_name == "record_git_authority_before":
                current.ledger.record_git_authority_before("run", "m1", git_before_sha256)
            elif mutator_name == "plan_execution":
                current.ledger.plan_execution(
                    capsule,
                    capsule_path=terminal.capsule_path,
                    capsule_sha256=terminal.capsule_sha256,
                    protected_before_sha256=terminal.protected_before_sha256,
                )
            elif mutator_name == "acquire_workspace_lease":
                current.ledger.acquire_workspace_lease(capsule)
            elif mutator_name == "record_thread_identity":
                current.ledger.record_thread_identity("run", "m1", terminal.thread_id or ThreadIdentity("thread-stale"))
            elif mutator_name == "record_thread_starting":
                current.ledger.record_thread_starting("run", "m1")
            elif mutator_name == "record_pre_identity_uncertainty":
                current.ledger.record_pre_identity_uncertainty("run", "m1")
            elif mutator_name == "record_turn_starting":
                current.ledger.record_turn_starting("run", "m1")
            elif mutator_name == "record_turn":
                current.ledger.record_turn("run", "m1", observation)
            elif mutator_name == "record_terminal_execution":
                current.ledger.record_terminal_execution(
                    "run",
                    "m1",
                    result=result,
                    validation=validation,
                    protected_after_sha256=protected_after_sha256,
                    git_authority_after_sha256=git_after_sha256,
                )
            elif mutator_name == "cancel_execution":
                current.ledger.cancel_execution("run", "m1")
            else:
                raise AssertionError(f"unhandled public mutator: {mutator_name}")

        for mutator_name in mutator_names:
            bytes_before = database_path.read_bytes()
            reopened = Controller(
                repository,
                _trusted_test_adapter_factory=_factory(_service()),
                _trusted_test_native_profile=_test_native_profile(root / f"native-home-{mutator_name}"),
            )
            rows_before = rows(reopened)
            with pytest.raises(StaleWriter):
                mutate(reopened, mutator_name)
            assert rows(reopened) == rows_before
            reopened.close()
            assert database_path.read_bytes() == bytes_before


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


def test_adapter_fresh_process_resume_inherits_native_permissions() -> None:
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
        workspace, runtime = _test_native_runtime(Path(directory))
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=None,
                cwd=workspace,
                native_runtime=runtime,
                permission_mode=NativePermissionMode.INHERIT_NATIVE,
                effective_permission=runtime.native_profile.effective_authority(NativePermissionMode.INHERIT_NATIVE),
            ),
            client_factory=lambda: client,
            sdk=Sdk(),
        )
        assert adapter.resume_thread(ThreadIdentity("thread-1")) == ThreadIdentity("thread-1")
        assert "sandbox" not in client.calls[0][1]
        assert "approval_mode" not in client.calls[0][1]
        assert client.calls[0][1]["cwd"] == str(workspace)


def test_adapter_resume_retains_prior_read_only_authority_when_native_broadens() -> None:
    class Thread:
        id = "thread-1"

    class Client:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def thread_resume(self, thread_id: str, **kwargs: object) -> Thread:
            assert thread_id == "thread-1"
            self.kwargs = kwargs
            return Thread()

        def close(self) -> None:
            pass

    class Sdk:
        Sandbox = type("Sandbox", (), {"read_only": "read"})
        ApprovalMode = type("Approval", (), {"deny_all": "deny"})
        ReasoningEffort = type("Effort", (), {"medium": "medium"})
        SkillInput = None
        version = "test"

    with TemporaryDirectory() as directory:
        client = Client()
        workspace, runtime = _test_native_runtime(Path(directory))
        retained = NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "read-only", "never")
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=None,
                cwd=workspace,
                native_runtime=runtime,
                permission_mode=NativePermissionMode.INHERIT_NATIVE,
                effective_permission=retained,
            ),
            client_factory=lambda: client,
            sdk=Sdk(),
        )
        assert adapter.resume_thread(ThreadIdentity("thread-1")) == ThreadIdentity("thread-1")
        assert client.kwargs["sandbox"] == "read"
        assert "approval_mode" not in client.kwargs


def test_adapter_read_only_request_is_the_only_sdk_permission_override() -> None:
    class Thread:
        id = "thread-1"

    class Client:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def thread_start(self, **kwargs: object) -> Thread:
            self.kwargs = kwargs
            return Thread()

        def close(self) -> None:
            pass

    class Sdk:
        Sandbox = type("Sandbox", (), {"read_only": "read"})
        ApprovalMode = type("Approval", (), {"deny_all": "deny"})
        ReasoningEffort = type("Effort", (), {"medium": "medium"})
        SkillInput = None
        version = "test"

    with TemporaryDirectory() as directory:
        client = Client()
        workspace, runtime = _test_native_runtime(Path(directory))
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=None,
                cwd=workspace,
                native_runtime=runtime,
                permission_mode=NativePermissionMode.READ_ONLY,
                effective_permission=runtime.native_profile.effective_authority(NativePermissionMode.READ_ONLY),
            ),
            client_factory=lambda: client,
            sdk=Sdk(),
        )
        assert adapter.start_thread() == ThreadIdentity("thread-1")
        assert client.kwargs["sandbox"] == "read"
        assert "approval_mode" not in client.kwargs


def test_production_adapter_uses_normal_sdk_child_with_private_native_home() -> None:
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
        workspace, runtime = _test_native_runtime(Path(directory))
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=None,
                cwd=workspace,
                native_runtime=runtime,
                permission_mode=NativePermissionMode.INHERIT_NATIVE,
                effective_permission=runtime.native_profile.effective_authority(NativePermissionMode.INHERIT_NATIVE),
            ),
            sdk=Sdk(),
        )
        assert adapter.start_thread() == ThreadIdentity("thread-production")
        sdk_config = captured["sdk_config"]
        assert isinstance(sdk_config, dict)
        assert "launch_args_override" not in sdk_config
        assert sdk_config["env"] == {"CODEX_HOME": str(runtime.runtime_home)}
        assert sdk_config["cwd"] == str(workspace)
        assert (runtime.runtime_home / "config.toml").is_file()
        for name in ("memories", "plugins", "skills"):
            assert (runtime.runtime_home / name).is_symlink()
        thread_start = captured["thread_start"]
        assert isinstance(thread_start, dict)
        assert "sandbox" not in thread_start
        assert "approval_mode" not in thread_start
        adapter.close()


def _doctor_config_details(home: Path) -> dict[str, object]:
    import codex_cli_bin

    environment = {
        "CODEX_HOME": str(home),
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CODEX_LB_API_KEY": "test-only",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
    completed = subprocess.run(
        (str(codex_cli_bin.bundled_codex_path()), "doctor", "--json"),
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    report = json.loads(completed.stdout)
    return report["checks"]["config.load"]["details"]


def test_blank_private_home_selects_builtin_openai_but_projection_selects_codex_lb() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        blank = root / "blank"
        blank.mkdir(mode=0o700)
        assert _doctor_config_details(blank)["model provider"] == "openai"

        workspace, runtime = _test_native_runtime(root / "projected")
        runtime.prepare()
        projected = tomllib.loads((runtime.runtime_home / "config.toml").read_text())
        assert projected["model_provider"] == "codex-lb"
        assert projected["model_providers"]["codex-lb"] == {
            "base_url": "http://127.0.0.1:2455/backend-api/codex",
            "env_key": "CODEX_LB_API_KEY",
            "name": "openai",
            "requires_openai_auth": True,
            "supports_websockets": True,
            "wire_api": "responses",
        }
        assert workspace.is_dir()


def test_native_permission_change_is_inherited_by_the_next_runtime_without_global_mutation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        first = _test_native_profile(home)
        source_before = (home / "config.toml").read_bytes()
        runtime_one = NativeRuntimeConfig(root / "runtime-one", first)
        runtime_one.prepare()
        private_config = runtime_one.runtime_home / "config.toml"
        private_config.write_text(
            private_config.read_text() + '\n[projects."/runtime-added"]\ntrust_level = "trusted"\n'
        )
        runtime_one.prepare()
        assert private_config.read_text() == first.projected_toml
        assert first.effective_permissions(NativePermissionMode.INHERIT_NATIVE)["sandbox_mode"] == (
            "danger-full-access"
        )
        assert (home / "config.toml").read_bytes() == source_before

        updated = source_before.replace(b'sandbox_mode = "danger-full-access"', b'sandbox_mode = "read-only"')
        (home / "config.toml").write_bytes(updated)
        (home / "config.toml").chmod(0o600)
        second = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        NativeRuntimeConfig(root / "runtime-two", second).prepare()
        assert second.profile_sha256 != first.profile_sha256
        assert second.effective_permissions(NativePermissionMode.INHERIT_NATIVE)["sandbox_mode"] == "read-only"
        assert second.effective_permissions(NativePermissionMode.READ_ONLY)["monotonic"] is True
        with pytest.raises(ValueError):
            NativePermissionMode("danger_full_access")


def test_native_profile_rejects_secret_fields_symlinks_and_launch_time_mutation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        profile = _test_native_profile(home)
        config = home / "config.toml"
        original = config.read_text()

        config.write_text(original + '\n[mcp_servers.leaky.env]\nAPI_TOKEN = "must-not-project"\n')
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="secret-bearing"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})

        config.write_text(original.replace('personality = "pragmatic"', 'personality = "changed"'))
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="changed during launch"):
            NativeRuntimeConfig(root / "runtime", profile).prepare()

        real_config = root / "real-config.toml"
        real_config.write_text(original)
        real_config.chmod(0o600)
        config.unlink()
        config.symlink_to(real_config)
        with pytest.raises(NativeProfileError, match="opened safely"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


def test_runtime_home_rejects_symlinks_without_mutating_their_targets() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        profile = _test_native_profile(root / "native-home")
        external = root / "external"
        external.mkdir(mode=0o755)
        marker = external / "marker.txt"
        marker.write_text("unchanged\n")
        before_mode = os.lstat(external).st_mode

        runtime_home = root / "runtime-home"
        runtime_home.symlink_to(external, target_is_directory=True)
        with pytest.raises(NativeProfileError, match="unsafe ancestor"):
            NativeRuntimeConfig(runtime_home, profile).prepare()
        assert marker.read_text() == "unchanged\n"
        assert os.lstat(external).st_mode == before_mode

        ancestor_target = root / "ancestor-target"
        ancestor_target.mkdir(mode=0o755)
        ancestor_marker = ancestor_target / "marker.txt"
        ancestor_marker.write_text("unchanged\n")
        ancestor_mode = os.lstat(ancestor_target).st_mode
        (root / "runtime-parent").symlink_to(ancestor_target, target_is_directory=True)
        with pytest.raises(NativeProfileError, match="unsafe ancestor"):
            NativeRuntimeConfig(root / "runtime-parent" / "home", profile).prepare()
        assert ancestor_marker.read_text() == "unchanged\n"
        assert os.lstat(ancestor_target).st_mode == ancestor_mode
        assert not (ancestor_target / "home").exists()

        real_home = root / "real-home"
        real_home.mkdir(mode=0o700)
        external_config = root / "external-config.toml"
        external_config.write_text("unchanged\n")
        external_config.chmod(0o600)
        config_mode = os.lstat(external_config).st_mode
        (real_home / "config.toml").symlink_to(external_config)
        with pytest.raises(NativeProfileError, match="safe regular file"):
            NativeRuntimeConfig(real_home, profile).prepare()
        assert external_config.read_text() == "unchanged\n"
        assert os.lstat(external_config).st_mode == config_mode


def test_runtime_home_rejects_non_directories_and_creates_private_tree() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        profile = _test_native_profile(root / "native-home")
        substituted = root / "substituted"
        substituted.write_text("not a directory\n")
        before = substituted.read_bytes()
        with pytest.raises(NativeProfileError, match="unsafe ancestor"):
            NativeRuntimeConfig(substituted / "home", profile).prepare()
        assert substituted.read_bytes() == before

        device_before = os.stat("/dev/null")
        with pytest.raises(NativeProfileError, match="unsafe ancestor"):
            NativeRuntimeConfig(Path("/dev/null"), profile).prepare()
        device_after = os.stat("/dev/null")
        assert (device_after.st_mode, device_after.st_size, device_after.st_mtime_ns) == (
            device_before.st_mode,
            device_before.st_size,
            device_before.st_mtime_ns,
        )

        clean = root / "private" / "nested" / "home"
        NativeRuntimeConfig(clean, profile).prepare()
        assert stat.S_IMODE(os.lstat(clean).st_mode) == 0o700
        assert (clean / "config.toml").is_file()
        assert stat.S_IMODE(os.lstat(clean / "config.toml").st_mode) == 0o600


def test_capsule_read_only_mode_reaches_sdk_as_a_monotonic_native_restriction() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        profile = _test_native_profile(Path(directory) / "native-home")
        captured: list[CodexSdkConfig] = []

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            captured.append(config)
            return FakeAdapter(config, _service())

        controller = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=profile,
        )
        capsule = replace(
            _capsule(repository, repository, base, branch),
            permission_mode=NativePermissionMode.READ_ONLY,
        )
        controller.plan(capsule)
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        assert captured[0].permission_mode is NativePermissionMode.READ_ONLY
        assert captured[0].native_runtime is not None
        assert captured[0].native_runtime.native_profile.effective_permissions(NativePermissionMode.READ_ONLY) == {
            "mode": "read_only",
            "sandbox_mode": "read-only",
            "approval_policy": "never",
            "native_sandbox_mode": "danger-full-access",
            "monotonic": True,
        }
        controller.close()


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
        assert ledger.schema_identity == "codex_flow_h3_causal_workspace_v7"
        assert "checkpoint" in ledger.schema_columns("executions")
        ledger.close()


def test_v4_sandbox_authority_schema_migrates_to_truthful_native_profile_authority() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "workflow.db"
        connection = sqlite3.connect(path)
        for ddl in _V4_TABLE_DDL.values():
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "4"),
                ("migration_marker", "complete"),
                ("schema_identity", "codex_flow_h3_integrity_v4"),
            ),
        )
        connection.commit()
        connection.close()

        ledger = Ledger(path)
        assert ledger.schema_identity == "codex_flow_h3_causal_workspace_v7"
        assert "native_profile_sha256" in ledger.schema_columns("execution_integrity")
        assert "sandbox_policy_sha256" not in ledger.schema_columns("execution_integrity")
        ledger.close()


def test_v5_native_profile_schema_migrates_to_permission_authority_v6() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "workflow.db"
        connection = sqlite3.connect(path)
        for ddl in _V5_TABLE_DDL.values():
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "5"),
                ("migration_marker", "complete"),
                ("schema_identity", "codex_flow_h3_native_profile_v5"),
            ),
        )
        connection.commit()
        connection.close()

        ledger = Ledger(path)
        assert ledger.schema_version == CURRENT_SCHEMA_VERSION
        assert ledger.schema_identity == "codex_flow_h3_causal_workspace_v7"
        assert "native_compatibility_sha256" in ledger.schema_columns("execution_integrity")
        assert "effective_permission_json" in ledger.schema_columns("execution_integrity")
        ledger.close()


def test_v6_permission_schema_migrates_to_causal_workspace_v7() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "workflow.db"
        connection = sqlite3.connect(path)
        for ddl in _V6_TABLE_DDL.values():
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("schema_version", "6"),
                ("migration_marker", "complete"),
                ("schema_identity", "codex_flow_h3_permission_authority_v6"),
            ),
        )
        timestamp = "2026-08-24T00:00:00.000000Z"
        workspace = str(Path(directory) / "repo")
        permission = NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "read-only", "never")
        permission_json = json.dumps(permission.facts, sort_keys=True, separators=(",", ":"))
        permission_sha256 = hashlib.sha256(permission_json.encode()).hexdigest()
        connection.execute("INSERT INTO runs VALUES (?, ?, NULL, '{}')", ("run", timestamp))
        connection.execute(
            "INSERT INTO milestones VALUES (?, ?, 'COMPLETED', ?, ?, '{}')",
            ("run", "m1", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO dispatches VALUES (?, ?, ?, ?, ?, ?)",
            ("run/m1/executor/1", "run", "m1", "executor", 1, timestamp),
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
            (
                (
                    "run/m1/1",
                    "run",
                    "m1",
                    1,
                    "PLANNED",
                    "STARTING",
                    "dispatch_claimed",
                    "dispatch_claimed",
                    "run/m1/executor/1",
                    timestamp,
                ),
                (
                    "run/m1/2",
                    "run",
                    "m1",
                    2,
                    "STARTING",
                    "RUNNING",
                    "state_transition",
                    None,
                    None,
                    timestamp,
                ),
                (
                    "run/m1/3",
                    "run",
                    "m1",
                    3,
                    "RUNNING",
                    "COMPLETED",
                    "state_transition",
                    "terminal_outcome",
                    None,
                    timestamp,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO workspace_leases VALUES (?, ?, 'current_checkout', ?, ?, ?, ?, ?)",
            (workspace, workspace, "main", "a" * 40, "lane", "run", timestamp),
        )
        connection.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 'result_durable', ?, ?, ?, ?, ?, "
            "0, ?, ?, 0, 0.1, ?, ?, ?, ?)",
            (
                "run",
                "m1",
                f"{workspace}/capsule.json",
                "b" * 64,
                workspace,
                "gpt-test",
                "medium",
                "thread",
                "turn",
                '{"status":"done"}',
                '{"status":"done"}',
                '["true"]',
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "e" * 64,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO sdk_lifecycle_events VALUES (?, ?, ?, 0, ?, ?)",
            ("run", "m1", "turn", "turn/completed", "turn"),
        )
        connection.execute(
            "INSERT INTO execution_integrity VALUES (?, ?, 'controller_v3', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run",
                "m1",
                "f" * 64,
                "1" * 64,
                permission_json,
                permission_sha256,
                "2" * 64,
                "2" * 64,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        connection.close()

        ledger = Ledger(path)
        assert ledger.schema_version == CURRENT_SCHEMA_VERSION
        assert ledger.schema_identity == "codex_flow_h3_causal_workspace_v7"
        columns = ledger.schema_columns("execution_integrity")
        assert "workspace_baseline_sha256" in columns
        assert "turn_started_at" in columns
        integrity = ledger.get_execution_integrity("run", "m1")
        assert integrity.provenance == "legacy_permission_v6"
        assert integrity.turn_started_at == timestamp
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


def test_native_external_write_alone_is_not_reclassified_as_controller_failure() -> None:
    class NativeExternalAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd.parent / "native-authority.txt").write_text("allowed by native profile\n")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NativeExternalAdapter(config, _service()),
        )
        controller.plan(_capsule(repository, repository, base, branch))
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        assert (repository.parent / "native-authority.txt").read_text() == "allowed by native profile\n"
        controller.close()


def test_mutable_artifact_symlink_is_rejected_even_when_native_external_write_succeeds() -> None:
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


def test_mutable_artifact_hardlink_is_rejected_even_when_native_external_write_succeeds() -> None:
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
