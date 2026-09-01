"""Fail-closed inspection of the native Codex agent configuration.

The H6 shared-session route reads the user's standard Codex home only to bind
immutable provider, discovery and permission facts.  It never copies
authentication or creates a private replacement home.  Controller-owned
execution policy is deliberately excluded and supplied through the SDK thread
boundary instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from .domain import JsonObject, NativePermissionAuthority, NativePermissionMode

_MAX_CONFIG_BYTES = 1_048_576
_MAX_MODEL_CATALOG_BYTES = 8_388_608
_MAX_DISCOVERY_ENTRIES = 100_000
_MAX_DISCOVERY_DEPTH = 64
_MAX_DISCOVERY_PATH_BYTES = 4_096
_MAX_DISCOVERY_FILE_BYTES = 536_870_912
_MAX_DISCOVERY_TOTAL_BYTES = 2_147_483_648
_MAX_DISCOVERY_SYMLINK_BYTES = 4_096
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RUNTIME_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|proxy[_-]?authorization|cookie|password|secret|credential|bearer|"
    r"client[_-]?secret|api[_-]?key|access[_-]?token|auth[_-]?token|(?:^|[_-])token(?:$|[_-]))",
    re.I,
)

_REQUIRED_PRESERVED_TOP_LEVEL = frozenset(
    {
        "approval_policy",
        "approvals_reviewer",
        "features",
        "marketplaces",
        "mcp_servers",
        "model_catalog_json",
        "model_provider",
        "model_providers",
        "personality",
        "plugins",
        "profiles",
        "projects",
        "skills",
        "sandbox_mode",
    }
)
_OPTIONAL_PRESERVED_TOP_LEVEL = frozenset({"agents", "hooks", "shell_environment_policy"})
_PRESERVED_TOP_LEVEL = _REQUIRED_PRESERVED_TOP_LEVEL | _OPTIONAL_PRESERVED_TOP_LEVEL
_CONTROLLER_OWNED_TOP_LEVEL = frozenset(
    {
        "model",
        "model_reasoning_effort",
        "plan_mode_reasoning_effort",
        "service_tier",
    }
)
_NON_AGENT_TOP_LEVEL = frozenset({"desktop", "notice", "tui"})
_PROVIDER_KEYS = frozenset(
    {
        "base_url",
        "env_key",
        "name",
        "requires_openai_auth",
        "supports_websockets",
        "wire_api",
    }
)
_MCP_SERVER_KEYS = frozenset(
    {
        "args",
        "bearer_token_env_var",
        "command",
        "cwd",
        "disabled_tools",
        "enabled",
        "enabled_tools",
        "env",
        "env_http_headers",
        "env_vars",
        "http_headers",
        "required",
        "startup_timeout_sec",
        "tool_timeout_sec",
        "url",
    }
)
_MCP_STRING_FIELDS = frozenset({"bearer_token_env_var", "command", "cwd", "url"})
_MCP_STRING_LIST_FIELDS = frozenset({"args", "disabled_tools", "enabled_tools", "env_vars"})
_MCP_BOOLEAN_FIELDS = frozenset({"enabled", "required"})
_MCP_TIMEOUT_FIELDS = frozenset({"startup_timeout_sec", "tool_timeout_sec"})
_MAX_MCP_COLLECTION_ENTRIES = 4_096
# MCP timeout values are persisted in the native TOML projection and later
# consumed by the pinned runtime.  Keep that boundary finite and bounded
# without coercing arbitrary Python integers through ``float`` (which can
# raise ``OverflowError`` for otherwise valid TOML integers).
_MAX_MCP_TIMEOUT_SECONDS = 86_400
_DISCOVERY_DIRECTORIES = ("memories", "plugins", "skills")

TomlValue: TypeAlias = str | int | float | bool | list["TomlValue"] | dict[str, "TomlValue"]


class NativeProfileError(ValueError):
    """The native profile cannot be safely projected into the sealed child."""


class NativeDiscoveryCompatibilityError(NativeProfileError):
    """A native discovery snapshot cannot be captured or verified safely."""


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    path: Path
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    """Bounded recursive identity for one native discovery directory."""

    root: SourceIdentity
    ancestor_identities: tuple[tuple[int, int, int, int, int], ...]
    sha256: str
    entry_count: int
    total_file_bytes: int
    max_depth: int


@dataclass(frozen=True, slots=True)
class DiscoveryMount:
    source: Path
    target_name: str
    identity: DiscoverySnapshot


@dataclass(frozen=True, slots=True)
class NativeProfileProjection:
    """Immutable, sanitized native configuration prepared for one SDK child."""

    source_home: Path
    config_source: SourceIdentity
    model_catalog_source: SourceIdentity
    discovery_mounts: tuple[DiscoveryMount, ...]
    projected_toml: str
    provider_id: str
    provider_name: str
    provider_base_url: str
    provider_wire_api: str
    provider_env_key: str
    provider_requires_openai_auth: bool
    provider_supports_websockets: bool
    native_sandbox_mode: str
    native_approval_policy: str
    projected_config_sha256: str
    worker_compatibility_sha256: str
    compatibility_sha256: str
    profile_sha256: str
    ephemeral_environment: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def load(
        cls,
        source_home: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> NativeProfileProjection:
        home = _real_directory(source_home, label="native Codex home")
        config_path = home / "config.toml"
        raw, config_identity = _read_regular(
            config_path,
            _MAX_CONFIG_BYTES,
            label="native Codex config",
            require_private_permissions=True,
        )
        try:
            decoded = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise NativeProfileError("native Codex config is not valid UTF-8 TOML") from exc
        if not isinstance(decoded, dict):
            raise NativeProfileError("native Codex config must be a TOML table")
        data = cast(dict[str, object], decoded)
        _validate_top_level(data)
        projected_data, ephemeral_environment = _project_native_config(data)
        _reject_secret_fields(projected_data)

        provider_id = _required_string(data, "model_provider")
        providers = _required_table(data, "model_providers")
        if provider_id not in providers:
            raise NativeProfileError("native profile does not define its selected model provider")
        for candidate_id in sorted(providers):
            candidate = _table_value(providers, candidate_id, label=f"model provider {candidate_id!r}")
            if set(candidate) != _PROVIDER_KEYS:
                raise NativeProfileError("native model provider has an ambiguous or unsupported shape")
            _validated_provider_url(_required_string(candidate, "base_url"))
            candidate_env_key = _required_string(candidate, "env_key")
            if _ENV_KEY.fullmatch(candidate_env_key) is None:
                raise NativeProfileError("native model provider env_key is not a key reference")
            _required_string(candidate, "name")
            if _required_string(candidate, "wire_api") not in {"responses", "chat"}:
                raise NativeProfileError("native model provider uses an unsupported wire API")
            _required_bool(candidate, "requires_openai_auth")
            _required_bool(candidate, "supports_websockets")
        provider = _table_value(providers, provider_id, label="selected model provider")
        if set(provider) != _PROVIDER_KEYS:
            raise NativeProfileError("selected model provider has an ambiguous or unsupported shape")
        provider_name = _required_string(provider, "name")
        provider_base_url = _validated_provider_url(_required_string(provider, "base_url"))
        provider_wire_api = _required_string(provider, "wire_api")
        if provider_wire_api not in {"responses", "chat"}:
            raise NativeProfileError("selected model provider uses an unsupported wire API")
        provider_env_key = _required_string(provider, "env_key")
        if _ENV_KEY.fullmatch(provider_env_key) is None:
            raise NativeProfileError("selected model provider env_key is not a key reference")
        effective_environment = os.environ if environment is None else environment
        if not effective_environment.get(provider_env_key):
            raise NativeProfileError("selected model provider key reference is unavailable")
        requires_auth = _required_bool(provider, "requires_openai_auth")
        supports_websockets = _required_bool(provider, "supports_websockets")
        native_sandbox_mode = _required_string(data, "sandbox_mode")
        if native_sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise NativeProfileError("native sandbox_mode is unsupported")
        native_approval_policy = _required_string(data, "approval_policy")
        if native_approval_policy not in {"untrusted", "on-request", "never"}:
            raise NativeProfileError("native approval_policy is unsupported")

        catalog_path = Path(_required_string(data, "model_catalog_json"))
        if not catalog_path.is_absolute():
            raise NativeProfileError("native model catalog path must be absolute")
        _, catalog_identity = _read_regular(
            catalog_path,
            _MAX_MODEL_CATALOG_BYTES,
            label="native model catalog",
            require_private_permissions=False,
        )
        mounts = tuple(
            DiscoveryMount(
                source=home / name,
                target_name=name,
                identity=_discovery_snapshot(home / name, label=f"native {name} discovery surface"),
            )
            for name in _DISCOVERY_DIRECTORIES
        )

        preserved = {
            key: _toml_value(projected_data[key], path=key)
            for key in sorted(_PRESERVED_TOP_LEVEL)
            if key in projected_data
        }
        projected_toml = _render_toml(preserved)
        projected_digest = hashlib.sha256(projected_toml.encode("utf-8")).hexdigest()
        facts: JsonObject = {
            "schema": "codex-flow/native-profile/v2",
            "provider": {
                "id": provider_id,
                "name": provider_name,
                "base_url": provider_base_url,
                "wire_api": provider_wire_api,
                "env_key": provider_env_key,
                "requires_openai_auth": requires_auth,
                "supports_websockets": supports_websockets,
            },
            "permissions": {
                "sandbox_mode": native_sandbox_mode,
                "approval_policy": native_approval_policy,
                "source": "native_config",
            },
            "model_catalog_sha256": catalog_identity.sha256,
            "projected_config_sha256": projected_digest,
            "discovery_surfaces": [mount.target_name for mount in mounts],
            "controller_overrides": ["cwd", "model", "reasoning_effort", "workspace"],
            "private_mutable_state": True,
        }
        compatibility_facts = {
            key: value for key, value in facts.items() if key not in {"permissions", "projected_config_sha256"}
        }
        compatibility_preserved = {
            key: value for key, value in preserved.items() if key not in {"approval_policy", "sandbox_mode"}
        }
        compatibility_facts["compatible_config_sha256"] = hashlib.sha256(
            _render_toml(compatibility_preserved).encode("utf-8")
        ).hexdigest()
        worker_compatibility_digest = hashlib.sha256(
            json.dumps(
                compatibility_facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        compatibility_facts["discovery_identities"] = [
            {
                "name": mount.target_name,
                "device": mount.identity.root.device,
                "inode": mount.identity.root.inode,
                "mode": mount.identity.root.mode,
                "links": mount.identity.root.links,
                "modified_ns": mount.identity.root.modified_ns,
                "recursive_sha256": mount.identity.sha256,
                "entry_count": mount.identity.entry_count,
                "total_file_bytes": mount.identity.total_file_bytes,
                "max_depth": mount.identity.max_depth,
                "ancestor_sha256": hashlib.sha256(
                    json.dumps(mount.identity.ancestor_identities, separators=(",", ":"), allow_nan=False).encode(
                        "ascii"
                    )
                ).hexdigest(),
            }
            for mount in mounts
        ]
        compatibility_digest = hashlib.sha256(
            json.dumps(
                compatibility_facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        profile_digest = hashlib.sha256(
            json.dumps(facts, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        result = cls(
            home,
            config_identity,
            catalog_identity,
            mounts,
            projected_toml,
            provider_id,
            provider_name,
            provider_base_url,
            provider_wire_api,
            provider_env_key,
            requires_auth,
            supports_websockets,
            native_sandbox_mode,
            native_approval_policy,
            projected_digest,
            worker_compatibility_digest,
            compatibility_digest,
            profile_digest,
            ephemeral_environment,
        )
        result.verify_sources()
        return result

    @property
    def sanitized_facts(self) -> JsonObject:
        return {
            "schema": "codex-flow/native-profile/v2",
            "provider": {
                "id": self.provider_id,
                "name": self.provider_name,
                "base_url": self.provider_base_url,
                "wire_api": self.provider_wire_api,
                "env_key": self.provider_env_key,
                "requires_openai_auth": self.provider_requires_openai_auth,
                "supports_websockets": self.provider_supports_websockets,
            },
            "permissions": {
                "sandbox_mode": self.native_sandbox_mode,
                "approval_policy": self.native_approval_policy,
                "source": "native_config",
            },
            "model_catalog_sha256": self.model_catalog_source.sha256,
            "projected_config_sha256": self.projected_config_sha256,
            "worker_compatibility_sha256": self.worker_compatibility_sha256,
            "compatibility_sha256": self.compatibility_sha256,
            "profile_sha256": self.profile_sha256,
            "discovery_surfaces": [mount.target_name for mount in self.discovery_mounts],
            "controller_overrides": ["cwd", "model", "reasoning_effort", "workspace"],
            "private_mutable_state": True,
        }

    def verify_sources(self) -> None:
        """Reject source substitution or mutation between projection and launch."""

        _, current_config = _read_regular(
            self.config_source.path,
            _MAX_CONFIG_BYTES,
            label="native Codex config",
            require_private_permissions=True,
        )
        _, current_catalog = _read_regular(
            self.model_catalog_source.path,
            _MAX_MODEL_CATALOG_BYTES,
            label="native model catalog",
            require_private_permissions=False,
        )
        if current_config != self.config_source or current_catalog != self.model_catalog_source:
            raise NativeProfileError("native profile source changed during launch")
        for mount in self.discovery_mounts:
            if (
                _discovery_snapshot(mount.source, label=f"native {mount.target_name} discovery surface")
                != mount.identity
            ):
                raise NativeDiscoveryCompatibilityError("native discovery surface changed during launch")

    def verify_worker_sources(self) -> None:
        """Verify launch authority while allowing ambient discovery edits.

        H6-E workers have no named discovery capability. Their compatibility
        identity protects config, provider, model catalog and discovery mount
        topology, but does not pin unrelated recursive discovery contents.
        """

        _, current_config = _read_regular(
            self.config_source.path,
            _MAX_CONFIG_BYTES,
            label="native Codex config",
            require_private_permissions=True,
        )
        _, current_catalog = _read_regular(
            self.model_catalog_source.path,
            _MAX_MODEL_CATALOG_BYTES,
            label="native model catalog",
            require_private_permissions=False,
        )
        if current_config != self.config_source or current_catalog != self.model_catalog_source:
            raise NativeProfileError("native profile source changed during launch")
        for mount in self.discovery_mounts:
            descriptor, ancestors = _open_absolute_directory(
                mount.source, label=f"native {mount.target_name} discovery surface"
            )
            try:
                current = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            root = mount.identity.root
            if ancestors != mount.identity.ancestor_identities or (
                current.st_dev,
                current.st_ino,
                current.st_mode,
            ) != (root.device, root.inode, root.mode):
                raise NativeDiscoveryCompatibilityError("native discovery mount changed during launch")

    def effective_authority(self, requested: NativePermissionMode) -> NativePermissionAuthority:
        """Return native permissions or a controller-requested monotonic restriction."""

        sandbox_mode = self.native_sandbox_mode if requested is NativePermissionMode.INHERIT_NATIVE else "read-only"
        authority = NativePermissionAuthority(requested, sandbox_mode, self.native_approval_policy)
        native = NativePermissionAuthority(
            NativePermissionMode.INHERIT_NATIVE,
            self.native_sandbox_mode,
            self.native_approval_policy,
        )
        if authority.meet(native) != authority:
            raise NativeProfileError("requested permissions would broaden native authority")
        return authority

    def effective_permissions(self, requested: NativePermissionMode) -> JsonObject:
        """Return native permissions or a controller-requested monotonic restriction."""

        facts = self.effective_authority(requested).facts
        facts["native_sandbox_mode"] = self.native_sandbox_mode
        return facts

    def prepare_runtime_home(self, runtime_home: Path) -> None:
        """Create private mutable state while exposing native discovery semantics."""

        descriptor = _open_private_runtime_home(runtime_home)
        try:
            self.verify_sources()
            expected = self.projected_toml.encode("utf-8")
            try:
                current = _read_private_file_at(descriptor, "config.toml")
            except FileNotFoundError:
                _write_new_file_at(descriptor, "config.toml", expected)
            else:
                if current != expected:
                    _replace_private_profile_at(descriptor, expected)
            for mount in self.discovery_mounts:
                try:
                    metadata = os.stat(mount.target_name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    os.symlink(
                        os.fspath(mount.source),
                        mount.target_name,
                        target_is_directory=True,
                        dir_fd=descriptor,
                    )
                else:
                    if not stat.S_ISLNK(metadata.st_mode):
                        raise NativeProfileError("private native discovery path was substituted")
                    if os.readlink(mount.target_name, dir_fd=descriptor) != os.fspath(mount.source):
                        raise NativeProfileError("private native discovery link changed")
            _verify_open_directory_path(runtime_home, descriptor)
            self.verify_sources()
        finally:
            os.close(descriptor)


def _project_native_config(data: Mapping[str, object]) -> tuple[dict[str, object], tuple[tuple[str, str], ...]]:
    """Project secret-bearing native values onto process-bound environment references.

    The private ``config.toml`` is durable runtime state, so it may contain
    only references. Literal MCP HTTP headers and every stdio environment
    value stay in memory long enough to launch the SDK child.
    """

    projected = {key: _clone_native_value(value) for key, value in data.items() if key != "mcp_servers"}
    mcp_servers = data.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        raise NativeProfileError("native profile field 'mcp_servers' must be a table")
    projected_servers: dict[str, object] = {}
    projected["mcp_servers"] = projected_servers
    ephemeral: dict[str, str] = {}
    environment_names: dict[str, tuple[str, str, str]] = {}

    def register_environment_name(server_name: str, name: str, source: str) -> None:
        normalized = _normalized_environment_name(name)
        prior = environment_names.get(normalized)
        if prior is not None:
            raise NativeProfileError("native MCP environment names must be unique and unambiguous")
        environment_names[normalized] = (server_name, source, name)

    protected_environment = {"CODEX_HOME"}
    selected_provider = data.get("model_provider")
    provider_tables = data.get("model_providers")
    if isinstance(selected_provider, str) and isinstance(provider_tables, dict):
        selected_table = provider_tables.get(selected_provider)
        if isinstance(selected_table, dict) and isinstance(selected_table.get("env_key"), str):
            protected_environment.add(cast(str, selected_table["env_key"]))
    for server_name, raw_server in sorted(mcp_servers.items()):
        if (
            not isinstance(server_name, str)
            or not server_name
            or len(server_name.encode("utf-8")) > 256
            or "\x00" in server_name
            or not isinstance(raw_server, dict)
        ):
            raise NativeProfileError("native MCP server definitions must be named tables")
        if len(raw_server) > len(_MCP_SERVER_KEYS):
            raise NativeProfileError("native MCP server has an unsupported field shape")
        for key in raw_server:
            if not isinstance(key, str):
                raise NativeProfileError("native MCP server field names must be strings")
            header_kind = _header_container_kind(key)
            if header_kind is not None and key != header_kind:
                raise NativeProfileError("native MCP header tables must use their canonical key spelling")
            if key not in _MCP_SERVER_KEYS:
                if _contains_header_container(raw_server[key]):
                    raise NativeProfileError("native profile contains an unsupported header field")
                raise NativeProfileError("native MCP server contains an unsupported field")

        projected_server: dict[str, object] = {}
        projected_servers[server_name] = projected_server
        for key in sorted(_MCP_STRING_FIELDS):
            if key not in raw_server:
                continue
            value = raw_server[key]
            if not _is_bounded_string(value):
                raise NativeProfileError("native MCP string fields must be bounded non-empty strings")
            if key == "bearer_token_env_var" and _RUNTIME_ENV_KEY.fullmatch(cast(str, value)) is None:
                raise NativeProfileError("native MCP bearer token reference must name an environment key")
            if key == "bearer_token_env_var":
                register_environment_name(server_name, cast(str, value), "bearer_token_env_var")
            projected_server[key] = value
        for key in sorted(_MCP_STRING_LIST_FIELDS - {"env_vars"}):
            if key not in raw_server:
                continue
            projected_server[key] = _mcp_string_list(raw_server[key])
        for key in sorted(_MCP_BOOLEAN_FIELDS):
            if key in raw_server:
                value = raw_server[key]
                if not isinstance(value, bool):
                    raise NativeProfileError("native MCP boolean fields must be booleans")
                projected_server[key] = value
        for key in sorted(_MCP_TIMEOUT_FIELDS):
            if key in raw_server:
                value = raw_server[key]
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise NativeProfileError("native MCP timeout fields must be finite positive numbers")
                if isinstance(value, int):
                    # Do not call math.isfinite on ints: Python converts an
                    # int to float there, and a very large TOML integer can
                    # overflow before validation gets a chance to reject it.
                    valid_timeout = 0 < value <= _MAX_MCP_TIMEOUT_SECONDS
                else:
                    valid_timeout = math.isfinite(value) and 0 < value <= _MAX_MCP_TIMEOUT_SECONDS
                if not valid_timeout:
                    raise NativeProfileError("native MCP timeout fields must be finite positive numbers")
                projected_server[key] = value

        literal_headers = raw_server.get("http_headers")
        reference_headers = raw_server.get("env_http_headers", {})
        if literal_headers is not None and not isinstance(literal_headers, dict):
            raise NativeProfileError("native MCP http_headers must be a table")
        if not isinstance(reference_headers, dict):
            raise NativeProfileError("native MCP env_http_headers must be a table")
        projected_headers: dict[str, object] = {}
        if len(reference_headers) > _MAX_MCP_COLLECTION_ENTRIES:
            raise NativeProfileError("native MCP header references exceed the entry limit")
        for header_name, environment_key in sorted(reference_headers.items()):
            if not _is_bounded_string(header_name) or not isinstance(environment_key, str):
                raise NativeProfileError("native MCP header references must contain bounded string entries")
            if _RUNTIME_ENV_KEY.fullmatch(environment_key) is None:
                raise NativeProfileError("native MCP header references must name environment keys")
            register_environment_name(server_name, environment_key, "env_http_headers")
            projected_headers[cast(str, header_name)] = environment_key
        if isinstance(literal_headers, dict) and len(literal_headers) > _MAX_MCP_COLLECTION_ENTRIES:
            raise NativeProfileError("native MCP HTTP headers exceed the entry limit")
        for header_name, raw_value in sorted((literal_headers or {}).items()):
            if (
                not isinstance(header_name, str)
                or not header_name
                or len(header_name) > 16_384
                or "\x00" in header_name
                or not isinstance(raw_value, str)
                or not raw_value
                or len(raw_value) > 16_384
                or "\x00" in raw_value
            ):
                raise NativeProfileError("native MCP HTTP headers must be bounded non-empty strings")
            reference = _ephemeral_reference("HTTP", server_name, header_name)
            register_environment_name(server_name, reference, "literal_http_header")
            prior = projected_headers.get(header_name)
            if prior is not None and prior != reference:
                raise NativeProfileError("native MCP header has conflicting literal and environment sources")
            projected_headers[header_name] = reference
            _record_ephemeral(ephemeral, reference, raw_value, protected=protected_environment)
        if projected_headers:
            projected_server["env_http_headers"] = projected_headers
        elif "env_http_headers" in raw_server:
            projected_server["env_http_headers"] = {}

        literal_environment = raw_server.get("env")
        if literal_environment is not None and not isinstance(literal_environment, dict):
            raise NativeProfileError("native MCP env must be a table")
        if isinstance(literal_environment, dict) and len(literal_environment) > _MAX_MCP_COLLECTION_ENTRIES:
            raise NativeProfileError("native MCP environment exceeds the entry limit")

        raw_environment_references = raw_server.get("env_vars", [])
        if not isinstance(raw_environment_references, list):
            raise NativeProfileError("native MCP env_vars must contain environment key references")
        if len(raw_environment_references) > _MAX_MCP_COLLECTION_ENTRIES:
            raise NativeProfileError("native MCP env_vars exceed the entry limit")
        references: list[str] = []
        for environment_key in raw_environment_references:
            if not isinstance(environment_key, str) or _RUNTIME_ENV_KEY.fullmatch(environment_key) is None:
                raise NativeProfileError("native MCP env_vars must contain environment key references")
            register_environment_name(server_name, environment_key, "env_vars")
            references.append(environment_key)

        if isinstance(literal_environment, dict):
            # Every literal is treated as sensitive.  The projected TOML gets
            # only an env_vars reference; the value is retained solely in the
            # SDK child environment for this process.
            collision_alias: str | None = None
            for key, raw_value in sorted(literal_environment.items()):
                if (
                    not isinstance(key, str)
                    or _RUNTIME_ENV_KEY.fullmatch(key) is None
                    or not isinstance(raw_value, str)
                    or len(raw_value) > 16_384
                    or "\x00" in raw_value
                ):
                    raise NativeProfileError("native MCP environment values must be bounded string entries")
                register_environment_name(server_name, key, "env")
                reference = key
                if key in protected_environment:
                    if key != "CODEX_HOME":
                        _record_ephemeral(ephemeral, key, raw_value, protected=protected_environment)
                    collision_alias = _collision_environment_alias(server_name, key)
                    register_environment_name(server_name, collision_alias, "literal_env_alias")
                    _record_ephemeral(ephemeral, collision_alias, raw_value, protected=protected_environment)
                    reference = collision_alias
                else:
                    _record_ephemeral(ephemeral, key, raw_value, protected=protected_environment)
                references.append(reference)
            if collision_alias is not None:
                command = projected_server.get("command")
                args = projected_server.get("args", [])
                if not isinstance(command, str) or not command:
                    raise NativeProfileError("CODEX_HOME MCP collision requires a stdio command")
                if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
                    raise NativeProfileError("CODEX_HOME MCP collision requires a string argument list")
                # The pinned app-server only exposes env_vars as names copied
                # from its own environment; it has no alias map.  A fixed
                # argv shell shim is therefore the narrowest source-bound
                # mechanism: the SDK keeps its private CODEX_HOME, while the
                # MCP child receives the native value under its original name.
                projected_server["command"] = "/bin/sh"
                projected_server["args"] = [
                    "-c",
                    f'export CODEX_HOME="${collision_alias}"; exec "$@"',
                    "--",
                    command,
                    *args,
                ]
        if references:
            projected_server["env_vars"] = sorted(references)
        elif "env_vars" in raw_server:
            projected_server["env_vars"] = []
    return projected, tuple(sorted(ephemeral.items()))


def _clone_native_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _clone_native_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_clone_native_value(child) for child in value]
    return value


def _collapsed_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _normalized_environment_name(value: str) -> str:
    """Normalize environment aliases before any projection or persistence."""

    return _collapsed_key(value)


def _header_container_kind(value: str) -> str | None:
    collapsed = _collapsed_key(value)
    if collapsed == "httpheaders":
        return "http_headers"
    if collapsed == "envhttpheaders":
        return "env_http_headers"
    return None


def _contains_header_container(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            isinstance(key, str) and (_header_container_kind(key) is not None or _contains_header_container(child))
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_header_container(child) for child in value)
    return False


def _is_sensitive_key(value: str) -> bool:
    collapsed = _collapsed_key(value)
    word_tokens = tuple(
        token
        for token in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).casefold().replace("-", "_").split("_")
        if token
    )
    return bool(
        _SENSITIVE_KEY.search(value)
        or any(
            marker in collapsed
            for marker in (
                "authorization",
                "bearer",
                "clientsecret",
                "cookie",
                "credential",
                "password",
                "privatekey",
                "secret",
                "token",
            )
        )
        or collapsed.endswith("apikey")
        or (word_tokens and word_tokens[-1] == "key")
    )


def _is_bounded_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 16_384 and "\x00" not in value


def _mcp_string_list(value: object, *, environment_keys: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_MCP_COLLECTION_ENTRIES
        or any(not _is_bounded_string(item) for item in value)
    ):
        raise NativeProfileError("native MCP list fields must contain bounded strings")
    result = cast(list[str], _clone_native_value(value))
    if environment_keys:
        if any(_RUNTIME_ENV_KEY.fullmatch(item) is None for item in result) or len(set(result)) != len(result):
            raise NativeProfileError("native MCP env_vars must contain bounded unique environment key references")
    return result


def _ephemeral_reference(kind: str, server_name: str, field_name: str) -> str:
    identity = f"{kind}\0{server_name}\0{field_name}".encode()
    return f"CODEX_FLOW_MCP_{kind}_{hashlib.sha256(identity).hexdigest()[:24].upper()}"


def _collision_environment_alias(server_name: str, field_name: str) -> str:
    identity = f"ENV_COLLISION\0{server_name}\0{field_name}".encode()
    return f"CODEX_FLOW_MCP_COLLISION_{hashlib.sha256(identity).hexdigest()[:24].upper()}"


def _record_ephemeral(
    environment: dict[str, str],
    key: str,
    value: str,
    *,
    protected: set[str],
) -> None:
    if key in protected:
        raise NativeProfileError("native MCP secret conflicts with a controller or provider environment key")
    prior = environment.get(key)
    if prior is not None and prior != value:
        raise NativeProfileError("native MCP secrets require conflicting values for one environment key")
    environment[key] = value


def _open_private_runtime_home(path: Path) -> int:
    """Open/create an absolute directory tree without following any symlink."""

    if not path.is_absolute() or ".." in path.parts:
        raise NativeProfileError("private native runtime home must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise NativeProfileError("private native runtime home contains an unsafe ancestor") from exc
            except OSError as exc:
                raise NativeProfileError("private native runtime home contains an unsafe ancestor") from exc
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise NativeProfileError("private native runtime home must be an owned directory")
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_open_directory_path(path: Path, descriptor: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        current = os.open("/", flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            metadata = os.fstat(current)
        finally:
            os.close(current)
    except OSError as exc:
        raise NativeProfileError("private native runtime home changed during preparation") from exc
    opened = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
        raise NativeProfileError("private native runtime home changed during preparation")


def _read_private_file_at(directory: int, name: str) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise NativeProfileError("private native profile is not a safe regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise NativeProfileError("private native profile is not a safe regular file")
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > _MAX_CONFIG_BYTES:
            raise NativeProfileError("private native profile is too large")
        return value
    finally:
        os.close(descriptor)


def _write_new_file_at(directory: int, name: str, value: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_private_profile_at(directory: int, value: bytes) -> None:
    temporary = ".config.toml.codex-flow-tmp"
    try:
        os.stat(temporary, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise NativeProfileError("private native profile replacement path is unavailable")
    try:
        _write_new_file_at(directory, temporary, value)
        os.replace(temporary, "config.toml", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except Exception:
        try:
            metadata = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                os.unlink(temporary, dir_fd=directory)
        raise


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise OSError("short private native profile write")
        offset += written


def _validate_top_level(data: Mapping[str, object]) -> None:
    keys = set(data)
    missing = _REQUIRED_PRESERVED_TOP_LEVEL - keys
    unknown = keys - _PRESERVED_TOP_LEVEL - _CONTROLLER_OWNED_TOP_LEVEL - _NON_AGENT_TOP_LEVEL
    if missing:
        raise NativeProfileError(f"native profile is missing required surfaces: {', '.join(sorted(missing))}")
    if unknown:
        raise NativeProfileError(f"native profile contains unsupported surfaces: {', '.join(sorted(unknown))}")
    for key in sorted(_OPTIONAL_PRESERVED_TOP_LEVEL & keys):
        if not isinstance(data[key], dict):
            raise NativeProfileError(f"native profile optional surface {key!r} must be a table")
    if "service_tier" in data:
        _required_string(data, "service_tier")


def _reject_secret_fields(value: object, *, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise NativeProfileError("native profile table keys must be strings")
            child_path = f"{path}.{key}" if path else key
            if path.endswith(".env_http_headers"):
                if not isinstance(child, str) or _RUNTIME_ENV_KEY.fullmatch(child) is None:
                    raise NativeProfileError("native MCP header environment references must name environment keys")
                continue
            allowed_reference = key in {
                "bearer_token_env_var",
                "env_key",
                "requires_openai_auth",
            }
            if _header_container_kind(key) is not None and not (
                key == "env_http_headers" and path.startswith("mcp_servers.") and path.count(".") == 1
            ):
                raise NativeProfileError(f"native profile contains an unsupported header field: {child_path}")
            if _is_sensitive_key(key) and not allowed_reference:
                raise NativeProfileError(f"native profile contains a secret-bearing field: {child_path}")
            _reject_secret_fields(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, path=f"{path}[{index}]")


def _required_table(data: Mapping[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise NativeProfileError(f"native profile field {key!r} must be a table")
    return cast(dict[str, object], value)


def _table_value(data: Mapping[str, object], key: str, *, label: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise NativeProfileError(f"{label} must be a table")
    return cast(dict[str, object], value)


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > 16_384 or "\x00" in value:
        raise NativeProfileError(f"native profile field {key!r} must be a bounded non-empty string")
    return value


def _required_bool(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise NativeProfileError(f"native profile field {key!r} must be boolean")
    return value


def _validated_provider_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise NativeProfileError("selected model provider base_url is not a safe absolute HTTP URL")
    if parsed.query or parsed.fragment:
        raise NativeProfileError("selected model provider base_url cannot contain query or fragment data")
    return value.rstrip("/")


def _real_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise NativeProfileError(f"{label} must be absolute")
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or path.resolve() != path or metadata.st_uid != os.getuid():
        raise NativeProfileError(f"{label} must be a real directory owned by the controller user")
    if metadata.st_mode & 0o022:
        raise NativeProfileError(f"{label} cannot be group/world writable")
    return path


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...

    def digest(self) -> bytes: ...

    def hexdigest(self) -> str: ...


@dataclass(slots=True)
class _DiscoveryScanState:
    digest: _Digest
    entries: dict[bytes, tuple[bytes, bytes | None]]
    entry_count: int = 0
    total_file_bytes: int = 0
    max_depth: int = 0


def _discovery_snapshot(path: Path, *, label: str) -> DiscoverySnapshot:
    try:
        return _capture_discovery_snapshot(path, label=label)
    except NativeDiscoveryCompatibilityError:
        raise
    except NativeProfileError as exc:
        raise NativeDiscoveryCompatibilityError(str(exc)) from exc
    except OSError as exc:
        raise NativeDiscoveryCompatibilityError(f"{label} cannot be captured safely") from exc


def _capture_discovery_snapshot(path: Path, *, label: str) -> DiscoverySnapshot:
    """Hash a discovery tree through no-follow directory descriptors.

    The snapshot retains content digests and structural metadata, never file
    content. Symlinks are fingerprinted as links and must resolve, through the
    captured tree, to an entry below the discovery root.
    """

    descriptor, ancestors = _open_absolute_directory(path, label=label)
    try:
        root_before = os.fstat(descriptor)
        if not stat.S_ISDIR(root_before.st_mode) or root_before.st_uid != os.getuid() or root_before.st_mode & 0o022:
            raise NativeProfileError(f"{label} must be a controller-owned non-writable directory")
        digest = hashlib.sha256()
        entries: dict[bytes, tuple[bytes, bytes | None]] = {b"": (b"directory", None)}
        state = _DiscoveryScanState(digest, entries)
        _hash_discovery_record(digest, b"root", b"", root_before, b"")
        _scan_discovery_directory(descriptor, (), 0, state, label=label)
        _validate_discovery_symlinks(path, entries, label=label)
        root_after = os.fstat(descriptor)
        if _stable_metadata(root_before) != _stable_metadata(root_after):
            raise NativeProfileError(f"{label} changed while it was scanned")
        verification, current_ancestors = _open_absolute_directory(path, label=label)
        try:
            current_root = os.fstat(verification)
        finally:
            os.close(verification)
        if ancestors != current_ancestors or _stable_metadata(root_after) != _stable_metadata(current_root):
            raise NativeProfileError(f"{label} path changed while it was scanned")
        root = SourceIdentity(
            path,
            root_after.st_dev,
            root_after.st_ino,
            root_after.st_mode,
            root_after.st_nlink,
            root_after.st_size,
            root_after.st_mtime_ns,
            None,
        )
        return DiscoverySnapshot(
            root,
            ancestors,
            digest.hexdigest(),
            state.entry_count,
            state.total_file_bytes,
            state.max_depth,
        )
    finally:
        os.close(descriptor)


def _open_absolute_directory(path: Path, *, label: str) -> tuple[int, tuple[tuple[int, int, int, int, int], ...]]:
    if not path.is_absolute() or ".." in path.parts:
        raise NativeProfileError(f"{label} must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    identities: list[tuple[int, int, int, int, int]] = []
    try:
        for part in path.parts[1:]:
            try:
                entry = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise NativeProfileError(f"{label} contains an unsafe or unavailable ancestor") from exc
            opened = os.fstat(child)
            if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(child)
                raise NativeProfileError(f"{label} contains an unsafe or unavailable ancestor")
            identities.append((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_gid))
            os.close(descriptor)
            descriptor = child
        return descriptor, tuple(identities)
    except BaseException:
        os.close(descriptor)
        raise


def _scan_discovery_directory(
    descriptor: int,
    parent_parts: tuple[bytes, ...],
    parent_depth: int,
    state: _DiscoveryScanState,
    *,
    label: str,
) -> None:
    directory_before = os.fstat(descriptor)
    try:
        names = sorted(os.listdir(descriptor), key=os.fsencode)
    except OSError as exc:
        raise NativeProfileError(f"{label} cannot be enumerated safely") from exc
    for name in names:
        encoded_name = os.fsencode(name)
        relative_parts = (*parent_parts, encoded_name)
        relative = b"/".join(relative_parts)
        depth = parent_depth + 1
        state.entry_count += 1
        state.max_depth = max(state.max_depth, depth)
        if state.entry_count > _MAX_DISCOVERY_ENTRIES:
            raise NativeProfileError(f"{label} exceeds the discovery entry limit")
        if depth > _MAX_DISCOVERY_DEPTH:
            raise NativeProfileError(f"{label} exceeds the discovery depth limit")
        if len(relative) > _MAX_DISCOVERY_PATH_BYTES:
            raise NativeProfileError(f"{label} contains an overlong relative path")
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise NativeProfileError(f"{label} changed while it was enumerated") from exc
        if before.st_uid != os.getuid():
            raise NativeProfileError(f"{label} contains an entry not owned by the controller user")
        if stat.S_ISDIR(before.st_mode):
            child = _open_discovery_child(descriptor, name, before, label=label)
            state.entries[relative] = (b"directory", None)
            _hash_discovery_record(state.digest, b"directory", relative, before, b"")
            try:
                _scan_discovery_directory(child, relative_parts, depth, state, label=label)
                after = os.fstat(child)
            finally:
                os.close(child)
            _verify_discovery_entry(descriptor, name, before, after, label=label)
        elif stat.S_ISREG(before.st_mode):
            file_digest, after = _hash_discovery_file(descriptor, name, before, state, label=label)
            state.entries[relative] = (b"regular", None)
            _hash_discovery_record(state.digest, b"regular", relative, after, file_digest)
            _verify_discovery_entry(descriptor, name, before, after, label=label)
        elif stat.S_ISLNK(before.st_mode):
            try:
                target = os.fsencode(os.readlink(name, dir_fd=descriptor))
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise NativeProfileError(f"{label} symlink changed while it was read") from exc
            if len(target) > _MAX_DISCOVERY_SYMLINK_BYTES:
                raise NativeProfileError(f"{label} contains an overlong symlink target")
            if _stable_metadata(before) != _stable_metadata(after):
                raise NativeProfileError(f"{label} symlink changed while it was read")
            state.entries[relative] = (b"symlink", target)
            _hash_discovery_record(state.digest, b"symlink", relative, after, target)
        else:
            raise NativeProfileError(f"{label} contains an unsupported special file")
    directory_after = os.fstat(descriptor)
    if _stable_metadata(directory_before) != _stable_metadata(directory_after):
        raise NativeProfileError(f"{label} changed while it was scanned")


def _open_discovery_child(parent: int, name: str, expected: os.stat_result, *, label: str) -> int:
    try:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise NativeProfileError(f"{label} directory changed while it was opened") from exc
    opened = os.fstat(child)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(child)
        raise NativeProfileError(f"{label} directory changed while it was opened")
    return child


def _hash_discovery_file(
    parent: int,
    name: str,
    before: os.stat_result,
    state: _DiscoveryScanState,
    *,
    label: str,
) -> tuple[bytes, os.stat_result]:
    if before.st_nlink != 1:
        raise NativeProfileError(f"{label} contains a hard-linked regular file")
    if before.st_size > _MAX_DISCOVERY_FILE_BYTES:
        raise NativeProfileError(f"{label} contains a file above the per-file limit")
    state.total_file_bytes += before.st_size
    if state.total_file_bytes > _MAX_DISCOVERY_TOTAL_BYTES:
        raise NativeProfileError(f"{label} exceeds the total file-byte limit")
    try:
        child = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise NativeProfileError(f"{label} file changed while it was opened") from exc
    try:
        opened = os.fstat(child)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise NativeProfileError(f"{label} file changed while it was opened")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(child, min(remaining, 1_048_576))
            if not chunk:
                raise NativeProfileError(f"{label} file changed while it was read")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(child, 1):
            raise NativeProfileError(f"{label} file changed while it was read")
        after = os.fstat(child)
        if _stable_metadata(before) != _stable_metadata(after):
            raise NativeProfileError(f"{label} file changed while it was read")
        return digest.digest(), after
    finally:
        os.close(child)


def _verify_discovery_entry(
    parent: int,
    name: str,
    before: os.stat_result,
    after: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise NativeProfileError(f"{label} entry changed while it was scanned") from exc
    if _stable_metadata(before) != _stable_metadata(after) or _stable_metadata(after) != _stable_metadata(current):
        raise NativeProfileError(f"{label} entry changed while it was scanned")


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_discovery_record(
    digest: _Digest,
    kind: bytes,
    relative: bytes,
    metadata: os.stat_result,
    payload: bytes,
) -> None:
    for value in (
        kind,
        relative,
        json.dumps(_stable_metadata(metadata), separators=(",", ":"), allow_nan=False).encode("ascii"),
        payload,
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _validate_discovery_symlinks(
    root: Path,
    entries: Mapping[bytes, tuple[bytes, bytes | None]],
    *,
    label: str,
) -> None:
    root_bytes = os.fsencode(os.fspath(root))
    for relative, (kind, target) in entries.items():
        if kind != b"symlink" or target is None:
            continue
        pending = _symlink_target_parts(root_bytes, relative, target, label=label)
        resolved: list[bytes] = []
        active_ancestors = {
            b"/".join(posixpath.dirname(relative).split(b"/")[:depth])
            for depth in range(len(posixpath.dirname(relative).split(b"/")) + 1)
        }
        active_ancestors.add(b"")
        visited_links = {relative}
        hops = 0
        while True:
            if not pending:
                resolved_target = b"/".join(resolved)
                if resolved_target in active_ancestors:
                    raise NativeProfileError(f"{label} contains a cyclic symlink to an active ancestor")
                break
            part = pending.pop(0)
            candidate = b"/".join((*resolved, part))
            entry = entries.get(candidate)
            if entry is None:
                raise NativeProfileError(f"{label} contains a dangling symlink")
            entry_kind, entry_target = entry
            if entry_kind == b"symlink":
                hops += 1
                if hops > _MAX_DISCOVERY_DEPTH or entry_target is None or candidate in visited_links:
                    raise NativeProfileError(f"{label} contains a cyclic or over-deep symlink")
                visited_links.add(candidate)
                pending = _symlink_target_parts(root_bytes, candidate, entry_target, label=label) + pending
                resolved = []
            else:
                if pending and entry_kind != b"directory":
                    raise NativeProfileError(f"{label} symlink traverses a non-directory entry")
                resolved.append(part)


def _symlink_target_parts(root: bytes, relative: bytes, target: bytes, *, label: str) -> list[bytes]:
    if b"\x00" in target:
        raise NativeProfileError(f"{label} contains an unsafe symlink target")
    if posixpath.isabs(target):
        normalized = posixpath.normpath(target)
        if normalized == root:
            relative_target = b""
        elif normalized.startswith(root + b"/"):
            relative_target = normalized[len(root) + 1 :]
        else:
            raise NativeProfileError(f"{label} symlink escapes the discovery root")
    else:
        relative_target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
        if relative_target in {b".", b""}:
            relative_target = b""
        elif relative_target == b".." or relative_target.startswith(b"../"):
            raise NativeProfileError(f"{label} symlink escapes the discovery root")
    if len(relative_target) > _MAX_DISCOVERY_PATH_BYTES:
        raise NativeProfileError(f"{label} contains an overlong resolved symlink target")
    return [part for part in relative_target.split(b"/") if part]


def _read_regular(
    path: Path,
    limit: int,
    *,
    label: str,
    require_private_permissions: bool,
) -> tuple[bytes, SourceIdentity]:
    if not path.is_absolute():
        raise NativeProfileError(f"{label} must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeProfileError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or (require_private_permissions and before.st_mode & 0o022)
            or before.st_size > limit
        ):
            raise NativeProfileError(f"{label} is not a bounded controller-owned regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        entry = os.stat(path, follow_symlinks=False)
        stable_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            stable_before != stable_after
            or (entry.st_dev, entry.st_ino) != (after.st_dev, after.st_ino)
            or len(raw) > limit
        ):
            raise NativeProfileError(f"{label} changed while it was read")
        identity = SourceIdentity(
            path,
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            hashlib.sha256(raw).hexdigest(),
        )
        return raw, identity
    finally:
        os.close(descriptor)


def _toml_value(value: object, *, path: str) -> TomlValue:
    if isinstance(value, bool | str | int | float):
        if isinstance(value, str) and (len(value) > 16_384 or "\x00" in value):
            raise NativeProfileError(f"native profile value at {path} is unbounded")
        return value
    if isinstance(value, list):
        if len(value) > 1_024:
            raise NativeProfileError(f"native profile array at {path} is unbounded")
        return [_toml_value(item, path=f"{path}[]") for item in value]
    if isinstance(value, dict):
        if len(value) > 1_024:
            raise NativeProfileError(f"native profile table at {path} is unbounded")
        return {
            key: _toml_value(child, path=f"{path}.{key}")
            for key, child in sorted(value.items())
            if isinstance(key, str)
        }
    raise NativeProfileError(f"native profile value at {path} has an unsupported TOML type")


def _key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _scalar(value: TomlValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list) and all(not isinstance(item, dict | list) for item in value):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    raise NativeProfileError("native profile contains a non-scalar value in a scalar TOML position")


def _render_toml(data: Mapping[str, TomlValue]) -> str:
    lines: list[str] = []

    def render_table(table: Mapping[str, TomlValue], prefix: tuple[str, ...], *, header: bool) -> None:
        if header:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[" + ".".join(_key(part) for part in prefix) + "]")
        for key, value in table.items():
            if not isinstance(value, dict) and not (isinstance(value, list) and value and isinstance(value[0], dict)):
                lines.append(f"{_key(key)} = {_scalar(value)}")
        for key, value in table.items():
            if isinstance(value, dict):
                render_table(value, (*prefix, key), header=True)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                if not all(isinstance(item, dict) for item in value):
                    raise NativeProfileError("native profile contains a mixed TOML array")
                for item in value:
                    if lines and lines[-1] != "":
                        lines.append("")
                    lines.append("[[" + ".".join(_key(part) for part in (*prefix, key)) + "]]")
                    item_table = cast(dict[str, TomlValue], item)
                    for item_key, item_value in item_table.items():
                        if isinstance(item_value, dict | list):
                            raise NativeProfileError("nested tables inside array tables are unsupported")
                        lines.append(f"{_key(item_key)} = {_scalar(item_value)}")

    render_table(data, (), header=False)
    return "\n".join(lines) + "\n"


__all__ = [
    "DiscoveryMount",
    "DiscoverySnapshot",
    "NativeDiscoveryCompatibilityError",
    "NativeProfileError",
    "NativeProfileProjection",
    "SourceIdentity",
]
