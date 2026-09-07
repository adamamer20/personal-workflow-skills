from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

import codex_flow.service as service_module
from codex_flow.ledger import Ledger
from codex_flow.service import (
    ServiceError,
    generate_unit,
    install_unit,
    process_birth_identity,
    refresh_with_credential,
)


def _installer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "install_personal_workflow_skills.py"
    spec = importlib.util.spec_from_file_location("install_personal_workflow_skills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _installer()
_TEST_REPLACEMENT_PID = os.getpid()
_TEST_REPLACEMENT_BIRTH = process_birth_identity(_TEST_REPLACEMENT_PID)
_TEST_INTERPRETER_DIGEST = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()


def test_make_bootstrap_runs_inside_the_locked_project_environment() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")
    assert "\t$(UV) run --python $(PYTHON) python scripts/install_personal_workflow_skills.py\n" in makefile
    assert "\t/usr/bin/env python3 scripts/install_personal_workflow_skills.py\n" not in makefile


def _write_sdk_distribution(home: Path) -> Path:
    site_packages = home / ".local" / "share" / "uv" / "tools" / "codex-flow" / "lib" / "python3.12" / "site-packages"
    package = site_packages / "openai_codex"
    dist_info = site_packages / "openai_codex-0.147.0.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    files = {
        "openai_codex/__init__.py": b'__version__ = "0.147.0"\n',
        "openai_codex-0.147.0.dist-info/METADATA": (b"Metadata-Version: 2.4\nName: openai-codex\nVersion: 0.147.0\n"),
    }
    rows: list[str] = []
    for relative, raw in files.items():
        target = site_packages / relative
        target.write_bytes(raw)
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
        rows.append(f"{relative},sha256={digest},{len(raw)}")
    rows.append("openai_codex-0.147.0.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return package / "__init__.py"


class FakeRunner:
    def __init__(self, root: Path, *, marketplace: bool = True, plugin: bool = True) -> None:
        self.root = root
        self.marketplace = marketplace
        self.plugin = plugin
        self.calls: list[tuple[str, ...]] = []
        self.fail_fragment: tuple[str, ...] | None = None

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if self.fail_fragment is not None and all(item in argv for item in self.fail_fragment):
            raise installer.BootstrapError("injected failure")
        if argv[-4:] == ("plugin", "marketplace", "list", "--json"):
            values = []
            if self.marketplace:
                values.append(
                    {
                        "name": installer.MARKETPLACE_NAME,
                        "marketplaceSource": {"sourceType": "local", "source": os.fspath(self.root)},
                    }
                )
            return installer.CommandResult(json.dumps({"marketplaces": values}), "")
        if "marketplace" in argv and "add" in argv:
            self.marketplace = True
            return installer.CommandResult("{}", "")
        if argv[-4:] == ("plugin", "list", "--available", "--json"):
            values = []
            if self.plugin:
                values.append(
                    {
                        "pluginId": f"{installer.PLUGIN_NAME}@{installer.MARKETPLACE_NAME}",
                        "version": installer.PLUGIN_VERSION,
                        "enabled": True,
                        "source": {
                            "source": "local",
                            "path": os.fspath(self.root / "plugins" / installer.PLUGIN_NAME),
                        },
                    }
                )
            return installer.CommandResult(json.dumps({"installed": values}), "")
        if "plugin" in argv and "add" in argv:
            self.plugin = True
            return installer.CommandResult("{}", "")
        if argv[-2:] == ("tool", "list"):
            return installer.CommandResult(f"codex-flow v{installer.CONTROLLER_VERSION}\n- codex-flow\n", "")
        return installer.CommandResult("", "")


@pytest.fixture
def standard_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    target = home / ".local" / "share" / "uv" / "tools" / "codex-flow" / "bin" / "codex-flow"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = home / ".local" / "bin" / "codex-flow"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    _write_sdk_distribution(home)
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/true")
    return home


def test_bootstrap_uses_closed_argv_and_is_repeatable(standard_home: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    runner = FakeRunner(root)

    first = installer.Bootstrap(root, runner=runner)
    facts = first.execute()
    second = installer.Bootstrap(root, runner=runner)
    repeated = second.execute()

    assert facts == repeated
    assert facts["controller_version"] == "0.2.0"
    assert facts["plugin_version"] == installer.PLUGIN_VERSION
    assert all(isinstance(call, tuple) and call for call in runner.calls)
    assert sum(call[1:3] == ("tool", "install") for call in runner.calls) == 2
    install_calls = [call for call in runner.calls if call[1:3] == ("tool", "install")]
    assert all("--no-cache" in call and call[call.index("--link-mode") + 1] == "copy" for call in install_calls)
    assert all(call[call.index("--reinstall-package") + 1] == "openai-codex" for call in install_calls)
    assert not any("marketplace" in call and "add" in call for call in runner.calls)
    assert not any(call[1:3] == ("plugin", "add") for call in runner.calls)


def test_fresh_bootstrap_adds_one_marketplace_and_plugin(standard_home: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    runner = FakeRunner(root, marketplace=False, plugin=False)

    installer.Bootstrap(root, runner=runner).execute()

    marketplace_adds = [call for call in runner.calls if "marketplace" in call and "add" in call]
    plugin_adds = [call for call in runner.calls if call[1:3] == ("plugin", "add")]
    assert len(marketplace_adds) == 1
    assert marketplace_adds[0][-2:] == (os.fspath(root), "--json")
    assert plugin_adds == [
        (
            "/usr/bin/true",
            "plugin",
            "add",
            f"{installer.PLUGIN_NAME}@{installer.MARKETPLACE_NAME}",
            "--json",
        )
    ]


def test_private_codex_home_is_rejected_before_commands(standard_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).parents[1].resolve()
    runner = FakeRunner(root)
    monkeypatch.setenv("CODEX_HOME", os.fspath(standard_home / "private-codex"))

    with pytest.raises(installer.BootstrapError, match="private CODEX_HOME"):
        installer.Bootstrap(root, runner=runner).preflight()
    assert runner.calls == []


@pytest.mark.parametrize("key", ["UV_TOOL_DIR", "UV_TOOL_BIN_DIR"])
def test_uv_path_substitution_is_rejected(standard_home: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    root = Path(__file__).parents[1].resolve()
    monkeypatch.setenv(key, os.fspath(standard_home / "substituted"))

    with pytest.raises(installer.BootstrapError, match="path substitution"):
        installer.Bootstrap(root, runner=FakeRunner(root)).preflight()


def test_marketplace_path_substitution_fails_closed(standard_home: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    runner = FakeRunner(root / "different")
    bootstrap = installer.Bootstrap(root, runner=runner)
    bootstrap.preflight()
    bootstrap.install_controller()

    with pytest.raises(installer.BootstrapError, match="marketplace path substitution"):
        bootstrap.install_marketplace()
    assert bootstrap.completed == ["preflight", "controller-installed"]


def test_marketplace_symlink_alias_fails_before_canonical_identity(standard_home: Path, tmp_path: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    alias = tmp_path / "marketplace-alias"
    alias.symlink_to(root, target_is_directory=True)
    runner = FakeRunner(alias)
    bootstrap = installer.Bootstrap(root, runner=runner)
    bootstrap.preflight()
    bootstrap.install_controller()

    with pytest.raises(installer.BootstrapError, match="marketplace authority uses a symlink"):
        bootstrap.install_marketplace()


def test_installed_plugin_symlink_alias_fails_before_canonical_identity(standard_home: Path, tmp_path: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    alias = tmp_path / "plugin-alias"
    alias.symlink_to(root / "plugins" / installer.PLUGIN_NAME, target_is_directory=True)
    runner = FakeRunner(root)
    original_run = runner.run

    def run(argv: tuple[str, ...]):
        result = original_run(argv)
        if argv[-4:] == ("plugin", "list", "--available", "--json"):
            payload = json.loads(result.stdout)
            payload["installed"][0]["source"]["path"] = os.fspath(alias)
            return installer.CommandResult(json.dumps(payload), "")
        return result

    runner.run = run  # type: ignore[method-assign]
    bootstrap = installer.Bootstrap(root, runner=runner)
    bootstrap.preflight()

    with pytest.raises(installer.BootstrapError, match="plugin authority uses a symlink"):
        bootstrap.verify()


def test_corrupted_installed_sdk_record_fails_readiness(standard_home: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    installed = (
        standard_home
        / ".local"
        / "share"
        / "uv"
        / "tools"
        / "codex-flow"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "openai_codex"
        / "__init__.py"
    )
    installed.write_text("# injected fixture\n", encoding="utf-8")

    with pytest.raises(installer.BootstrapError, match="RECORD integrity failed"):
        installer.Bootstrap(root, runner=FakeRunner(root)).verify()


def test_symlink_in_plugin_bundle_is_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "plugin"
    bundle.mkdir()
    target = bundle / "target"
    target.write_text("safe", encoding="utf-8")
    (bundle / "alias").symlink_to(target)

    with pytest.raises(installer.BootstrapError, match="contains a symlink"):
        installer._source_files(bundle)


def test_external_failure_preserves_truthful_completed_phases(standard_home: Path) -> None:
    root = Path(__file__).parents[1].resolve()
    runner = FakeRunner(root)
    runner.fail_fragment = ("marketplace", "list")
    bootstrap = installer.Bootstrap(root, runner=runner)
    bootstrap.preflight()
    bootstrap.install_controller()

    with pytest.raises(installer.BootstrapError, match="injected failure"):
        bootstrap.install_marketplace()
    assert bootstrap.completed == ["preflight", "controller-installed"]


@pytest.mark.parametrize("migration_required", [False, True])
def test_bootstrap_fences_active_harness_before_shared_tool_install(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migration_required: bool
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))
    runner = FakeRunner(root)
    bootstrap = installer.Bootstrap(root, runner=runner)
    unit_path = bootstrap._matching_harness_unit()
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("existing harness unit\n", encoding="utf-8")
    fence_calls: list[str] = []

    class ActiveChildLedger:
        def __init__(self, path: Path, *, allow_legacy: bool = False) -> None:
            assert path == root / ".codex-flow" / "workflow.db"
            assert allow_legacy is migration_required

        def arm_harness_refresh_fence(self) -> None:
            assert not migration_required
            fence_calls.append("arm_harness_refresh_fence")
            raise installer.HarnessRefreshBlocked("worker child is active")

        def arm_predecessor_refresh_fence(self) -> None:
            assert migration_required
            fence_calls.append("arm_predecessor_refresh_fence")
            raise installer.HarnessRefreshBlocked("worker child is active")

        def close(self) -> None:
            pass

    ledger_path = root / ".codex-flow" / "workflow.db"
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True if path == ledger_path else original_is_file(path),
    )
    monkeypatch.setattr(
        installer,
        "ledger_schema_compatibility",
        lambda path: {"migration_required": migration_required},
    )
    monkeypatch.setattr(installer, "Ledger", ActiveChildLedger)

    with pytest.raises(installer.BootstrapError, match="harness refresh deferred"):
        bootstrap.execute()
    assert bootstrap.completed == ["preflight"]
    assert fence_calls == ["arm_predecessor_refresh_fence" if migration_required else "arm_harness_refresh_fence"]
    assert not any(call[1:3] == ("tool", "install") for call in runner.calls)


@pytest.mark.parametrize(
    "failure_stage",
    [
        None,
        "authorize-before",
        "authorize-after",
        "publish-before",
        "publish-after",
        "daemon-reload",
        "import-environment",
        "start",
    ],
)
def test_bootstrap_service_entrypoint_gates_plugin_cache_after_refresh(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str | None
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))

    service_root = tmp_path / "service-root"
    service_root.mkdir()
    executable = tmp_path / "codex-flow"
    executable.write_text(f"#!{Path(sys.executable).resolve()}\n", encoding="utf-8")
    executable.chmod(0o755)
    service_state = service_root / ".codex-flow"
    service_state.mkdir()
    ledger_path = service_state / "workflow.db"
    ledger = Ledger(ledger_path)
    ledger.acquire_harness(
        repository_root=service_root,
        state_root=service_root,
        pid=500,
        process_birth_identity="old-birth",
        executable_digest="a" * 64,
        version=installer.CONTROLLER_VERSION,
        owner_nonce_sha256="b" * 64,
    )
    ledger.close()
    service_unit = generate_unit(
        service_root, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="e" * 64
    )
    old_unit = generate_unit(
        service_root, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="c" * 64
    )
    service_config = tmp_path / "service-config"
    install_unit(old_unit, config_home=service_config)
    original_authorize = Ledger.authorize_controller_profile_refresh
    original_install = service_module.install_unit

    def authorize(self: Ledger, **kwargs: str) -> int:
        if failure_stage == "authorize-before":
            raise RuntimeError("injected authorization failure")
        result = original_authorize(self, **kwargs)
        if failure_stage == "authorize-after":
            raise RuntimeError("injected committed authorization failure")
        return result

    def publish(unit, **kwargs):
        if failure_stage == "publish-before":
            raise RuntimeError("injected publication failure")
        result = original_install(unit, **kwargs)
        if failure_stage == "publish-after":
            raise RuntimeError("injected published failure")
        return result

    monkeypatch.setattr(Ledger, "authorize_controller_profile_refresh", authorize)
    monkeypatch.setattr(service_module, "install_unit", publish)

    matching_unit = installer.Bootstrap(root, runner=FakeRunner(root))._matching_harness_unit()
    matching_unit.parent.mkdir(parents=True)
    matching_unit.write_text(service_unit.text, encoding="utf-8")
    processes = {(500, "old-birth"): True}
    service_active = {"value": True}
    service_events: list[str] = []

    class ServiceResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def service_runner(argv: tuple[str, ...], **_: object) -> ServiceResult:
        operation = argv[2]
        service_events.append(operation)
        if operation == "show":
            result = ServiceResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_active["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return ServiceResult(0 if service_active["value"] else 3)
        if operation == failure_stage:
            return ServiceResult(1)
        if operation == "start":
            service_active["value"] = True
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=service_root,
                state_root=service_root,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
                version=service_unit.version,
                owner_nonce_sha256="d" * 64,
            )
            replacement.close()
        return ServiceResult(0)

    def service_shutdown(socket_path: Path, _timeout: float) -> dict[str, object]:
        assert socket_path.name == "harness.sock"
        service_events.append("shutdown")
        processes[(500, "old-birth")] = False
        service_active["value"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    installer_events: list[str] = []

    class ServiceRunner(FakeRunner):
        def run(self, argv: tuple[str, ...]):
            if argv[1:3] == ("harness", "refresh"):
                installer_events.append("refresh")
                try:
                    refresh_with_credential(
                        service_unit,
                        config_home=service_config,
                        profile_sha256=service_unit.profile_sha256,
                        native_compatibility_sha256="d" * 64,
                        environment={"OPENAI_API_KEY": "secret"},
                        runner=service_runner,
                        process_is_live=lambda pid, birth: processes.get((pid, birth), False),
                        clock=lambda: 0.0,
                        sleeper=lambda _delay: None,
                        shutdown_sender=service_shutdown,
                    )
                except ServiceError as exc:
                    raise installer.BootstrapError(str(exc)) from exc
                return installer.CommandResult('{"refreshed":true}', "")
            result = super().run(argv)
            if argv[1:3] == ("tool", "install"):
                installer_events.append("tool-install")
            return result

    runner = ServiceRunner(root)
    bootstrap = installer.Bootstrap(root, runner=runner)

    def fenced() -> None:
        installer_events.append("fence")
        bootstrap.completed.append("harness-refresh-fenced")

    monkeypatch.setattr(bootstrap, "fence_existing_harness", fenced)

    if failure_stage is None:
        facts = bootstrap.execute()
        assert facts["controller_version"] == installer.CONTROLLER_VERSION
        assert "marketplace-ready" in bootstrap.completed
        assert "plugin-installed" in bootstrap.completed
        assert (
            installer_events.index("fence") < installer_events.index("tool-install") < installer_events.index("refresh")
        )
        assert service_events[-1] == "is-active"
    else:
        with pytest.raises(installer.BootstrapError):
            bootstrap.execute()
        assert bootstrap.completed == ["preflight", "harness-refresh-fenced", "controller-installed"]
        assert installer_events == ["fence", "tool-install", "refresh"]
        assert not any("marketplace" in call or "plugin" in call for call in runner.calls)


def test_bootstrap_rejects_unsafe_harness_unit_before_shared_tool_install(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))
    runner = FakeRunner(root)
    bootstrap = installer.Bootstrap(root, runner=runner)
    unit_path = bootstrap._matching_harness_unit()
    unit_path.parent.mkdir(parents=True)
    target = tmp_path / "unit-target"
    target.write_text("existing harness unit\n", encoding="utf-8")
    unit_path.symlink_to(target)

    with pytest.raises(installer.BootstrapError, match="matching harness unit is unsafe"):
        bootstrap.execute()
    assert bootstrap.completed == ["preflight"]
    assert not any(call[1:3] == ("tool", "install") for call in runner.calls)


def test_bootstrap_execute_refreshes_one_exact_fenced_v19_harness_without_installer_drift(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))
    runner = FakeRunner(root)
    unit_path = installer.Bootstrap(root, runner=runner)._matching_harness_unit()
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("exact stopped v18 predecessor\n", encoding="utf-8")
    before_script = hashlib.sha256((root / "scripts" / "install_personal_workflow_skills.py").read_bytes()).digest()
    calls: list[tuple[str, ...]] = []

    authority = {
        "repository_root": os.fspath(root),
        "state_root": os.fspath(root),
        "version": installer.CONTROLLER_VERSION,
        "epoch": 7,
        "pid": 123,
        "process_birth_identity": "birth-v19",
        "requested_shutdown": 1,
    }

    class FencedV19Ledger:
        def __init__(self, path: Path, **_: object) -> None:
            assert path == root / ".codex-flow" / "workflow.db"

        def arm_harness_refresh_fence(self) -> dict[str, object]:
            calls.append(("arm_harness_refresh_fence",))
            return authority

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        installer,
        "ledger_schema_compatibility",
        lambda path: {"ledger_schema_version": 19, "migration_required": False},
    )
    monkeypatch.setattr(installer, "Ledger", FencedV19Ledger)
    bootstrap = installer.Bootstrap(root, runner=runner)
    facts = bootstrap.execute()

    refresh_calls = [call for call in runner.calls if call[1:3] == ("harness", "refresh")]
    assert refresh_calls == [
        (
            os.fspath(standard_home / ".local" / "bin" / "codex-flow"),
            "harness",
            "refresh",
            "--state-root",
            os.fspath(root),
        )
    ]
    assert calls == [("arm_harness_refresh_fence",), ("close",)]
    assert bootstrap.completed == [
        "preflight",
        "harness-refresh-fenced",
        "controller-installed",
        "harness-refreshed",
        "marketplace-ready",
        "plugin-installed",
        "verified",
    ]
    assert facts["controller_version"] == installer.CONTROLLER_VERSION
    assert (
        hashlib.sha256((root / "scripts" / "install_personal_workflow_skills.py").read_bytes()).digest()
        == before_script
    )
