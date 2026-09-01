"""Descriptor-bound, secretless discovery of installed Codex plugins.

The inherited Codex home is read-only authority. Every directory and file is
opened without following its final component, checked by descriptor identity,
and consumed under finite discovery and bundle bounds. This module performs no
install, enable, setup, trust, authentication, or connector operation.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from .contracts import PluginCapabilitySnapshot, PluginReadiness, PluginRequirement
from .domain import SkillInput

_MAX_HOME_CONFIG_BYTES: Final[int] = 1_048_576
_MAX_MANIFEST_BYTES: Final[int] = 1_048_576
_MAX_DISCOVERY_ENTRIES: Final[int] = 4_096
_MAX_CANDIDATE_BUNDLES: Final[int] = 128
_MAX_BUNDLE_DEPTH: Final[int] = 32
_MAX_BUNDLE_FILES: Final[int] = 8_192
_MAX_SINGLE_FILE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_BUNDLE_BYTES: Final[int] = 256 * 1024 * 1024
_MAX_RESOLUTION_BYTES: Final[int] = 256 * 1024 * 1024
_MAX_PLUGIN_ID_BYTES: Final[int] = 128
_MAX_MANIFEST_ITEMS: Final[int] = 128
_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}(?:@[a-z0-9][a-z0-9._-]{0,127})?$")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|proxy[_-]?authorization|cookie|password|secret|credential|bearer|"
    r"client[_-]?secret|api[_-]?key|access[_-]?token|auth[_-]?token|(?:^|[_-])token(?:$|[_-]))",
    re.I,
)
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW


class PluginCapabilityError(ValueError):
    """A plugin requirement or installed capability is malformed or unsafe."""


class PluginCapabilityUnavailable(PluginCapabilityError):
    """A required plugin cannot be used without external mutation."""


@dataclass(slots=True)
class _ReadBudget:
    entries: int = 0
    candidates: int = 0
    bytes_read: int = 0

    def add_entries(self, count: int) -> None:
        self.entries += count
        if self.entries > _MAX_DISCOVERY_ENTRIES:
            raise PluginCapabilityError("plugin discovery exceeds its total-entry bound")

    def add_candidate(self) -> None:
        self.candidates += 1
        if self.candidates > _MAX_CANDIDATE_BUNDLES:
            raise PluginCapabilityError("plugin discovery exceeds its bundle-count bound")

    def add_bytes(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > _MAX_RESOLUTION_BYTES:
            raise PluginCapabilityError("plugin resolution exceeds its total-byte bound")


@dataclass(frozen=True, slots=True)
class _BundleContents:
    digest: str
    files: Mapping[str, bytes]
    directories: frozenset[str]


@dataclass(slots=True)
class _Candidate:
    descriptor: int
    canonical_id: str
    source: str
    expected_name: str
    expected_version: str | None = None


@dataclass(slots=True)
class _VerifiedSkillBinding:
    descriptor: int
    input: SkillInput

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


def standard_codex_home() -> Path:
    """Return the inherited standard Codex home without creating it."""

    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode), metadata.st_nlink, metadata.st_size


def _validate_name(name: str, *, label: str) -> str:
    if (
        name in {"", ".", ".."}
        or not isinstance(name, str)
        or len(name.encode("utf-8")) > 256
        or "/" in name
        or "\x00" in name
    ):
        raise PluginCapabilityError(f"{label} contains an invalid entry name")
    return name


def _open_root(path: Path, *, label: str) -> int:
    try:
        before = path.lstat()
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise PluginCapabilityUnavailable(f"{label} is unavailable") from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode) or _identity(before)[:4] != _identity(after)[:4]:
            raise PluginCapabilityError(f"{label} identity changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent: int, name: str, *, label: str, optional: bool = False) -> int | None:
    _validate_name(name, label=label)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise PluginCapabilityError(f"{label} must not be a symlink")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if optional:
            return None
        raise PluginCapabilityUnavailable(f"{label} is unavailable") from None
    except OSError as exc:
        if optional and exc.errno == errno.ENOENT:
            return None
        raise PluginCapabilityError(f"{label} is not a safe directory") from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode) or _identity(before)[:4] != _identity(after)[:4]:
            raise PluginCapabilityError(f"{label} identity changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_names(descriptor: int, budget: _ReadBudget, *, label: str) -> tuple[str, ...]:
    before = os.fstat(descriptor)
    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError as exc:
        raise PluginCapabilityUnavailable(f"{label} cannot be enumerated") from exc
    after = os.fstat(descriptor)
    if _identity(before)[:4] != _identity(after)[:4]:
        raise PluginCapabilityError(f"{label} identity changed during enumeration")
    for name in names:
        _validate_name(name, label=label)
    budget.add_entries(len(names))
    return names


def _read_file_at(
    parent: int,
    name: str,
    *,
    limit: int,
    label: str,
    budget: _ReadBudget,
    optional: bool = False,
) -> bytes | None:
    _validate_name(name, label=label)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise PluginCapabilityError(f"{label} must not be a symlink")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if optional:
            return None
        raise PluginCapabilityUnavailable(f"{label} is unavailable") from None
    except OSError as exc:
        raise PluginCapabilityError(f"{label} is not a safe regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if opened.st_size > limit:
            raise PluginCapabilityError(f"{label} exceeds its byte bound")
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _identity(before) != _identity(opened):
            raise PluginCapabilityError(f"{label} must be an identity-stable single-link regular file")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise PluginCapabilityError(f"{label} changed during its bounded read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise PluginCapabilityError(f"{label} grew during its bounded read")
        if _identity(opened) != _identity(os.fstat(descriptor)):
            raise PluginCapabilityError(f"{label} identity changed during its bounded read")
        content = b"".join(chunks)
        budget.add_bytes(len(content))
        return content
    finally:
        os.close(descriptor)


def _read_bundle(descriptor: int, budget: _ReadBudget) -> _BundleContents:
    files: dict[str, bytes] = {}
    directories: set[str] = {""}
    total = 0

    def visit(current: int, relative: str, depth: int) -> None:
        nonlocal total
        if depth > _MAX_BUNDLE_DEPTH:
            raise PluginCapabilityError("plugin bundle exceeds its depth bound")
        for name in _entry_names(current, budget, label="plugin bundle"):
            child_relative = f"{relative}/{name}" if relative else name
            try:
                metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError as exc:
                raise PluginCapabilityUnavailable("plugin bundle entry disappeared") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PluginCapabilityError("plugin bundle contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                child = _open_directory_at(current, name, label="plugin bundle directory")
                assert child is not None
                directories.add(child_relative)
                try:
                    visit(child, child_relative, depth + 1)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PluginCapabilityError("plugin bundle contains a non-regular or hardlinked file")
            if len(files) >= _MAX_BUNDLE_FILES:
                raise PluginCapabilityError("plugin bundle exceeds its file-count bound")
            content = _read_file_at(
                current,
                name,
                limit=_MAX_SINGLE_FILE_BYTES,
                label="plugin bundle file",
                budget=budget,
            )
            assert content is not None
            total += len(content)
            if total > _MAX_BUNDLE_BYTES:
                raise PluginCapabilityError("plugin bundle exceeds its total-byte bound")
            files[child_relative] = content

    visit(descriptor, "", 1)
    digest = hashlib.sha256()
    for relative, content in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return _BundleContents(digest.hexdigest(), files, frozenset(directories))


def bundle_digest(plugin_path: Path) -> str:
    """Hash one bounded plugin directory using descriptor-relative reads."""

    descriptor = _open_root(plugin_path, label="plugin bundle")
    try:
        return _read_bundle(descriptor, _ReadBudget()).digest
    finally:
        os.close(descriptor)


def _reject_secrets(value: object, *, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PluginCapabilityError(f"{path} contains a non-string key")
            if _SENSITIVE_KEY.search(key):
                raise PluginCapabilityError(f"{path} contains a secret-bearing field")
            _reject_secrets(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) > _MAX_MANIFEST_ITEMS:
            raise PluginCapabilityError(f"{path} contains too many entries")
        for index, child in enumerate(value):
            _reject_secrets(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise PluginCapabilityError(f"{path} contains a secret-bearing value")
        if re.search(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", value):
            raise PluginCapabilityError(f"{path} contains a secret-bearing value")
        if re.search(r"(?i)(?:^|\s)(?:sk-|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{8,}", value):
            raise PluginCapabilityError(f"{path} contains a secret-bearing value")
        if re.search(r"(?i)(?:authorization|credential|password|secret|token|api[_-]?key|auth)[=:]\s*\S+", value):
            raise PluginCapabilityError(f"{path} contains a secret-bearing value")


def _text(value: object, *, label: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit or "\x00" in value:
        raise PluginCapabilityError(f"{label} must be a bounded non-empty string")
    return value


def _ids(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_MANIFEST_ITEMS:
        raise PluginCapabilityError(f"{label} must be a bounded array")
    result = tuple(_text(item, label=label, limit=256) for item in value)
    if len(set(result)) != len(result):
        raise PluginCapabilityError(f"{label} must contain unique ids")
    return tuple(sorted(result))


def _manifest(contents: _BundleContents) -> dict[str, object]:
    candidates = [name for name in ("plugin.json", ".codex-plugin/plugin.json") if name in contents.files]
    if len(candidates) != 1:
        raise PluginCapabilityUnavailable("plugin manifest is missing or ambiguous")
    raw = contents.files[candidates[0]]
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise PluginCapabilityError("plugin manifest exceeds its byte bound")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginCapabilityError("plugin manifest is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise PluginCapabilityError("plugin manifest must be an object")
    _reject_secrets(decoded)
    return decoded


def _config_plugins(home_descriptor: int, budget: _ReadBudget) -> Mapping[str, object] | None:
    raw = _read_file_at(
        home_descriptor,
        "config.toml",
        limit=_MAX_HOME_CONFIG_BYTES,
        label="native Codex config",
        budget=budget,
        optional=True,
    )
    if raw is None:
        return None
    try:
        decoded = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PluginCapabilityError("native Codex config is not valid TOML") from exc
    plugins = decoded.get("plugins") if isinstance(decoded, dict) else None
    return plugins if isinstance(plugins, Mapping) else None


def _config_enabled(plugins: Mapping[str, object] | None, canonical_id: str) -> bool | None:
    if plugins is None:
        return None
    value = plugins.get(canonical_id)
    if value is None and "@" in canonical_id:
        value = plugins.get(canonical_id.split("@", 1)[0])
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    return False


def _skill_root(manifest: Mapping[str, object], contents: _BundleContents) -> tuple[str, tuple[str, ...]]:
    declared = manifest.get("bundled_skill_ids", manifest.get("bundled_skills", manifest.get("skills", [])))
    if isinstance(declared, str):
        path = PurePosixPath(declared)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginCapabilityError("bundled skills path escapes plugin bundle")
        root = path.as_posix().rstrip("/")
        if root not in contents.directories:
            raise PluginCapabilityUnavailable("bundled skills directory is unavailable")
        prefix = root + "/"
        skills = sorted(
            child[len(prefix) :]
            for child in contents.directories
            if child.startswith(prefix) and "/" not in child[len(prefix) :]
        )
        return root, _ids(skills, label="bundled skill ids")
    return "skills", _ids(declared, label="bundled skill ids")


def _snapshot(
    candidate: _Candidate, contents: _BundleContents, plugins: Mapping[str, object] | None
) -> tuple[PluginCapabilitySnapshot, str]:
    manifest = _manifest(contents)
    manifest_name = _text(
        manifest.get("canonical_id", manifest.get("id", manifest.get("name"))),
        label="plugin canonical id",
        limit=_MAX_PLUGIN_ID_BYTES,
    )
    if manifest_name.split("@", 1)[0] != candidate.expected_name:
        raise PluginCapabilityError("plugin manifest id does not match its directory")
    version = _text(manifest.get("version"), label="plugin version", limit=128)
    if candidate.expected_version is not None and version != candidate.expected_version:
        raise PluginCapabilityError("plugin manifest version does not match its cache directory")
    if _PLUGIN_ID.fullmatch(candidate.canonical_id) is None:
        raise PluginCapabilityError("plugin canonical id is malformed")
    declared_digest = manifest.get("bundle_digest", manifest.get("bundle_sha256"))
    if declared_digest is not None and declared_digest != contents.digest:
        raise PluginCapabilityError("plugin bundle digest does not match its installed bytes")
    skill_root, skill_ids = _skill_root(manifest, contents)
    connector_ids = _ids(
        manifest.get("mcp_connectors", manifest.get("connectors", manifest.get("mcp", []))),
        label="MCP connector ids",
    )
    manifest_enabled = manifest.get("enabled")
    if manifest_enabled is not None and not isinstance(manifest_enabled, bool):
        raise PluginCapabilityError("plugin enabled state must be boolean")
    configured = _config_enabled(plugins, candidate.canonical_id)
    enabled = bool(manifest_enabled) if manifest_enabled is not None else configured is True
    if configured is not None:
        enabled = enabled and configured
    flags = tuple(manifest.get(name, False) for name in ("setup_required", "unsupported", "app_only"))
    if any(not isinstance(value, bool) for value in flags):
        raise PluginCapabilityError("plugin readiness flags must be boolean")
    setup_required, unsupported, app_only = flags
    if app_only or candidate.source.lower() in {"app", "app_only", "desktop", "codex_app"} or unsupported:
        readiness = PluginReadiness.UNSUPPORTED
    elif setup_required:
        readiness = PluginReadiness.SETUP_REQUIRED
    elif enabled:
        readiness = PluginReadiness.READY
    else:
        readiness = PluginReadiness.UNKNOWN
    return (
        PluginCapabilitySnapshot(
            candidate.canonical_id,
            version,
            candidate.source,
            enabled,
            contents.digest,
            skill_ids,
            connector_ids,
            readiness,
        ),
        skill_root,
    )


def _directory_candidates(home_descriptor: int, budget: _ReadBudget) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    def add(descriptor: int, canonical_id: str, source: str, name: str, version: str | None = None) -> None:
        budget.add_candidate()
        candidates.append(_Candidate(descriptor, canonical_id, source, name, version))

    plugins = _open_directory_at(home_descriptor, "plugins", label="native plugin discovery directory", optional=True)
    if plugins is not None:
        try:
            for name in _entry_names(plugins, budget, label="native plugin discovery directory"):
                if name == "cache":
                    continue
                try:
                    child = _open_directory_at(plugins, name, label="flat plugin bundle")
                except PluginCapabilityError:
                    metadata = os.stat(name, dir_fd=plugins, follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise
                    continue
                assert child is not None
                add(child, name, "bundled", name)
            cache = _open_directory_at(plugins, "cache", label="plugin cache", optional=True)
            if cache is not None:
                try:
                    for marketplace in _entry_names(cache, budget, label="plugin cache"):
                        market = _open_directory_at(cache, marketplace, label="plugin cache marketplace")
                        assert market is not None
                        try:
                            for name in _entry_names(market, budget, label="plugin cache marketplace"):
                                plugin = _open_directory_at(market, name, label="plugin cache name")
                                assert plugin is not None
                                try:
                                    for version in _entry_names(plugin, budget, label="plugin cache versions"):
                                        bundle = _open_directory_at(plugin, version, label="plugin cache bundle")
                                        assert bundle is not None
                                        add(bundle, f"{name}@{marketplace}", marketplace, name, version)
                                finally:
                                    os.close(plugin)
                        finally:
                            os.close(market)
                finally:
                    os.close(cache)
        finally:
            os.close(plugins)
    marketplaces = _open_directory_at(home_descriptor, "local-marketplaces", label="local marketplaces", optional=True)
    if marketplaces is not None:
        try:
            for marketplace in _entry_names(marketplaces, budget, label="local marketplaces"):
                market = _open_directory_at(marketplaces, marketplace, label="local marketplace")
                assert market is not None
                try:
                    plugins = _open_directory_at(market, "plugins", label="local marketplace plugins", optional=True)
                    if plugins is None:
                        continue
                    try:
                        for name in _entry_names(plugins, budget, label="local marketplace plugins"):
                            bundle = _open_directory_at(plugins, name, label="local marketplace bundle")
                            assert bundle is not None
                            add(bundle, f"{name}@{marketplace}", marketplace, name)
                    finally:
                        os.close(plugins)
                finally:
                    os.close(market)
        finally:
            os.close(marketplaces)
    return candidates


def _discover(
    home: Path,
    *,
    selected: tuple[str, ...] | None,
    bind_requirement: PluginRequirement | None = None,
) -> tuple[tuple[PluginCapabilitySnapshot, ...], _VerifiedSkillBinding | None]:
    budget = _ReadBudget()
    home_descriptor = _open_root(home, label="standard Codex home")
    candidates: list[_Candidate] = []
    binding: _VerifiedSkillBinding | None = None
    try:
        plugins = _config_plugins(home_descriptor, budget)
        candidates = _directory_candidates(home_descriptor, budget)
        entries: list[PluginCapabilitySnapshot] = []
        seen: set[str] = set()
        for candidate in candidates:
            if selected is not None and candidate.canonical_id not in selected:
                continue
            if candidate.canonical_id in seen:
                raise PluginCapabilityError("plugin canonical identity is ambiguous")
            contents = _read_bundle(candidate.descriptor, budget)
            snapshot, skill_root = _snapshot(candidate, contents, plugins)
            seen.add(snapshot.canonical_id)
            entries.append(snapshot)
            if bind_requirement is not None and snapshot.canonical_id == bind_requirement.canonical_id:
                skill_name = bind_requirement.bundled_skill_ids[0]
                current = os.dup(candidate.descriptor)
                try:
                    for part in PurePosixPath(skill_root, skill_name).parts:
                        child = _open_directory_at(current, part, label="bundled skill directory")
                        assert child is not None
                        os.close(current)
                        current = child
                    descriptor_path = f"/proc/self/fd/{current}"
                    if _identity(os.stat(descriptor_path))[:4] != _identity(os.fstat(current))[:4]:
                        raise PluginCapabilityError("held bundled skill descriptor identity changed")
                    binding = _VerifiedSkillBinding(current, SkillInput(skill_name, descriptor_path))
                    current = -1
                finally:
                    if current >= 0:
                        os.close(current)
        if selected is not None:
            missing = sorted(set(selected) - {item.canonical_id for item in entries})
            if missing:
                raise PluginCapabilityUnavailable(f"required plugin is not installed: {missing!r}")
        return tuple(sorted(entries, key=lambda item: item.canonical_id)), binding
    except BaseException:
        if binding is not None:
            binding.close()
        raise
    finally:
        for candidate in candidates:
            os.close(candidate.descriptor)
        os.close(home_descriptor)


def discover_plugin_capabilities(
    home: Path | str | None = None, *, plugin_ids: Sequence[str] | None = None
) -> tuple[PluginCapabilitySnapshot, ...]:
    """Discover bounded installed plugin bundles under the inherited home."""

    selected = None if plugin_ids is None else tuple(plugin_ids)
    if selected is not None and (
        len(set(selected)) != len(selected) or any(_PLUGIN_ID.fullmatch(item) is None for item in selected)
    ):
        raise PluginCapabilityError("plugin ids must be unique canonical identifiers")
    snapshots, binding = _discover(Path(home) if home is not None else standard_codex_home(), selected=selected)
    assert binding is None
    return snapshots


def _check_requirement(requirement: PluginRequirement, snapshot: PluginCapabilitySnapshot) -> None:
    if not snapshot.enabled:
        raise PluginCapabilityUnavailable(f"required plugin is disabled: {requirement.canonical_id}")
    if snapshot.readiness is PluginReadiness.SETUP_REQUIRED:
        raise PluginCapabilityUnavailable(f"required plugin needs setup: {requirement.canonical_id}")
    if snapshot.readiness is PluginReadiness.UNSUPPORTED:
        raise PluginCapabilityUnavailable(f"required plugin is unsupported: {requirement.canonical_id}")
    if snapshot.readiness is not PluginReadiness.READY:
        raise PluginCapabilityUnavailable(f"required plugin readiness is unknown: {requirement.canonical_id}")
    if snapshot.version != requirement.version or snapshot.source != requirement.source:
        raise PluginCapabilityUnavailable(f"required plugin identity changed: {requirement.canonical_id}")
    if snapshot.bundle_digest != requirement.bundle_digest:
        raise PluginCapabilityUnavailable(f"required plugin bytes changed: {requirement.canonical_id}")
    if not set(requirement.bundled_skill_ids).issubset(snapshot.bundled_skill_ids):
        raise PluginCapabilityUnavailable(f"required bundled skill is unavailable: {requirement.canonical_id}")
    if not set(requirement.mcp_connectors).issubset(snapshot.mcp_connectors):
        raise PluginCapabilityUnavailable(f"required MCP connector is unavailable: {requirement.canonical_id}")


def resolve_plugin_requirements(
    requirements: Sequence[PluginRequirement], home: Path | str | None = None
) -> tuple[PluginCapabilitySnapshot, ...]:
    """Resolve every explicit typed requirement and fail closed on drift."""

    typed = tuple(requirements)
    if any(not isinstance(item, PluginRequirement) for item in typed):
        raise PluginCapabilityError("plugin requirements must be typed records")
    snapshots = discover_plugin_capabilities(home, plugin_ids=[item.canonical_id for item in typed]) if typed else ()
    by_id = {item.canonical_id: item for item in snapshots}
    for requirement in typed:
        _check_requirement(requirement, by_id[requirement.canonical_id])
    return tuple(by_id[item.canonical_id] for item in typed)


def verify_plugin_requirements(
    requirements: Sequence[PluginRequirement], home: Path | str | None = None
) -> tuple[PluginCapabilitySnapshot, ...]:
    """Re-read plugin identity, readiness, and bytes at an execution boundary."""

    return resolve_plugin_requirements(requirements, home)


@contextmanager
def _verified_skill_input(
    requirements: Sequence[PluginRequirement],
    expected_snapshots: Sequence[PluginCapabilitySnapshot],
    home: Path | str | None = None,
) -> Iterator[SkillInput | None]:
    """Hold the one verified skill descriptor through SDK input consumption."""

    typed = tuple(requirements)
    expected = tuple(expected_snapshots)
    if not typed:
        if expected:
            raise PluginCapabilityError("plugin capability snapshot exists without a requirement")
        yield None
        return
    if len(typed) != 1 or len(expected) != 1 or len(typed[0].bundled_skill_ids) != 1 or typed[0].mcp_connectors:
        raise PluginCapabilityUnavailable("v2 execution requires exactly one plugin and one bundled skill")
    snapshots, binding = _discover(
        Path(home) if home is not None else standard_codex_home(),
        selected=(typed[0].canonical_id,),
        bind_requirement=typed[0],
    )
    if snapshots != expected:
        if binding is not None:
            binding.close()
        raise PluginCapabilityUnavailable("required plugin snapshot or capability digest changed")
    _check_requirement(typed[0], snapshots[0])
    if binding is None:
        raise PluginCapabilityUnavailable("required bundled skill descriptor is unavailable")
    try:
        yield binding.input
    finally:
        binding.close()


def plugin_capability_digest(snapshot: PluginCapabilitySnapshot) -> str:
    """Return the canonical digest bound to a sanitized snapshot."""

    if not isinstance(snapshot, PluginCapabilitySnapshot):
        raise PluginCapabilityError("plugin capability digest requires a typed snapshot")
    return snapshot.capability_digest


__all__ = [
    "PluginCapabilityError",
    "PluginCapabilityUnavailable",
    "bundle_digest",
    "discover_plugin_capabilities",
    "plugin_capability_digest",
    "resolve_plugin_requirements",
    "standard_codex_home",
    "verify_plugin_requirements",
]
