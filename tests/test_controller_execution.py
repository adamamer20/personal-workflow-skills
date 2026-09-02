from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
import subprocess
import threading
import time
import tomllib
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import codex_flow.controller as controller_module
import codex_flow.native_profile as native_profile_module
from codex_flow.backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig, NativeRuntimeConfig
from codex_flow.cli import _detached_status_payload, app
from codex_flow.contracts import PluginRequirement, model_facing_result_schema
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
    load_capsule,
)
from codex_flow.domain import (
    ControllerCheckpoint,
    ExecutionCapsule,
    ExecutionRecord,
    ExecutionStatus,
    LifecycleEvent,
    LifecyclePhase,
    MilestoneId,
    NativePermissionAuthority,
    NativePermissionMode,
    ReasoningEffort,
    RunId,
    SkillInput,
    ThreadIdentity,
    TurnObservation,
    ValidationFailureCode,
    ValidationObservation,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
    validate_output_schema,
)
from codex_flow.ledger import (
    _EXECUTION_INTEGRITY_V7_DDL,
    _V2_TABLE_DDL,
    _V4_TABLE_DDL,
    _V5_TABLE_DDL,
    _V6_TABLE_DDL,
    CURRENT_SCHEMA_VERSION,
    CorruptSchemaError,
    Ledger,
    RecordNotFound,
    SchemaError,
    StaleWriter,
    WorkspaceLeaseConflict,
    _decode_json_object,
)
from codex_flow.native_profile import NativeProfileError, NativeProfileProjection
from codex_flow.plugin_capabilities import bundle_digest
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


def _nested_discovery_file(home: Path, surface: str, *, content: str = "alpha") -> Path:
    nested = home / surface / "demo" / "nested"
    nested.mkdir(parents=True)
    source = nested / "SOURCE.md"
    source.write_text(content)
    return source


def test_protected_path_digest_uses_git_relative_path_order_for_sibling_directories(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    (repository / "mocks" / "mock-api").mkdir(parents=True)
    (repository / "mocks" / "mock-api-client").mkdir(parents=True)
    (repository / "mocks" / "mock-api" / "fixture.txt").write_text("api\n")
    (repository / "mocks" / "mock-api-client" / "fixture.txt").write_text("client\n")
    _git(repository, "add", "mocks")
    _git(repository, "commit", "-qm", "add mock fixtures")
    base_sha = _git(repository, "rev-parse", "HEAD")

    paths = ("mocks",)
    assert controller_module.protected_paths_digest(repository, paths) == (
        controller_module.protected_paths_digest_at_revision(repository, base_sha, paths)
    )


def test_protected_path_digest_ignores_build_outputs_but_observes_tracked_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    frontend = repository / "frontend"
    frontend.mkdir()
    (frontend / ".gitignore").write_text(".next/\nnode_modules/\n")
    (frontend / "source.ts").write_text("export const value = 1;\n")
    _git(repository, "add", "frontend")
    _git(repository, "commit", "-qm", "add frontend")
    base_sha = _git(repository, "rev-parse", "HEAD")
    paths = ("frontend",)
    baseline = controller_module.protected_paths_digest_at_revision(repository, base_sha, paths)

    (frontend / ".next").mkdir()
    (frontend / ".next" / "build-manifest.json").write_text("{}\n")
    executable = frontend / "node_modules" / "package" / "bin.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("export {};\n")
    bin_directory = frontend / "node_modules" / ".bin"
    bin_directory.mkdir()
    (bin_directory / "package").symlink_to("../package/bin.js")

    assert controller_module.protected_paths_digest(repository, paths) == baseline

    (frontend / "source.ts").write_text("export const value = 2;\n")
    assert controller_module.protected_paths_digest(repository, paths) != baseline


def test_ignored_build_outputs_and_symlinks_are_excluded_from_preflight_scope() -> None:
    class BuildOutputAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            frontend = self.config.cwd / "frontend"
            (frontend / ".next").mkdir()
            (frontend / ".next" / "build-manifest.json").write_text("{}\n")
            executable = frontend / "node_modules" / "package" / "bin.js"
            executable.parent.mkdir(parents=True)
            executable.write_text("export {};\n")
            bin_directory = frontend / "node_modules" / ".bin"
            bin_directory.mkdir()
            (bin_directory / "package").symlink_to("../package/bin.js")
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        frontend = repository / "frontend"
        frontend.mkdir()
        (frontend / ".gitignore").write_text(".next/\nnode_modules/\n")
        (frontend / "source.ts").write_text("export const value = 1;\n")
        _git(repository, "add", "frontend")
        _git(repository, "commit", "-qm", "add frontend")
        base = _git(repository, "rev-parse", "HEAD")
        capsule = replace(_capsule(repository, repository, base, branch), protected_paths=("frontend",))
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: BuildOutputAdapter(config, _service()),
        )
        controller.plan(capsule)

        terminal = controller.start("run", "m1")

        assert terminal.status is ExecutionStatus.COMPLETED
        assert terminal.protected_after_sha256 == terminal.protected_before_sha256
        controller.close()


def test_preflight_ignores_existing_venv_symlink_and_build_directories() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        frontend = repository / "frontend"
        frontend.mkdir()
        (frontend / ".gitignore").write_text(".next/\nnode_modules/\n.venv/\n")
        (frontend / "source.ts").write_text("export const value = 1;\n")
        _git(repository, "add", "frontend")
        _git(repository, "commit", "-qm", "add frontend")
        base = _git(repository, "rev-parse", "HEAD")

        venv_bin = frontend / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(repository / "protected.txt")
        (frontend / ".next").mkdir()
        (frontend / ".next" / "manifest.json").write_text("{}\n")
        (frontend / "node_modules" / ".bin").mkdir(parents=True)
        (frontend / "node_modules" / ".bin" / "tool").symlink_to("../../source.ts")

        capsule = replace(_capsule(repository, repository, base, branch), protected_paths=("frontend",))
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        controller.plan(capsule)
        terminal = controller.start("run", "m1")

        assert terminal.status is ExecutionStatus.COMPLETED
        assert terminal.protected_after_sha256 == terminal.protected_before_sha256
        controller.close()


def _tamper_pre_external_authority(repository: Path, base: str, kind: str) -> None:
    if kind == "git_config":
        _git(repository, "config", "codex-flow.drift", "changed")
    elif kind == "index":
        _git(repository, "update-index", "--assume-unchanged", "source.txt")
    elif kind == "ref":
        _git(repository, "update-ref", "refs/codex-flow/drift", base)
    elif kind == "history":
        _git(repository, "commit", "--allow-empty", "-qm", "authority drift")
    elif kind == "out_of_scope_file":
        (repository / "unexpected.txt").write_text("drift\n")
    elif kind == "empty_directory":
        (repository / "unexpected-empty").mkdir()
    elif kind == "allowed_path":
        (repository / "result.txt").write_text("drift\n")
    else:  # pragma: no cover - table contract
        raise AssertionError(f"unknown tamper kind: {kind}")


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


def _plugin_profile(root: Path) -> tuple[NativeProfileProjection, Path, PluginRequirement]:
    profile = _test_native_profile(root)
    plugin = profile.source_home / "plugins" / "demo"
    (plugin / "skills" / "demo.skill").mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        '{"name":"demo","version":"1","skills":"skills","enabled":true}\n', encoding="utf-8"
    )
    with (profile.source_home / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write("\n[plugins.demo]\nenabled = true\n")
    profile = NativeProfileProjection.load(profile.source_home, environment={"CODEX_LB_API_KEY": "test-only"})
    requirement = PluginRequirement("demo", "1", "bundled", bundle_digest(plugin), ("demo.skill",), ())
    return profile, plugin, requirement


def test_direct_controller_passes_typed_skill_input_and_rejects_byte_drift_before_sdk_identity() -> None:
    class SkillCapturingAdapter(FakeAdapter):
        def run_turn(
            self, thread: ThreadIdentity, input: str | SkillInput, *, output_schema: Any = None
        ) -> TurnObservation:
            assert isinstance(input, SkillInput)
            assert input.name == "demo.skill"
            assert Path(input.path).is_dir()
            return super().run_turn(thread, input, output_schema=output_schema)  # type: ignore[arg-type]

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        profile, plugin, requirement = _plugin_profile(Path(directory) / "native-home")
        service = _service()
        capsule = replace(
            _capsule(repository, repository, base, branch),
            plugin_requirements=(requirement.to_json(),),
        )
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: SkillCapturingAdapter(config, service),
            _trusted_test_native_profile=profile,
        )
        controller.plan(capsule)
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        assert service["starts"] == service["turns"] == 1
        controller.close()

        repository = Path(directory) / "drift-repo"
        base, branch = _repository(repository)
        service = _service()
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=profile,
        )
        controller.plan(
            replace(_capsule(repository, repository, base, branch), plugin_requirements=(requirement.to_json(),))
        )
        (plugin / "skill-byte.txt").write_text("drift\n", encoding="utf-8")
        with pytest.raises((ControllerError, native_profile_module.NativeDiscoveryCompatibilityError)):
            controller.start("run", "m1")
        assert service["starts"] == service["turns"] == 0
        controller.close()


def _process_start_contender(
    repository: str,
    run_id: str,
    milestone_id: str,
    delay: float,
    gate: Any,
    results: Any,
) -> None:
    service = _service()
    controller = Controller(Path(repository), _trusted_test_adapter_factory=_factory(service))
    try:
        gate.wait(10)
        time.sleep(delay)
        record = controller.start(run_id, milestone_id)
        results.put(("completed", str(record.run_id), service))
    except WorkspaceLeaseConflict:
        results.put(("conflict", run_id, service))
    except Exception as exc:  # pragma: no cover - diagnostic for process-test failures
        results.put(("error", type(exc).__name__, str(exc), service))
    finally:
        controller.close()


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


def test_completed_execution_reopens_through_integrated_review_and_acceptance() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        controller.plan(_capsule(repository, repository, base, branch))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.COMPLETED

        controller.ledger.record_review_transition(
            "run",
            "m1",
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="integrated_review_started",
            data={"modes": ["objective", "architecture"]},
        )
        controller.close()

        reviewing = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        assert reviewing.status("run", "m1").status is ExecutionStatus.COMPLETED
        assert reviewing.ledger.current_state("run", "m1") is WorkflowState.REVIEWING
        reviewing.ledger.record_review_transition(
            "run",
            "m1",
            WorkflowState.ACCEPTED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.ACCEPTANCE,
            kind="integrated_review_accepted",
            data={"P0": 0, "P1": 0},
        )
        reviewing.close()

        accepted = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        assert accepted.status("run", "m1").status is ExecutionStatus.COMPLETED
        assert accepted.ledger.current_state("run", "m1") is WorkflowState.ACCEPTED
        accepted.close()


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


@pytest.mark.parametrize("surface", ["skills", "plugins", "memories"])
def test_fresh_resume_rejects_nested_discovery_drift_before_adapter_creation(surface: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        _test_native_profile(home)
        source = _nested_discovery_file(home, surface)
        initial = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_turn":
                raise RuntimeError("injected post-turn crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=initial,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="post-turn crash"):
            first.start("run", "m1")
        assert first.status("run", "m1").turn_id == "turn-stable"
        first.close()

        source.write_text("omega")
        changed = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        second = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=changed,
        )
        with pytest.raises(UnsafeResumeCompatibilityChange, match="compatibility changed"):
            second.resume("run", "m1")
        assert adapter_creations == 0
        assert service == {"starts": 1, "resumes": 0, "turns": 1, "closes": 1}
        second.close()


def test_schema_v7_reopen_of_prior_discovery_fingerprint_fails_closed() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        current = _test_native_profile(root / "native-home")
        prior_algorithm = replace(current, compatibility_sha256="f" * 64)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("injected crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=prior_algorithm,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="injected crash"):
            first.start("run", "m1")
        first.close()

        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        reopened = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=current,
        )
        with pytest.raises(UnsafeResumeCompatibilityChange, match="compatibility changed"):
            reopened.resume("run", "m1")
        assert adapter_creations == 0
        assert service["resumes"] == 0
        reopened.close()


def test_resume_verifies_the_original_discovery_snapshot_before_adapter_creation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        _test_native_profile(home)
        source = _nested_discovery_file(home, "skills")
        captured = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_turn":
                raise RuntimeError("injected post-turn crash")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            _trusted_test_native_profile=captured,
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="post-turn crash"):
            first.start("run", "m1")
        first.close()

        source.write_text("omega")
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        reopened = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=captured,
        )
        with pytest.raises(UnsafeResumeCompatibilityChange, match="discovery surface changed"):
            reopened.resume("run", "m1")
        assert adapter_creations == 0
        assert service["resumes"] == 0
        reopened.close()


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


@pytest.mark.parametrize(
    "tamper",
    ("git_config", "index", "ref", "history", "out_of_scope_file", "empty_directory", "allowed_path"),
)
def test_restart_from_durable_baseline_rejects_exact_authority_drift_before_adapter(tamper: str) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        def crash(stage: str) -> None:
            if stage == "after_workspace_baseline":
                raise RuntimeError("baseline crash")

        first = Controller(repository, _trusted_test_adapter_factory=factory, fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="baseline crash"):
            first.start("run", "m1")
        durable = first.status("run", "m1")
        integrity = first.ledger.get_execution_integrity("run", "m1")
        assert durable.checkpoint is ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE
        assert integrity.git_authority_before_sha256 is not None
        assert adapter_creations == 0
        first.close()

        _tamper_pre_external_authority(repository, base, tamper)
        reopened = Controller(repository, _trusted_test_adapter_factory=factory)
        with pytest.raises(ControllerError):
            reopened.start("run", "m1")
        assert adapter_creations == 0
        assert reopened.status("run", "m1").checkpoint is ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE
        assert service == {"starts": 0, "resumes": 0, "turns": 0, "closes": 0}
        reopened.close()


@pytest.mark.parametrize("tamper", ("git_config", "out_of_scope_file", "empty_directory", "allowed_path"))
def test_same_process_pre_external_authorization_is_race_aware_and_fail_closed(tamper: str) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, _service())

        def mutate_after_baseline(stage: str) -> None:
            if stage == "after_workspace_baseline":
                _tamper_pre_external_authority(repository, base, tamper)

        controller = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            fault_injector=mutate_after_baseline,
        )
        controller.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(ControllerError):
            controller.start("run", "m1")
        assert adapter_creations == 0
        assert controller.status("run", "m1").checkpoint is ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE
        controller.close()


@pytest.mark.parametrize(
    "tamper",
    ("git_config", "index", "ref", "history", "out_of_scope_file", "empty_directory", "allowed_path"),
)
def test_fresh_resume_rejects_exact_authority_drift_before_adapter_creation(tamper: str) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("identity crash")

        first = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="identity crash"):
            first.start("run", "m1")
        assert first.status("run", "m1").status is ExecutionStatus.THREAD_STARTED
        first.close()

        _tamper_pre_external_authority(repository, base, tamper)
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        reopened = Controller(repository, _trusted_test_adapter_factory=factory)
        with pytest.raises(ControllerError):
            reopened.resume("run", "m1")
        assert adapter_creations == 0
        assert service == {"starts": 1, "resumes": 0, "turns": 0, "closes": 1}
        assert reopened.status("run", "m1").status is ExecutionStatus.THREAD_STARTED
        reopened.close()


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


@pytest.mark.parametrize("alias_kind", ("dot_parent", "symlink", "relative", "trailing_dot"))
def test_physical_workspace_alias_cannot_acquire_a_second_nonterminal_owner(alias_kind: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        if alias_kind == "dot_parent":
            alias = Path(f"{repository}/../repo")
        elif alias_kind == "symlink":
            alias = root / "repo-alias"
            alias.symlink_to(repository, target_is_directory=True)
        elif alias_kind == "relative":
            alias = Path(os.path.relpath(repository, Path.cwd()))
        else:
            alias = Path(f"{repository}/.")
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("hold nonterminal owner")

        first = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            fault_injector=crash,
        )
        first.plan(_capsule(repository, repository, base, branch, run="owner-one", milestone="one"))
        with pytest.raises(RuntimeError, match="hold nonterminal owner"):
            first.start("owner-one", "one")
        first.close()

        second_adapter_creations = 0

        def second_factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal second_adapter_creations
            second_adapter_creations += 1
            return FakeAdapter(config, service)

        second = Controller(alias, _trusted_test_adapter_factory=second_factory)
        alias_capsule = _capsule(alias, alias, base, branch, run="owner-two", milestone="two")
        second.plan(alias_capsule)
        with pytest.raises(WorkspaceLeaseConflict, match=r"nonterminal execution owner|different facts"):
            second.start("owner-two", "two")
        assert second_adapter_creations == 0
        assert service["starts"] == 1
        assert second.status("owner-two", "two").workspace_path == repository.resolve()
        assert second.ledger.get_workspace_lease(alias).owner_run_id == RunId("owner-one")
        second.close()


def test_canonical_execution_reopens_through_symlink_alias_without_second_thread_start() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        alias = root / "repo-alias"
        alias.symlink_to(repository, target_is_directory=True)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("close and reopen")

        first = Controller(alias, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        first.plan(_capsule(alias, alias, base, branch))
        with pytest.raises(RuntimeError, match="close and reopen"):
            first.start("run", "m1")
        assert first.status("run", "m1").workspace_path == repository.resolve()
        first.close()

        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        assert reopened.resume("run", "m1").status is ExecutionStatus.COMPLETED
        assert service["starts"] == 1
        assert service["resumes"] == 1
        assert len(reopened.ledger.snapshot("run").dispatches) == 1
        reopened.close()


@pytest.mark.parametrize("round_index", range(8))
def test_two_preplanned_alias_contenders_have_one_atomic_lease_winner(round_index: int) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        alias = root / "repo-alias"
        alias.symlink_to(repository, target_is_directory=True)
        services = (_service(), _service())
        controllers = (
            Controller(repository, _trusted_test_adapter_factory=_factory(services[0])),
            Controller(alias, _trusted_test_adapter_factory=_factory(services[1])),
        )
        capsules = (
            _capsule(repository, repository, base, branch, run="owner-one", milestone="one", lane="lane-one"),
            _capsule(alias, alias, base, branch, run="owner-two", milestone="two", lane="lane-two"),
        )
        controllers[0].plan(capsules[0])
        controllers[1].plan(capsules[1])
        barrier = threading.Barrier(2)

        def start(index: int) -> Any:
            barrier.wait()
            try:
                return controllers[index].start(capsules[index].run_id, capsules[index].milestone_id)
            except Exception as exc:
                return exc

        order = (0, 1) if round_index % 2 == 0 else (1, 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, index) for index in order]
            _done, not_done = wait(futures, timeout=60)
            assert not not_done, "workspace lease contenders did not converge within the bounded deadline"
            outcomes = [future.result() for future in futures]
        records = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, WorkspaceLeaseConflict)]
        assert len(records) == 1 and records[0].status is ExecutionStatus.COMPLETED
        assert len(conflicts) == 1
        assert sum(service["starts"] for service in services) == 1
        assert sum(service["turns"] for service in services) == 1
        lease = controllers[0].ledger.get_workspace_lease(repository)
        assert lease.owner_run_id == records[0].run_id
        loser = capsules[0] if records[0].run_id == capsules[1].run_id else capsules[1]
        assert (
            controllers[0].status(loser.run_id, loser.milestone_id).checkpoint is ControllerCheckpoint.CAPSULE_PLANNED
        )
        for controller in controllers:
            controller.close()
        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        assert reopened.status(records[0].run_id, records[0].milestone_id).status is ExecutionStatus.COMPLETED
        assert reopened.ledger.get_workspace_lease(repository).owner_run_id == records[0].run_id
        reopened.close()


@pytest.mark.parametrize("round_index", range(4))
def test_two_preplanned_alias_contenders_have_one_process_level_lease_winner(round_index: int) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        alias = root / "repo-alias"
        alias.symlink_to(repository, target_is_directory=True)
        planner = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsules = (
            _capsule(repository, repository, base, branch, run="process-one", milestone="one", lane="process-one"),
            _capsule(alias, alias, base, branch, run="process-two", milestone="two", lane="process-two"),
        )
        for capsule in capsules:
            planner.plan(capsule)
        planner.close()

        context = multiprocessing.get_context("fork")
        gate = context.Event()
        results = context.Queue()
        delays = (0.0, 0.05) if round_index % 2 == 0 else (0.05, 0.0)
        processes = [
            context.Process(
                target=_process_start_contender,
                args=(
                    os.fspath(repository if index == 0 else alias),
                    str(capsule.run_id),
                    str(capsule.milestone_id),
                    delays[index],
                    gate,
                    results,
                ),
            )
            for index, capsule in enumerate(capsules)
        ]
        try:
            for process in processes:
                process.start()
            gate.set()
            for process in processes:
                process.join(20)
            assert all(not process.is_alive() and process.exitcode == 0 for process in processes)
            outcomes = [results.get(timeout=2) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()
        assert sorted(outcome[0] for outcome in outcomes) == ["completed", "conflict"]
        assert sum(outcome[-1]["starts"] for outcome in outcomes) == 1
        assert sum(outcome[-1]["turns"] for outcome in outcomes) == 1
        completed_run = next(outcome[1] for outcome in outcomes if outcome[0] == "completed")
        loser_capsule = capsules[0] if completed_run == str(capsules[1].run_id) else capsules[1]
        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        assert reopened.ledger.get_workspace_lease(repository).owner_run_id == RunId(completed_run)
        assert (
            reopened.status(loser_capsule.run_id, loser_capsule.milestone_id).checkpoint
            is ControllerCheckpoint.CAPSULE_PLANNED
        )
        reopened.close()


def test_atomic_lease_winner_survives_identity_crash_and_loser_cannot_strand_it() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        alias = root / "repo-alias"
        alias.symlink_to(repository, target_is_directory=True)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("identity crash")

        winner = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        loser_creations = 0

        def loser_factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal loser_creations
            loser_creations += 1
            return FakeAdapter(config, service)

        loser = Controller(alias, _trusted_test_adapter_factory=loser_factory)
        winner_capsule = _capsule(repository, repository, base, branch, run="winner", milestone="one", lane="winner")
        loser_capsule = _capsule(alias, alias, base, branch, run="loser", milestone="two", lane="loser")
        winner.plan(winner_capsule)
        loser.plan(loser_capsule)
        with pytest.raises(RuntimeError, match="identity crash"):
            winner.start("winner", "one")
        with pytest.raises(WorkspaceLeaseConflict):
            loser.start("loser", "two")
        assert loser_creations == 0
        assert winner.status("winner", "one").status is ExecutionStatus.THREAD_STARTED
        winner.close()
        loser.close()

        reopened = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        assert reopened.resume("winner", "one").status is ExecutionStatus.COMPLETED
        assert service == {"starts": 1, "resumes": 1, "turns": 1, "closes": 2}
        assert reopened.ledger.get_workspace_lease(repository).owner_run_id == RunId("winner")
        reopened.close()


def test_noncanonical_legacy_workspace_key_fails_closed_on_reopen() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()

        def crash(stage: str) -> None:
            if stage == "after_thread_identity":
                raise RuntimeError("persist lease")

        controller = Controller(
            repository,
            _trusted_test_adapter_factory=_factory(service),
            fault_injector=crash,
        )
        controller.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(RuntimeError, match="persist lease"):
            controller.start("run", "m1")
        controller.close()

        legacy_alias = f"{repository}/../repo"
        connection = sqlite3.connect(repository / ".codex-flow" / "workflow.db")
        connection.execute("UPDATE workspace_leases SET workspace_path = ?", (legacy_alias,))
        connection.execute("UPDATE executions SET workspace_path = ?", (legacy_alias,))
        connection.commit()
        connection.close()

        with pytest.raises(CorruptSchemaError, match="canonical physical path"):
            Controller(repository, _trusted_test_adapter_factory=_factory(service))


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


@pytest.mark.parametrize(
    "tamper",
    ("content", "delete", "type", "symlink", "hardlink", "git_state"),
)
def test_fresh_successor_rejects_predecessor_terminal_file_or_git_tampering(tamper: str) -> None:
    service: dict[str, Any] = {**_service(), "target": "first.txt"}

    class NamedOutputAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            target = str(self.service["target"])
            (self.config.cwd / target).write_text("trusted\n")
            return TurnObservation(thread, f"turn-{target}", "completed", None, {"status": "done"}, ())

    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, _branch = _repository(repository)
        lane = f"predecessor-{tamper.replace('_', '-')}"
        workspace = repository.parent / "repo.worktrees" / lane
        first = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: NamedOutputAdapter(config, service),
        )
        capsule_one = replace(
            _capsule(
                repository,
                workspace,
                base,
                f"agent/{lane}",
                run="predecessor-run",
                milestone="one",
                mode=WorkspaceMode.MANAGED_WORKTREE,
                lane=lane,
            ),
            mutable_paths=("first.txt",),
        )
        first.plan(capsule_one)
        assert first.start("predecessor-run", "one").status is ExecutionStatus.COMPLETED
        accepted = first.ledger.get_execution_integrity("predecessor-run", "one")
        assert accepted.workspace_terminal is not None
        first.close()

        output = workspace / "first.txt"
        if tamper == "content":
            output.write_text("tampered\n")
        elif tamper == "delete":
            output.unlink()
        elif tamper == "type":
            output.unlink()
            output.mkdir()
        elif tamper == "symlink":
            output.unlink()
            output.symlink_to(root / "outside.txt")
        elif tamper == "hardlink":
            os.link(output, workspace / "linked-first.txt")
        else:
            _git(workspace, "config", "codex-flow.tampered", "yes")

        adapter_creations = 0

        def successor_factory(config: CodexSdkConfig) -> NamedOutputAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return NamedOutputAdapter(config, service)

        successor = Controller(repository, _trusted_test_adapter_factory=successor_factory)
        capsule_two = replace(
            capsule_one,
            milestone_id=MilestoneId("two"),
            mutable_paths=("second.txt",),
        )
        successor.plan(capsule_two)
        service["target"] = "second.txt"
        with pytest.raises(ControllerError):
            successor.start("predecessor-run", "two")
        assert adapter_creations == 0
        assert service["starts"] == 1
        assert successor.status("predecessor-run", "two").status is ExecutionStatus.PLANNED
        successor.close()


@pytest.mark.parametrize("tamper", ("add_empty", "delete_empty"))
def test_fresh_successor_rejects_predecessor_terminal_empty_directory_tampering(tamper: str) -> None:
    service = _service()

    class EmptyOutputAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            if "first" in input:
                (self.config.cwd / "owned-directory").mkdir()
                turn_id = "turn-first"
            else:
                (self.config.cwd / "second.txt").write_text("second\n")
                turn_id = "turn-second"
            return TurnObservation(thread, turn_id, "completed", None, {"status": "done"}, ())

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        lane = f"empty-terminal-{tamper.replace('_', '-')}"
        workspace = repository.parent / "repo.worktrees" / lane
        first_capsule = replace(
            _capsule(
                repository,
                workspace,
                base,
                f"agent/{lane}",
                run="empty-terminal-run",
                milestone="one",
                mode=WorkspaceMode.MANAGED_WORKTREE,
                lane=lane,
            ),
            mutable_paths=("owned-directory",),
            prompt="first milestone",
        )
        first = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: EmptyOutputAdapter(config, service),
        )
        first.plan(first_capsule)
        assert first.start("empty-terminal-run", "one").status is ExecutionStatus.COMPLETED
        first.close()

        owned = workspace / "owned-directory"
        if tamper == "add_empty":
            (owned / "nested-empty").mkdir()
        else:
            owned.rmdir()

        adapter_creations = 0

        def successor_factory(config: CodexSdkConfig) -> EmptyOutputAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return EmptyOutputAdapter(config, service)

        successor = Controller(repository, _trusted_test_adapter_factory=successor_factory)
        successor.plan(
            replace(
                first_capsule,
                milestone_id=MilestoneId("two"),
                mutable_paths=("second.txt",),
                prompt="second milestone",
            )
        )
        with pytest.raises(ControllerError, match="predecessor"):
            successor.start("empty-terminal-run", "two")
        assert adapter_creations == 0
        successor.close()


@pytest.mark.parametrize("boundary", ("after_workspace_baseline", "after_thread_identity"))
def test_reopened_successor_revalidates_predecessor_root_before_every_external_boundary(boundary: str) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        service = _service()
        first = Controller(repository, _trusted_test_adapter_factory=_factory(service))
        first.plan(
            _capsule(
                repository,
                repository,
                base,
                branch,
                run="predecessor-run",
                milestone="one",
                lane="predecessor-lane",
            )
        )
        assert first.start("predecessor-run", "one").status is ExecutionStatus.COMPLETED
        first.close()

        def crash(stage: str) -> None:
            if stage == boundary:
                raise RuntimeError("successor crash")

        successor = Controller(repository, _trusted_test_adapter_factory=_factory(service), fault_injector=crash)
        successor.plan(
            _capsule(
                repository,
                repository,
                base,
                branch,
                run="predecessor-run",
                milestone="two",
                lane="predecessor-lane",
            )
        )
        with pytest.raises(RuntimeError, match="successor crash"):
            successor.start("predecessor-run", "two")
        checkpoint = successor.status("predecessor-run", "two")
        assert checkpoint.checkpoint in {
            ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE,
            ControllerCheckpoint.THREAD_IDENTITY_DURABLE,
        }
        successor.close()

        (repository / "result.txt").write_text("tampered predecessor root\n")
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, service)

        reopened = Controller(repository, _trusted_test_adapter_factory=factory)
        operation = reopened.start if boundary == "after_workspace_baseline" else reopened.resume
        with pytest.raises(ControllerError, match=r"predecessor|baseline"):
            operation("predecessor-run", "two")
        assert adapter_creations == 0
        reopened.close()


@pytest.mark.parametrize("mode", (WorkspaceMode.MANAGED_WORKTREE, WorkspaceMode.EXISTING_WORKTREE))
def test_distinct_worktree_codex_flow_write_is_not_exempt_from_mutation_scope(mode: WorkspaceMode) -> None:
    class WorkspaceStateAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / ".codex-flow").mkdir()
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


@pytest.mark.parametrize("mode", (WorkspaceMode.MANAGED_WORKTREE, WorkspaceMode.EXISTING_WORKTREE))
def test_distinct_worktree_out_of_scope_empty_directory_fails_closed(mode: WorkspaceMode) -> None:
    class EmptyDirectoryAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            observation = super().run_turn(thread, input, output_schema=output_schema)
            assert self.config.cwd is not None
            (self.config.cwd / "outside-empty").mkdir()
            return observation

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        lane = f"empty-{mode.value.replace('_', '-')}"
        workspace = repository.parent / "repo.worktrees" / lane
        if mode is WorkspaceMode.EXISTING_WORKTREE:
            workspace.parent.mkdir()
            _git(repository, "worktree", "add", "-b", f"agent/{lane}", str(workspace), base)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: EmptyDirectoryAdapter(config, _service()),
        )
        controller.plan(_capsule(repository, workspace, base, f"agent/{lane}", mode=mode, lane=lane))
        terminal = controller.start("run", "m1")
        assert terminal.status is ExecutionStatus.FAILED
        assert terminal.result == {"status": "failed", "reason": "mutation_outside_owned_paths"}
        controller.close()


def test_empty_directory_topology_remains_usable_inside_mutable_root() -> None:
    class OwnedTopologyAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            nested = self.config.cwd / "owned" / "nested"
            nested.mkdir(parents=True)
            nested.rename(nested.with_name("renamed"))
            return TurnObservation(thread, "turn-owned", "completed", None, {"status": "done"}, ())

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: OwnedTopologyAdapter(config, _service()),
        )
        capsule = replace(_capsule(repository, repository, base, branch), mutable_paths=("owned",))
        controller.plan(capsule)
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        assert (repository / "owned" / "renamed").is_dir()
        controller.close()


def test_workspace_directory_topology_bound_fails_before_adapter_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, _service())

        controller = Controller(repository, _trusted_test_adapter_factory=factory)
        controller.plan(_capsule(repository, repository, base, branch))
        monkeypatch.setattr(controller_module, "_MAX_WORKSPACE_SCAN_ENTRIES", 1)
        with pytest.raises(ControllerError, match="topology exceeds the entry limit"):
            controller.start("run", "m1")
        assert adapter_creations == 0
        controller.close()


@pytest.mark.parametrize("mode", (WorkspaceMode.MANAGED_WORKTREE, WorkspaceMode.EXISTING_WORKTREE))
@pytest.mark.parametrize("operation", ("remove", "rename"))
def test_successor_turn_rejects_predecessor_empty_directory_removal_or_rename(
    mode: WorkspaceMode,
    operation: str,
) -> None:
    service: dict[str, Any] = {**_service(), "operation": "create"}

    class SequentialTopologyAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            owned = self.config.cwd / "owned-empty"
            if self.service["operation"] == "create":
                owned.mkdir()
                turn_id = "turn-create"
            else:
                if self.service["operation"] == "remove":
                    owned.rmdir()
                else:
                    owned.rename(self.config.cwd / "renamed-empty")
                (self.config.cwd / "result.txt").write_text("second\n")
                turn_id = "turn-second"
            return TurnObservation(thread, turn_id, "completed", None, {"status": "done"}, ())

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, _branch = _repository(repository)
        lane = f"topology-{mode.value.replace('_', '-')}-{operation}"
        workspace = repository.parent / "repo.worktrees" / lane
        if mode is WorkspaceMode.EXISTING_WORKTREE:
            workspace.parent.mkdir()
            _git(repository, "worktree", "add", "-b", f"agent/{lane}", str(workspace), base)
        controller = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: SequentialTopologyAdapter(config, service),
        )
        first = replace(
            _capsule(
                repository,
                workspace,
                base,
                f"agent/{lane}",
                run="topology-run",
                milestone="one",
                mode=mode,
                lane=lane,
            ),
            mutable_paths=("owned-empty",),
        )
        controller.plan(first)
        assert controller.start("topology-run", "one").status is ExecutionStatus.COMPLETED
        second = replace(first, milestone_id=MilestoneId("two"), mutable_paths=("result.txt",))
        controller.plan(second)
        service["operation"] = operation
        terminal = controller.start("topology-run", "two")
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


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "required": []},
        {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok", "ok"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "nested": {"type": "object", "properties": {}, "required": []},
            },
            "required": ["nested"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
            "description": "unsupported",
        },
    ],
)
def test_capsule_rejects_open_optional_or_ambiguous_output_schema_before_planning(schema: dict[str, Any]) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        with pytest.raises(ValueError):
            replace(_capsule(repository, repository, base, branch), output_schema=schema)
        assert not (repository / ".codex-flow").exists()


def test_capsule_accepts_valid_strict_nested_object_and_array_schema() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "ok": {"type": "boolean"}},
                        "required": ["name", "ok"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        capsule = replace(_capsule(repository, repository, base, branch), output_schema=schema)
        assert capsule.output_schema == schema


def test_capsule_preserves_exact_canonical_result_schema_before_planning() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        schema = model_facing_result_schema()

        capsule = replace(_capsule(repository, repository, base, branch), output_schema=schema)

        assert capsule.output_schema == schema
        assert capsule.output_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert capsule.output_schema["properties"]["status"]["enum"] == [
            "completed",
            "needs_decision",
            "external_blocked",
            "failed",
        ]
        assert capsule.output_schema["properties"]["validations"]["maxItems"] == 128
        assert not (repository / ".codex-flow").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda schema: schema.update({"anyOf": []}),
        lambda schema: schema.update({"maxProperties": "128"}),
        lambda schema: schema["properties"]["summary"].update({"pattern": 7}),
        lambda schema: schema["properties"]["validations"].update({"uniqueItems": "yes"}),
        lambda schema: schema["properties"]["status"].update({"enum": ["completed", "completed"]}),
        lambda schema: schema["properties"]["summary"].update({"const": False}),
        lambda schema: schema.update({"additionalProperties": True}),
    ],
)
def test_canonical_schema_keyword_drift_fails_before_controller_state(mutation) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        schema = json.loads(json.dumps(model_facing_result_schema()))
        mutation(schema)

        with pytest.raises(ValueError):
            replace(_capsule(repository, repository, base, branch), output_schema=schema)
        assert not (repository / ".codex-flow").exists()


def test_bounded_semantic_map_schema_is_validated_recursively() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://personal-workflow-skills.invalid/schemas/semantic-map.json",
        "title": "Semantic map",
        "description": "Bounded recursive metadata and names.",
        "type": "object",
        "minProperties": 0,
        "maxProperties": 4,
        "propertyNames": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^semantic-"},
        "additionalProperties": {"type": "string", "const": "owned"},
    }

    validate_output_schema(schema)


def test_named_record_bounds_never_make_declared_properties_optional() -> None:
    schema = {
        "type": "object",
        "properties": {"required_value": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
        "minProperties": 0,
        "maxProperties": 1,
    }

    with pytest.raises(ValueError, match="require every declared property"):
        validate_output_schema(schema)


def test_schema_enum_uses_json_instance_equality_for_numeric_values() -> None:
    duplicate_numeric_enum = {
        "type": "object",
        "properties": {"value": {"type": "number", "enum": [1, 1.0]}},
        "required": ["value"],
        "additionalProperties": False,
    }
    boolean_and_number_enum = {
        "type": "object",
        "properties": {"value": {"type": ["boolean", "number"], "enum": [True, 1]}},
        "required": ["value"],
        "additionalProperties": False,
    }

    with pytest.raises(ValueError, match="unique values"):
        validate_output_schema(duplicate_numeric_enum)
    validate_output_schema(boolean_and_number_enum)


def test_capsule_detaches_nested_schema_aliases_and_keeps_digest_stable() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["nested", "items"],
            "additionalProperties": False,
        }
        capsule = replace(_capsule(repository, repository, base, branch), output_schema=schema)
        before = json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True, allow_nan=False)

        # Mutating the caller-owned source tree cannot alter the capsule.
        schema["properties"]["nested"]["properties"]["name"]["type"] = "boolean"
        schema["properties"]["items"]["items"] = {"type": "boolean"}
        schema["required"].append("later")
        assert json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True, allow_nan=False) == before

        with pytest.raises(TypeError):
            capsule.output_schema["properties"] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            capsule.output_schema["properties"]["nested"]["properties"]["name"] = {}  # type: ignore[index]
        with pytest.raises((TypeError, AttributeError)):
            capsule.output_schema["required"].append("later")  # type: ignore[attr-defined]


class _StatefulItemsMapping(Mapping[str, Any]):
    """Expose a valid first item snapshot and a different later view."""

    def __init__(self, first: dict[str, Any], later: dict[str, Any]) -> None:
        self._first = first
        self._later = later
        self.items_calls = 0

    def __getitem__(self, key: str) -> Any:
        return self._first[key]

    def __iter__(self):
        return iter(self._first)

    def __len__(self) -> int:
        return len(self._first)

    def items(self):
        self.items_calls += 1
        return tuple((self._first if self.items_calls == 1 else self._later).items())


class _StatefulRequiredSequence(Sequence[str]):
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)


class _DuplicateItemsMapping(_StatefulItemsMapping):
    def items(self):
        self.items_calls += 1
        child = {"type": "string"}
        return (("status", child), ("status", child))


def test_capsule_freezes_one_owned_snapshot_before_schema_validation() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        properties = _StatefulItemsMapping(
            {"status": {"type": "string"}},
            {"status": {"type": "boolean"}, "forged": {"type": "string"}},
        )
        schema = {
            "type": "object",
            "properties": properties,
            "required": _StatefulRequiredSequence(["status"]),
            "additionalProperties": False,
        }
        capsule = replace(_capsule(repository, repository, base, branch), output_schema=schema)
        assert properties.items_calls == 1
        assert capsule.output_schema["properties"]["status"]["type"] == "string"


def test_capsule_rejects_stateful_mapping_that_changes_before_owned_snapshot() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        properties = _DuplicateItemsMapping({"status": {"type": "string"}}, {})
        schema = {
            "type": "object",
            "properties": properties,
            "required": [],
            "additionalProperties": False,
        }
        with pytest.raises(ValueError):
            replace(_capsule(repository, repository, base, branch), output_schema=schema)


def test_plan_revalidates_counterfeit_schema_before_any_ledger_write() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(_service()))
        capsule = _capsule(repository, repository, base, branch)
        before = controller.ledger.path.read_bytes()
        object.__setattr__(
            capsule,
            "output_schema",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
        )
        with pytest.raises(ValueError):
            controller.plan(capsule)
        assert controller.ledger.path.read_bytes() == before
        with pytest.raises(RecordNotFound):
            controller.ledger.get_execution("run", "m1")
        controller.close()


@pytest.mark.parametrize(
    "encoding",
    ("utf-16", "utf-32"),
)
def test_load_capsule_rejects_non_utf8_bom_encodings(encoding: str) -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        capsule = replace(_capsule(repository, repository, base, branch), prompt="résumé")
        path = repository / "capsule.json"
        payload = json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True).encode(encoding)
        path.write_bytes(payload)
        with pytest.raises(ControllerError, match="not valid JSON"):
            load_capsule(path)


def test_load_capsule_accepts_strict_utf8_non_ascii_and_ledger_rejects_bom_text() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        capsule = replace(_capsule(repository, repository, base, branch), prompt="résumé")
        path = repository / "capsule.json"
        path.write_bytes(json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        loaded, _digest = load_capsule(path)
        assert loaded.prompt == "résumé"

        with pytest.raises(SchemaError, match="not valid JSON"):
            _decode_json_object("\ufeff{}", field_name="event data")


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
        profile = controller.ledger.record_native_profile(
            "run", "m1", "0" * 64, "1" * 64, authority, base, (), "2" * 64
        )
        assert (
            controller.ledger.record_native_profile("run", "m1", "0" * 64, "1" * 64, authority, base, (), "2" * 64)
            == profile
        )
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
        controller.ledger.record_native_profile("run", "m1", "0" * 64, "1" * 64, authority, base, (), "2" * 64)
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
                    git_before_sha256,
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
                    workspace_terminal_head_sha=(
                        integrity.workspace_terminal_head_sha if integrity is not None else None
                    ),
                    workspace_terminal=(integrity.workspace_terminal if integrity is not None else None),
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


def test_detached_status_projects_terminal_queue_result() -> None:
    raw_result = json.dumps(
        {
            "schema_version": 1,
            "status": "completed",
            "summary": "bounded worker completed",
            "changed_surfaces": ["source.txt"],
            "validations": [{"name": "diff", "passed": True, "evidence": "clean"}],
            "durable_status": "completed",
            "next_action": None,
        },
        separators=(",", ":"),
    )
    record = ExecutionRecord(
        RunId("run"),
        MilestoneId("m1"),
        Path("/tmp/workspace"),
        Path("/tmp/capsule.json"),
        "1" * 64,
        "gpt-test",
        ReasoningEffort.MEDIUM,
        ExecutionStatus.PLANNED,
        ControllerCheckpoint.CAPSULE_PLANNED,
        None,
        None,
        None,
        None,
        None,
        "2" * 64,
        None,
        "2026-08-31T00:00:00Z",
        "2026-08-31T00:00:00Z",
    )
    queue = {
        "state": "completed",
        "thread_id": "thread-real",
        "raw_result_json": raw_result,
        "updated_at": "2026-08-31T00:01:00Z",
    }
    controller = SimpleNamespace(ledger=SimpleNamespace(queue_dispatch=lambda _dispatch_id: queue))

    payload = _detached_status_payload(controller, record)  # type: ignore[arg-type]

    assert payload["status"] == "completed"
    assert payload["checkpoint"] == "result_durable"
    assert payload["thread_id"] == "thread-real"
    assert payload["result"] == json.loads(raw_result)
    assert payload["updated_at"] == "2026-08-31T00:01:00Z"


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


def test_active_native_profile_projects_all_mcp_servers_through_pinned_runtime_parser() -> None:
    import codex_cli_bin

    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    if ".codex-flow" in source_home.parts and "sdk-runtime" in source_home.parts:
        source_home = (Path.home() / ".codex").resolve()
    source_before = (source_home / "config.toml").read_bytes()
    source_mcp_servers = set(tomllib.loads(source_before.decode("utf-8"))["mcp_servers"])
    profile = NativeProfileProjection.load(source_home, environment={"CODEX_LB_API_KEY": "test-only"})
    projected = tomllib.loads(profile.projected_toml)
    assert profile.provider_id == "codex-lb"
    assert set(projected["mcp_servers"]) == source_mcp_servers

    with TemporaryDirectory() as directory:
        runtime = NativeRuntimeConfig(Path(directory) / "runtime-home", profile)
        runtime.prepare()
        environment = {key: value for key, value in os.environ.items() if key not in {"CODEX_HOME", "HOME"}}
        environment.update(runtime.environment)
        environment["HOME"] = directory
        environment["CODEX_LB_API_KEY"] = "test-only"
        diagnostic = subprocess.run(
            (str(codex_cli_bin.bundled_codex_path()), "doctor", "--json"),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        report = json.loads(diagnostic.stdout)
        config_check = report["checks"]["config.load"]
        assert config_check["status"] == "ok"
        assert config_check["details"]["model provider"] == "codex-lb"
        assert config_check["details"]["mcp servers"] == str(len(source_mcp_servers))
        assert (Path(directory) / "runtime-home" / "config.toml").read_bytes() == profile.projected_toml.encode()
    assert (source_home / "config.toml").read_bytes() == source_before


def test_native_profile_optional_policy_tables_are_omitted_when_absent_and_preserved_when_present() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        original = config.read_text()
        without_optional = original
        without_optional = without_optional.replace("\n[agents]\nenabled = true\n", "")
        without_optional = without_optional.replace("\n[hooks]\n", "")
        without_optional = without_optional.replace("\n[shell_environment_policy]\n", "")
        config.write_text(without_optional)
        config.chmod(0o600)
        omitted = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected_omitted = tomllib.loads(omitted.projected_toml)
        assert not any(key in projected_omitted for key in ("agents", "hooks", "shell_environment_policy"))

        config.write_text(original)
        config.chmod(0o600)
        preserved = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected_preserved = tomllib.loads(preserved.projected_toml)
        assert projected_preserved["agents"] == {"enabled": True}
        assert projected_preserved["hooks"] == {}
        assert projected_preserved["shell_environment_policy"] == {}


@pytest.mark.parametrize("field", ("startup_timeout_sec", "tool_timeout_sec"))
def test_native_profile_rejects_huge_mcp_timeout_without_integer_float_overflow(field: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text(
            config.read_text() + f'\n[mcp_servers.timeout-check]\ncommand = "echo"\n{field} = {10**400}\n'
        )
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="finite positive numbers"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


def test_native_profile_preserves_valid_integer_mcp_timeout_projection() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text(
            config.read_text()
            + '\n[mcp_servers.timeout-check]\ncommand = "echo"\nstartup_timeout_sec = 7\ntool_timeout_sec = 11\n'
        )
        config.chmod(0o600)
        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected = tomllib.loads(profile.projected_toml)
        assert projected["mcp_servers"]["timeout-check"]["startup_timeout_sec"] == 7
        assert projected["mcp_servers"]["timeout-check"]["tool_timeout_sec"] == 11


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda text: text.replace('approval_policy = "never"\n', ""), "missing required surfaces"),
        (
            lambda text: text.replace("\n[agents]\nenabled = true\n", "").replace(
                'model_provider = "codex-lb"\n', 'agents = "invalid"\nmodel_provider = "codex-lb"\n'
            ),
            "optional surface",
        ),
        (
            lambda text: text.replace(
                'model_provider = "codex-lb"\n', 'unknown_surface = true\nmodel_provider = "codex-lb"\n'
            ),
            "unsupported surfaces",
        ),
    ),
)
def test_native_profile_rejects_missing_or_malformed_authority_surfaces(mutation: Any, message: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        before = config.read_bytes()
        config.write_text(mutation(config.read_text()))
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match=message):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        config.write_bytes(before)
        config.chmod(0o600)
        assert config.read_bytes() == before


def test_native_profile_service_tier_is_controller_owned_and_not_projected() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text('service_tier = "priority"\n' + config.read_text())
        config.chmod(0o600)
        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert "service_tier" not in tomllib.loads(profile.projected_toml)


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


@pytest.mark.parametrize("surface", ["skills", "plugins", "memories"])
def test_recursive_discovery_identity_detects_same_length_nested_modification(surface: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        source = _nested_discovery_file(home, surface)
        before = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})

        source.write_text("omega")

        with pytest.raises(NativeProfileError, match="discovery surface changed"):
            before.verify_sources()
        before.verify_worker_sources()
        after = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert after.compatibility_sha256 != before.compatibility_sha256
        assert after.worker_compatibility_sha256 == before.worker_compatibility_sha256
        serialized_facts = json.dumps(after.sanitized_facts, sort_keys=True)
        assert "SOURCE.md" not in serialized_facts
        assert "omega" not in serialized_facts


@pytest.mark.parametrize(
    "operation",
    ["create", "delete", "rename", "type", "root_substitution", "ancestor_substitution"],
)
def test_recursive_discovery_identity_detects_structural_substitution(operation: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        source = _nested_discovery_file(home, "skills")
        before = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})

        if operation == "create":
            source.with_name("CREATED.md").write_text("created")
        elif operation == "delete":
            source.unlink()
        elif operation == "rename":
            source.rename(source.with_name("RENAMED.md"))
        elif operation == "type":
            source.unlink()
            source.mkdir()
        elif operation == "root_substitution":
            discovery_root = home / "skills"
            discovery_root.rename(home / "skills-replaced")
            discovery_root.mkdir(mode=0o700)
            _nested_discovery_file(home, "skills")
        else:
            old_home = home.with_name("native-home-replaced")
            home.rename(old_home)
            home.mkdir(mode=0o700)
            for child in old_home.iterdir():
                child.rename(home / child.name)

        with pytest.raises(NativeProfileError, match="discovery surface changed"):
            before.verify_sources()
        if operation in {"root_substitution", "ancestor_substitution"}:
            with pytest.raises(NativeProfileError, match="discovery mount changed"):
                before.verify_worker_sources()
        else:
            before.verify_worker_sources()
        after = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert after.compatibility_sha256 != before.compatibility_sha256
        if operation not in {"root_substitution", "ancestor_substitution"}:
            assert after.worker_compatibility_sha256 == before.worker_compatibility_sha256


def test_discovery_symlinks_are_internal_fingerprinted_and_never_followed_outside() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        _test_native_profile(home)
        target = _nested_discovery_file(home, "plugins")
        alternate = target.with_name("ALTERNATE.md")
        alternate.write_text("other")
        link = home / "plugins" / "demo" / "current"
        link.symlink_to("nested/SOURCE.md")
        before = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})

        link.unlink()
        link.symlink_to("nested/ALTERNATE.md")
        changed = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert changed.compatibility_sha256 != before.compatibility_sha256
        with pytest.raises(NativeProfileError, match="discovery surface changed"):
            before.verify_sources()

        external = root / "external.txt"
        external.write_text("outside")
        link.unlink()
        link.symlink_to(external)
        with pytest.raises(NativeProfileError, match="escapes the discovery root"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


@pytest.mark.parametrize("surface", ["skills", "plugins", "memories"])
@pytest.mark.parametrize("cycle", ["root", "parent", "multi_node", "dangling", "nested_cycle"])
def test_discovery_rejects_root_ancestor_and_link_graph_cycles(surface: str, cycle: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        discovery = home / surface
        if cycle == "root":
            (discovery / "loop").symlink_to(".")
        elif cycle == "parent":
            nested = discovery / "nested"
            nested.mkdir()
            (nested / "loop").symlink_to("..")
        elif cycle == "multi_node":
            (discovery / "first").symlink_to("second")
            (discovery / "second").symlink_to("first")
        elif cycle == "dangling":
            (discovery / "loop").symlink_to("missing")
        else:
            nested = discovery / "nested" / "deeper"
            nested.mkdir(parents=True)
            (nested / "loop").symlink_to("..")

        with pytest.raises(NativeProfileError, match=r"cyclic|dangling"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


@pytest.mark.parametrize("surface", ["skills", "plugins", "memories"])
def test_discovery_root_cycle_blocks_controller_before_adapter_creation(surface: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        profile = _test_native_profile(home)
        (home / surface / "loop").symlink_to(".")
        adapter_creations = 0

        def factory(config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FakeAdapter(config, _service())

        controller = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=profile,
        )
        controller.plan(_capsule(repository, repository, base, branch))
        with pytest.raises(NativeProfileError, match="cyclic symlink"):
            controller.start("run", "m1")
        assert adapter_creations == 0
        controller.close()


def test_discovery_rejects_hardlinks_and_special_files() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        source = _nested_discovery_file(home, "memories")
        linked = source.with_name("LINKED.md")
        os.link(source, linked)
        with pytest.raises(NativeProfileError, match="hard-linked"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})

        linked.unlink()
        fifo = source.with_name("unsafe.fifo")
        os.mkfifo(fifo)
        with pytest.raises(NativeProfileError, match="unsupported special file"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


@pytest.mark.parametrize(
    ("constant", "limit", "expected"),
    [
        ("_MAX_DISCOVERY_ENTRIES", 1, "entry limit"),
        ("_MAX_DISCOVERY_DEPTH", 1, "depth limit"),
        ("_MAX_DISCOVERY_PATH_BYTES", 5, "overlong relative path"),
        ("_MAX_DISCOVERY_FILE_BYTES", 4, "per-file limit"),
        ("_MAX_DISCOVERY_TOTAL_BYTES", 4, "total file-byte limit"),
    ],
)
def test_discovery_limits_fail_closed(
    monkeypatch: pytest.MonkeyPatch, constant: str, limit: int, expected: str
) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        _nested_discovery_file(home, "skills")
        monkeypatch.setattr(native_profile_module, constant, limit)
        with pytest.raises(NativeProfileError, match=expected):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


def test_native_profile_rejects_secret_fields_symlinks_and_launch_time_mutation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        profile = _test_native_profile(home)
        config = home / "config.toml"
        original = config.read_text()

        config.write_text(original + '\n[mcp_servers.leaky.env]\nAPI_TOKEN = "must-not-project"\n')
        config.chmod(0o600)
        projected = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert "must-not-project" not in projected.projected_toml
        assert dict(projected.ephemeral_environment)["API_TOKEN"] == "must-not-project"

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


def test_native_profile_projects_header_and_mcp_secrets_only_through_ephemeral_environment() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secrets = {
            "Authorization": "Bearer header-auth-unique",
            "Proxy-Authorization": "proxy-auth-unique",
            "Cookie": "cookie-value-unique",
            "X-Custom": "arbitrary-header-unique",
            "API_KEY": "api-key-value-unique",
            "ACCESS_TOKEN": "access-token-value-unique",
            "PASSWORD": "password-value-unique",
            "CLIENT_SECRET": "client-secret-value-unique",
        }
        config.write_text(
            config.read_text()
            + f'''
[mcp_servers.remote]
url = "http://127.0.0.1:9"
http_headers = {{ Authorization = "{secrets["Authorization"]}", "Proxy-Authorization" = "{secrets["Proxy-Authorization"]}", Cookie = "{secrets["Cookie"]}", "X-Custom" = "{secrets["X-Custom"]}" }}

[mcp_servers.stdio]
command = "sh"
args = ["-c", "exit 0"]

[mcp_servers.stdio.env]
API_KEY = "{secrets["API_KEY"]}"
ACCESS_TOKEN = "{secrets["ACCESS_TOKEN"]}"
PASSWORD = "{secrets["PASSWORD"]}"
CLIENT_SECRET = "{secrets["CLIENT_SECRET"]}"
VISIBLE_SETTING = "retained"
'''
        )
        config.chmod(0o600)

        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected_bytes = profile.projected_toml.encode()
        for secret in secrets.values():
            assert secret.encode() not in projected_bytes
        projected = tomllib.loads(profile.projected_toml)
        remote = projected["mcp_servers"]["remote"]
        assert "http_headers" not in remote
        assert set(remote["env_http_headers"]) == {
            "Authorization",
            "Proxy-Authorization",
            "Cookie",
            "X-Custom",
        }
        stdio = projected["mcp_servers"]["stdio"]
        assert "env" not in stdio
        assert set(stdio["env_vars"]) == {
            "API_KEY",
            "ACCESS_TOKEN",
            "PASSWORD",
            "CLIENT_SECRET",
            "VISIBLE_SETTING",
        }
        ephemeral = dict(profile.ephemeral_environment)
        assert set(secrets.values()).issubset(ephemeral.values())
        assert ephemeral["VISIBLE_SETTING"] == "retained"

        runtime = NativeRuntimeConfig(root / "runtime-home", profile)
        runtime.prepare()
        assert set(secrets.values()).issubset(runtime.environment.values())
        for path in runtime.runtime_home.rglob("*"):
            if path.is_file():
                payload = path.read_bytes()
                for secret in secrets.values():
                    assert secret.encode() not in payload


@pytest.mark.parametrize(
    "header_key",
    (
        "HTTP_HEADERS",
        "Http_Headers",
        "http-Headers",
        "httpHeaders",
        "HTTPHEADERS",
        "EnvHttpHeaders",
        "ENVHTTPHEADERS",
        "env-http-headers",
    ),
)
def test_native_profile_rejects_noncanonical_header_shapes_without_echoing_values(header_key: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secret = "case-variant-header-secret"
        config.write_text(config.read_text() + f'\n[mcp_servers.bad]\n{header_key} = {{ "X-Custom" = "{secret}" }}\n')
        config.chmod(0o600)
        with pytest.raises(NativeProfileError) as captured:
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert secret not in str(captured.value)


@pytest.mark.parametrize(
    "fragment",
    (
        '[mcp_servers.bad.options.httpHeaders]\nAuthorization = "{secret}"\n',
        '[mcp_servers.bad.transport.EnvHttpHeaders]\nAuthorization = "{secret}"\n',
        '[mcp_servers.bad]\nheaders = {{ Authorization = "{secret}" }}\n',
        '[mcp_servers.bad]\nauthentication = {{ authToken = "{secret}" }}\n',
        '[mcp_servers.bad]\nhttp_headers = {{ Authorization = "canonical" }}\nhttpHeaders = {{ Cookie = "{secret}" }}\n',
    ),
)
def test_native_profile_closed_mcp_schema_rejects_nested_unknown_and_collision_shapes(fragment: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secret = "closed-schema-variant-secret"
        config.write_text(config.read_text() + "\n" + fragment.format(secret=secret))
        config.chmod(0o600)
        with pytest.raises(NativeProfileError) as captured:
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert secret not in str(captured.value)


def test_native_profile_rejects_nested_header_shape_without_echoing_values() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secret = "nested-header-secret"
        config.write_text(
            config.read_text() + f'\n[mcp_servers.bad.options.http_headers]\nAuthorization = "{secret}"\n'
        )
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="unsupported header field") as captured:
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert secret not in str(captured.value)


def test_native_profile_rejects_secret_environment_collision_without_echoing_value() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secret = "provider-collision-secret"
        config.write_text(
            config.read_text()
            + f'''
[mcp_servers.bad]
command = "sh"

[mcp_servers.bad.env]
CODEX_LB_API_KEY = "{secret}"
'''
        )
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="conflicts with a controller or provider") as captured:
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert secret not in str(captured.value)


def test_controller_route_keeps_native_secret_values_out_of_every_repository_owned_byte() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        base, branch = _repository(repository)
        home = root / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        secrets = (
            "controller-header-secret",
            "controller-token-secret",
            "controller-api-key-secret",
            "controller-client-secret",
            "controller-password-secret",
            "controller-private-key-secret",
            "controller-generic-key-secret",
        )
        config.write_text(
            config.read_text()
            + f'''
[mcp_servers.remote]
url = "http://127.0.0.1:9"
http_headers = {{ Authorization = "{secrets[0]}" }}

[mcp_servers.stdio]
command = "sh"
args = ["-c", "exit 0"]

[mcp_servers.stdio.env]
API_TOKEN = "{secrets[1]}"
apiKey = "{secrets[2]}"
clientSecret = "{secrets[3]}"
PassWord = "{secrets[4]}"
privateKey = "{secrets[5]}"
serviceKey = "{secrets[6]}"
'''
        )
        config.chmod(0o600)
        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        service = _service()
        adapter_creations = 0

        def factory(sdk_config: CodexSdkConfig) -> FakeAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            assert sdk_config.native_runtime is not None
            sdk_config.native_runtime.prepare()
            assert set(secrets).issubset(sdk_config.native_runtime.environment.values())
            return FakeAdapter(sdk_config, service)

        controller = Controller(
            repository,
            _trusted_test_adapter_factory=factory,
            _trusted_test_native_profile=profile,
        )
        controller.plan(_capsule(repository, repository, base, branch))
        assert controller.start("run", "m1").status is ExecutionStatus.COMPLETED
        controller.close()
        assert adapter_creations == 1

        for path in repository.rglob("*"):
            if path.is_file() and not path.is_symlink():
                payload = path.read_bytes()
                for secret in secrets:
                    assert secret.encode() not in payload


def test_native_profile_projects_every_literal_stdio_environment_value_process_only() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        _test_native_profile(home)
        values = {
            "GITHUB_PAT": "github-pat-arbitrary",
            "CI_JOB_JWT": "ci-jwt-arbitrary",
            "DATABASE_URL": "postgres://user:pass@example.invalid/db",
            "INNOCUOUS_SETTING": "ordinary-value",
            "MixedCase": "case-value",
            "EMPTY_VALUE": "",
        }
        config = home / "config.toml"
        entries = "\n".join(f'{key} = "{value}"' for key, value in values.items())
        config.write_text(
            config.read_text()
            + f"""\n[mcp_servers.arbitrary_stdio]
command = "sh"

[mcp_servers.arbitrary_stdio.env]
{entries}
"""
        )
        config.chmod(0o600)
        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected = tomllib.loads(profile.projected_toml)["mcp_servers"]["arbitrary_stdio"]
        assert "env" not in projected
        assert set(projected["env_vars"]) == set(values)
        ephemeral = dict(profile.ephemeral_environment)
        assert {key: ephemeral[key] for key in values} == values

        runtime = NativeRuntimeConfig(root / "runtime-home", profile)
        runtime.prepare()
        assert {key: runtime.environment[key] for key in values} == values
        for path in runtime.runtime_home.rglob("*"):
            if path.is_file() and not path.is_symlink():
                payload = path.read_bytes()
                for value in values.values():
                    if value:
                        assert value.encode() not in payload

        captured: dict[str, object] = {}

        class Client:
            def thread_start(self, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(id="arbitrary-env-thread")

            def close(self) -> None:
                return None

        class Sdk:
            Sandbox = SimpleNamespace(read_only="read-only", workspace_write="workspace-write", full_access="full")
            ApprovalMode = SimpleNamespace(deny_all="deny-all")
            ReasoningEffort = SimpleNamespace(medium="medium")
            SkillInput = None
            version = "test"

            @staticmethod
            def CodexConfig(**kwargs: object) -> object:
                captured.update(kwargs)
                return kwargs

            @staticmethod
            def Codex(*, config: object) -> Client:
                assert isinstance(config, dict)
                return Client()

        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                "gpt-test",
                ReasoningEffort.MEDIUM,
                sandbox=None,
                cwd=root,
                native_runtime=runtime,
                permission_mode=NativePermissionMode.INHERIT_NATIVE,
                effective_permission=profile.effective_authority(NativePermissionMode.INHERIT_NATIVE),
            ),
            sdk=Sdk(),
        )
        adapter.start_thread()
        assert captured["env"] == {"CODEX_HOME": str(runtime.runtime_home), **values}
        adapter.close()


def test_native_profile_preserves_controller_codex_home_and_delivers_source_value_to_mcp_child() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text(
            config.read_text()
            + """
[mcp_servers.collision]
command = "/bin/sh"
args = ["-c", "printf %s \\\"$CODEX_HOME\\\""]

[mcp_servers.collision.env]
CODEX_HOME = "/native/source-home"
"""
        )
        config.chmod(0o600)
        profile = NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        projected = tomllib.loads(profile.projected_toml)["mcp_servers"]["collision"]
        assert projected["command"] == "/bin/sh"
        assert "CODEX_HOME" not in projected["env_vars"]
        aliases = [name for name in projected["env_vars"] if name.startswith("CODEX_FLOW_MCP_COLLISION_")]
        assert len(aliases) == 1
        alias = aliases[0]
        assert dict(profile.ephemeral_environment)[alias] == "/native/source-home"

        runtime = NativeRuntimeConfig(root / "runtime-home", profile)
        runtime.prepare()
        assert runtime.environment["CODEX_HOME"] == str(runtime.runtime_home)
        child = subprocess.run(
            (projected["command"], *projected["args"]),
            env={**os.environ, **runtime.environment},
            capture_output=True,
            text=True,
            check=True,
        )
        assert child.stdout == "/native/source-home"
        assert set(tomllib.loads(profile.projected_toml)["mcp_servers"]) == {"collision"}


@pytest.mark.parametrize(
    "fragment",
    (
        """
[mcp_servers.bad]
command = "sh"
env_vars = ["GITHUB_PAT"]

[mcp_servers.bad.env]
GITHUB_PAT = "literal"
""",
        """
[mcp_servers.bad]
command = "sh"

[mcp_servers.bad.env]
GITHUB_PAT = "one"
github_pat = "two"
""",
        """
[mcp_servers.bad]
command = "sh"
env_vars = ["A_B", "AB"]
""",
        """
[mcp_servers.first]
command = "sh"
env_vars = ["SHARED"]

[mcp_servers.second]
command = "sh"
env_vars = ["shared"]
""",
    ),
)
def test_native_profile_rejects_duplicate_or_aliased_stdio_environment_names(fragment: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text(config.read_text() + fragment)
        config.chmod(0o600)
        with pytest.raises(NativeProfileError, match="unique and unambiguous"):
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})


@pytest.mark.parametrize(
    "fragment",
    (
        """
[mcp_servers.bad]
command = "sh"

[mcp_servers.bad.env]
bad-name = "invalid-key"
""",
        """
[mcp_servers.bad]
command = "sh"
env_vars = ["BAD-NAME"]
""",
        '\n[mcp_servers.bad]\ncommand = "sh"\n\n[mcp_servers.bad.env]\nTOO_BIG = "' + "x" * 16385 + '"\n',
    ),
)
def test_native_profile_rejects_invalid_stdio_environment_entries_without_echoing_values(fragment: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "native-home"
        _test_native_profile(home)
        config = home / "config.toml"
        config.write_text(config.read_text() + fragment)
        config.chmod(0o600)
        with pytest.raises(NativeProfileError) as captured:
            NativeProfileProjection.load(home, environment={"CODEX_LB_API_KEY": "test-only"})
        assert "x" * 128 not in str(captured.value)


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


def test_legacy_ledger_migrates_forward_to_canonical_execution_schema() -> None:
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
        assert ledger.schema_identity == "codex_flow_provider_transient_recovery_v17"
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
        assert ledger.schema_identity == "codex_flow_provider_transient_recovery_v17"
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
        assert ledger.schema_identity == "codex_flow_provider_transient_recovery_v17"
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
        assert ledger.schema_identity == "codex_flow_provider_transient_recovery_v17"
        columns = ledger.schema_columns("execution_integrity")
        assert "workspace_baseline_sha256" in columns
        assert "turn_started_at" in columns
        integrity = ledger.get_execution_integrity("run", "m1")
        assert integrity.provenance == "legacy_permission_v6"
        assert integrity.turn_started_at == timestamp
        ledger.close()


def test_migrated_v7_predecessor_without_terminal_snapshot_requires_explicit_reconciliation() -> None:
    service = _service()

    class FirstOutputAdapter(FakeAdapter):
        def run_turn(self, thread: ThreadIdentity, input: str, *, output_schema: Any = None) -> TurnObservation:
            self.service["turns"] += 1
            assert self.config.cwd is not None
            (self.config.cwd / "first.txt").write_text("accepted\n")
            return TurnObservation(thread, "turn-first", "completed", None, {"status": "done"}, ())

    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        first_capsule = replace(
            _capsule(repository, repository, base, branch, run="legacy-run", milestone="one"),
            mutable_paths=("first.txt",),
        )
        first = Controller(
            repository,
            _trusted_test_adapter_factory=lambda config: FirstOutputAdapter(config, service),
        )
        first.plan(first_capsule)
        assert first.start("legacy-run", "one").status is ExecutionStatus.COMPLETED
        first.close()

        database = repository / ".codex-flow" / "workflow.db"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE app_native_dispatches")
        for table in (
            "wake_outbox",
            "controller_action_outbox",
            "controller_decision_generations",
            "controller_decisions",
            "authorized_successors",
            "worker_liveness",
            "queue_bindings",
            "notification_outbox",
            "successor_outbox",
            "attempt_capabilities",
            "dispatch_queue",
            "supervisor_authority",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE execution_integrity RENAME TO execution_integrity_v8")
        connection.execute(_EXECUTION_INTEGRITY_V7_DDL)
        connection.execute(
            "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
            "native_compatibility_sha256, effective_permission_json, effective_permission_sha256, "
            "workspace_baseline_head_sha, workspace_baseline_json, workspace_baseline_sha256, turn_started_at, "
            "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
            "SELECT run_id, milestone_id, 'controller_v4', native_profile_sha256, native_compatibility_sha256, "
            "effective_permission_json, effective_permission_sha256, workspace_baseline_head_sha, "
            "workspace_baseline_json, workspace_baseline_sha256, turn_started_at, git_authority_before_sha256, "
            "git_authority_after_sha256, created_at, updated_at FROM execution_integrity_v8"
        )
        connection.execute("DROP TABLE execution_integrity_v8")
        connection.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
        connection.execute(
            "UPDATE schema_meta SET value = 'codex_flow_h3_causal_workspace_v7' WHERE key = 'schema_identity'"
        )
        connection.commit()
        connection.close()

        adapter_creations = 0

        def successor_factory(config: CodexSdkConfig) -> FirstOutputAdapter:
            nonlocal adapter_creations
            adapter_creations += 1
            return FirstOutputAdapter(config, service)

        successor = Controller(repository, _trusted_test_adapter_factory=successor_factory)
        assert successor.ledger.schema_version == CURRENT_SCHEMA_VERSION
        successor.plan(replace(first_capsule, milestone_id=MilestoneId("two"), mutable_paths=("second.txt",)))
        with pytest.raises(ControllerError, match="explicit reconciliation"):
            successor.start("legacy-run", "two")
        assert adapter_creations == 0
        successor.close()


def test_ignored_out_of_scope_file_is_excluded_from_preflight_scope() -> None:
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
        assert terminal.status is ExecutionStatus.COMPLETED
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

    service = _service()
    original_digest = controller_module.protected_paths_digest

    def fail_digest(*_args: Any, **_kwargs: Any) -> str:
        if service["turns"]:
            raise OSError("secret digest failure")
        return original_digest(*_args, **_kwargs)

    monkeypatch.setattr(controller_module, "protected_paths_digest", fail_digest)
    with TemporaryDirectory() as directory:
        repository = Path(directory) / "repo"
        base, branch = _repository(repository)
        controller = Controller(repository, _trusted_test_adapter_factory=_factory(service))
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
