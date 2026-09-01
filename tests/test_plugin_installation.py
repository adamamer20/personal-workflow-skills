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


def _installer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "install_personal_workflow_skills.py"
    spec = importlib.util.spec_from_file_location("install_personal_workflow_skills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _installer()


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


def test_bootstrap_fences_active_supervisor_before_shared_tool_install(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))
    runner = FakeRunner(root)
    bootstrap = installer.Bootstrap(root, runner=runner)
    unit_path = bootstrap._matching_supervisor_unit()
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("existing supervisor unit\n", encoding="utf-8")

    class ActiveChildLedger:
        def __init__(self, _path: Path) -> None:
            pass

        def arm_supervisor_refresh_fence(self) -> None:
            raise installer.SupervisorRefreshBlocked("worker child is active")

        def close(self) -> None:
            pass

    ledger_path = root / ".codex-flow" / "workflow.db"
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True if path == ledger_path else original_is_file(path),
    )
    monkeypatch.setattr(installer, "Ledger", ActiveChildLedger)

    with pytest.raises(installer.BootstrapError, match="supervisor refresh deferred"):
        bootstrap.execute()
    assert bootstrap.completed == ["preflight"]
    assert not any(call[1:3] == ("tool", "install") for call in runner.calls)


def test_bootstrap_rejects_unsafe_supervisor_unit_before_shared_tool_install(
    standard_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = Path(__file__).parents[1].resolve()
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(config_home))
    runner = FakeRunner(root)
    bootstrap = installer.Bootstrap(root, runner=runner)
    unit_path = bootstrap._matching_supervisor_unit()
    unit_path.parent.mkdir(parents=True)
    target = tmp_path / "unit-target"
    target.write_text("existing supervisor unit\n", encoding="utf-8")
    unit_path.symlink_to(target)

    with pytest.raises(installer.BootstrapError, match="matching supervisor unit is unsafe"):
        bootstrap.execute()
    assert bootstrap.completed == ["preflight"]
    assert not any(call[1:3] == ("tool", "install") for call in runner.calls)
