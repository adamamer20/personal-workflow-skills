#!/usr/bin/env python3
"""Install the matching Codex Flow tool and personal workflow plugin."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_flow.ledger import HarnessRefreshBlocked, Ledger, LedgerError, ledger_schema_compatibility

PLUGIN_NAME = "personal-workflow-skills"
MARKETPLACE_NAME = "adam-workflows"
PLUGIN_VERSION = "0.1.12+codex.20260907000000"
CONTROLLER_VERSION = "0.2.0"
SDK_DISTRIBUTION = "openai-codex"
SDK_VERSION = "0.147.0"


class BootstrapError(RuntimeError):
    """A closed bootstrap phase failed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str


class CommandRunner:
    """Run only explicit argument vectors through resolved executables."""

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise BootstrapError(f"command failed ({completed.returncode}){suffix}")
        return CommandResult(completed.stdout, completed.stderr)


def _closed_json(value: str, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{label} did not return JSON") from exc
    if not isinstance(decoded, dict):
        raise BootstrapError(f"{label} returned a non-object JSON value")
    return decoded


def _manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise BootstrapError(f"manifest is not a regular single-link file: {path}")
    return _closed_json(path.read_text(encoding="utf-8"), label=path.name)


def _source_files(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise BootstrapError(f"plugin bundle is indirect or missing: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise BootstrapError(f"plugin bundle contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(mode) or path.stat().st_nlink != 1:
            raise BootstrapError(f"plugin bundle contains an unsafe file: {relative}")
        files[relative] = path.read_bytes()
    if not files:
        raise BootstrapError("plugin bundle is empty")
    return files


def _authority_directory(path: Path, *, label: str) -> Path:
    """Reject raw symlink topology before returning a canonical directory."""

    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as exc:
            raise BootstrapError(f"{label} is missing: {path}") from exc
        if stat.S_ISLNK(mode):
            raise BootstrapError(f"{label} uses a symlink path: {path}")
    resolved = absolute.resolve(strict=True)
    if not resolved.is_dir():
        raise BootstrapError(f"{label} is not a directory: {path}")
    return resolved


def _verify_distribution_record(tool_root: Path, *, distribution: str, version: str) -> None:
    """Verify one pinned installed dependency against its wheel RECORD."""

    site_packages = tool_root / "lib" / "python3.12" / "site-packages"
    normalized = distribution.replace("-", "_")
    dist_info = site_packages / f"{normalized}-{version}.dist-info"
    record = dist_info / "RECORD"
    if dist_info.is_symlink() or not dist_info.is_dir() or record.is_symlink() or not record.is_file():
        raise BootstrapError(f"installed {distribution} {version} metadata is unavailable")
    try:
        rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BootstrapError(f"installed {distribution} RECORD is malformed") from exc
    package_init = f"{normalized}/__init__.py"
    verified: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise BootstrapError(f"installed {distribution} RECORD is malformed")
        relative, encoded_digest, encoded_size = row
        parts = Path(relative).parts
        if not relative or Path(relative).is_absolute() or ".." in parts:
            raise BootstrapError(f"installed {distribution} RECORD path is unsafe")
        if relative == f"{dist_info.name}/RECORD":
            if encoded_digest or encoded_size:
                raise BootstrapError(f"installed {distribution} RECORD self-entry is malformed")
            continue
        if not encoded_digest.startswith("sha256=") or not encoded_size.isdecimal():
            raise BootstrapError(f"installed {distribution} RECORD lacks a pinned file identity")
        target = site_packages.joinpath(*parts)
        if target.is_symlink() or not target.is_file():
            raise BootstrapError(f"installed {distribution} file is unavailable: {relative}")
        raw = target.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
        if encoded_digest != f"sha256={digest}" or int(encoded_size) != len(raw):
            raise BootstrapError(f"installed {distribution} RECORD integrity failed: {relative}")
        verified.add(relative)
    if package_init not in verified:
        raise BootstrapError(f"installed {distribution} package identity is missing")


class Bootstrap:
    def __init__(self, repository_root: Path, *, runner: CommandRunner | None = None) -> None:
        self.root = repository_root.absolute()
        self.runner = runner or CommandRunner()
        self.completed: list[str] = []
        self.uv = self._executable("uv")
        self.codex = self._executable("codex")
        self.standard_home = Path.home().resolve()
        self.plugin_root = self.root / "plugins" / PLUGIN_NAME

    def _matching_harness_unit(self) -> Path:
        """Return the exact repository-scoped user unit path."""

        digest = hashlib.sha256(os.fspath(self.root.resolve()).encode("utf-8")).hexdigest()[:16]
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", self.standard_home / ".config"))
        return config_home / "systemd" / "user" / f"codex-flow-{digest}.service"

    def refresh_existing_harness(self) -> None:
        """Refresh only a safe, pre-existing matching harness unit."""

        path = self._matching_harness_unit()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BootstrapError("matching harness unit is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BootstrapError("matching harness unit is unsafe")
        launcher = self.standard_home / ".local" / "bin" / "codex-flow"
        self.runner.run((os.fspath(launcher), "harness", "refresh", "--state-root", os.fspath(self.root)))
        self.completed.append("harness-refreshed")

    def fence_existing_harness(self) -> None:
        """Arm the durable refresh fence before replacing the shared tool."""

        path = self._matching_harness_unit()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BootstrapError("matching harness unit is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BootstrapError("matching harness unit is unsafe")
        ledger_path = self.root / ".codex-flow" / "workflow.db"
        if not ledger_path.is_file():
            raise BootstrapError("matching harness ledger is unavailable")
        try:
            compatibility = ledger_schema_compatibility(ledger_path)
            if bool(compatibility.get("migration_required")):
                ledger = Ledger(ledger_path, allow_legacy=True)
                fence_method = ledger.arm_predecessor_refresh_fence
            else:
                ledger = Ledger(ledger_path)
                fence_method = ledger.arm_harness_refresh_fence
        except LedgerError as exc:
            raise BootstrapError("matching harness ledger cannot be opened") from exc
        try:
            try:
                authority = fence_method()
            except HarnessRefreshBlocked as exc:
                raise BootstrapError(f"harness refresh deferred: {exc}") from exc
            except LedgerError as exc:
                raise BootstrapError("harness refresh fence could not be armed") from exc
            if authority is None:
                raise BootstrapError("matching harness authority is unavailable")
        finally:
            ledger.close()
        self.completed.append("harness-refresh-fenced")

    @staticmethod
    def _executable(name: str) -> str:
        selected = shutil.which(name)
        if selected is None:
            raise BootstrapError(f"required executable is unavailable: {name}")
        resolved = Path(selected).resolve(strict=True)
        if not resolved.is_file():
            raise BootstrapError(f"required executable is not a file: {name}")
        return os.fspath(resolved)

    def preflight(self) -> None:
        if _authority_directory(self.root, label="repository authority") != self.root:
            raise BootstrapError("repository path substitution is not allowed")
        configured_home = os.environ.get("CODEX_HOME")
        standard_codex_home = self.standard_home / ".codex"
        if configured_home is not None and Path(configured_home).resolve() != standard_codex_home:
            raise BootstrapError("private CODEX_HOME is not supported by this shared-user bootstrap")
        for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR"):
            if os.environ.get(key):
                raise BootstrapError(f"{key} path substitution is not supported")
        marketplace = _manifest(self.root / ".agents" / "plugins" / "marketplace.json")
        plugin = _manifest(self.plugin_root / ".codex-plugin" / "plugin.json")
        entries = marketplace.get("plugins")
        if marketplace.get("name") != MARKETPLACE_NAME or not isinstance(entries, list) or len(entries) != 1:
            raise BootstrapError("marketplace manifest does not contain one expected plugin")
        entry = entries[0]
        if not isinstance(entry, dict):
            raise BootstrapError("marketplace plugin entry is malformed")
        if entry.get("name") != PLUGIN_NAME or plugin.get("name") != PLUGIN_NAME:
            raise BootstrapError("plugin identity mismatch")
        if entry.get("version") != PLUGIN_VERSION or plugin.get("version") != PLUGIN_VERSION:
            raise BootstrapError("plugin manifest and marketplace version must match the bootstrap")
        source = entry.get("source")
        if source != {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}:
            raise BootstrapError("marketplace source path is not the canonical local bundle")
        _source_files(self.plugin_root)
        self.completed.append("preflight")

    def _marketplaces(self) -> list[dict[str, Any]]:
        payload = _closed_json(
            self.runner.run((self.codex, "plugin", "marketplace", "list", "--json")).stdout,
            label="marketplace list",
        )
        values = payload.get("marketplaces")
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise BootstrapError("marketplace list is malformed")
        return values

    def _plugins(self) -> list[dict[str, Any]]:
        payload = _closed_json(
            self.runner.run((self.codex, "plugin", "list", "--available", "--json")).stdout,
            label="plugin list",
        )
        installed = payload.get("installed")
        if not isinstance(installed, list) or any(not isinstance(item, dict) for item in installed):
            raise BootstrapError("plugin list is malformed")
        return installed

    def install_controller(self) -> None:
        self.runner.run(
            (
                self.uv,
                "tool",
                "install",
                "--python",
                "3.12",
                "--force",
                "--reinstall-package",
                SDK_DISTRIBUTION,
                "--no-cache",
                "--link-mode",
                "copy",
                os.fspath(self.root),
            )
        )
        self.completed.append("controller-installed")

    def install_marketplace(self) -> None:
        matches = [item for item in self._marketplaces() if item.get("name") == MARKETPLACE_NAME]
        if len(matches) > 1:
            raise BootstrapError("duplicate marketplace authority detected")
        if matches:
            source = matches[0].get("marketplaceSource")
            if not isinstance(source, dict) or source.get("sourceType") != "local":
                raise BootstrapError("configured marketplace is not the expected local source")
            reported = Path(str(source.get("source", "")))
            if not reported.exists() and not reported.is_symlink():
                raise BootstrapError("configured marketplace path substitution detected")
            configured = _authority_directory(reported, label="configured marketplace authority")
            if configured != self.root:
                raise BootstrapError("configured marketplace path substitution detected")
        else:
            self.runner.run((self.codex, "plugin", "marketplace", "add", os.fspath(self.root), "--json"))
        self.completed.append("marketplace-ready")

    def install_plugin(self) -> None:
        matches = [item for item in self._plugins() if item.get("pluginId") == f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"]
        if len(matches) > 1:
            raise BootstrapError("duplicate plugin authority detected")
        if not matches or matches[0].get("version") != PLUGIN_VERSION or matches[0].get("enabled") is not True:
            self.runner.run((self.codex, "plugin", "add", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "--json"))
        self.completed.append("plugin-installed")

    def verify(self) -> dict[str, str]:
        tool_list = self.runner.run((self.uv, "tool", "list")).stdout
        if f"codex-flow v{CONTROLLER_VERSION}" not in tool_list.splitlines():
            raise BootstrapError("installed codex-flow version is incompatible")
        launcher = self.standard_home / ".local" / "bin" / "codex-flow"
        expected = self.standard_home / ".local" / "share" / "uv" / "tools" / "codex-flow" / "bin" / "codex-flow"
        if not launcher.is_symlink() or launcher.resolve(strict=True) != expected.resolve(strict=True):
            raise BootstrapError("codex-flow launcher path substitution detected")
        if not expected.is_file() or expected.is_symlink() or expected.stat().st_nlink != 1:
            raise BootstrapError("codex-flow target is not a regular single-link file")
        _verify_distribution_record(
            expected.parent.parent,
            distribution=SDK_DISTRIBUTION,
            version=SDK_VERSION,
        )
        self.runner.run((os.fspath(launcher), "--help"))
        self.runner.run((os.fspath(launcher), "tui", "--help"))

        matches = [item for item in self._plugins() if item.get("pluginId") == f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"]
        if len(matches) != 1:
            raise BootstrapError("installed plugin authority is missing or duplicated")
        plugin = matches[0]
        if plugin.get("version") != PLUGIN_VERSION or plugin.get("enabled") is not True:
            raise BootstrapError("installed plugin version or enabled state is incompatible")
        source = plugin.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            raise BootstrapError("installed plugin source is not local")
        installed_root = _authority_directory(Path(str(source.get("path", ""))), label="installed plugin authority")
        expected_root = _authority_directory(self.plugin_root, label="checkout plugin authority")
        if installed_root != expected_root:
            raise BootstrapError("installed plugin path substitution detected")
        if _source_files(installed_root) != _source_files(self.plugin_root):
            raise BootstrapError("installed plugin bytes do not match the checkout")
        self.completed.append("verified")
        return {
            "controller_version": CONTROLLER_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "controller_command": os.fspath(launcher),
            "tui_command": f"{launcher} tui",
        }

    def execute(self) -> dict[str, str]:
        self.preflight()
        self.fence_existing_harness()
        self.install_controller()
        self.refresh_existing_harness()
        self.install_marketplace()
        self.install_plugin()
        return self.verify()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    bootstrap: Bootstrap | None = None
    try:
        bootstrap = Bootstrap(root)
        facts = bootstrap.execute()
    except (BootstrapError, OSError, ValueError) as exc:
        phases = ",".join(bootstrap.completed) if bootstrap is not None and bootstrap.completed else "none"
        print(f"bootstrap failed after [{phases}]: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "ready", **facts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
