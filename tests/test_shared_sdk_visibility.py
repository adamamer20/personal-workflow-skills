from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

import codex_flow.plugin_capabilities as plugin_capabilities
from codex_flow.backends.codex_sdk import (
    LEAF_WORKER_CONFIG_OVERRIDES,
    CodexSdkAdapter,
    CodexSdkConfig,
    CodexSdkNotifier,
    WakeDeliveryAmbiguous,
    WakeDeliveryUnavailable,
)
from codex_flow.contracts import PluginReadiness, PluginRequirement
from codex_flow.domain import ReasoningEffort, Sandbox, ThreadIdentity
from codex_flow.plugin_capabilities import (
    PluginCapabilityError,
    PluginCapabilityUnavailable,
    _verified_skill_input,
    bundle_digest,
    discover_plugin_capabilities,
    resolve_plugin_requirements,
)


class _Thread:
    id = "shared-thread"


class _Client:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
        self.resumes: list[tuple[str, dict[str, object]]] = []

    def thread_start(self, **kwargs: object) -> _Thread:
        self.starts.append(kwargs)
        return _Thread()

    def thread_resume(self, thread_id: str, **kwargs: object) -> _Thread:
        self.resumes.append((thread_id, kwargs))
        return _Thread()

    def thread_archive(self, _thread_id: str) -> None:
        return None

    def close(self) -> None:
        return None


class _WakeTurn:
    id = "wake-turn"


class _WakeThread:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[str] = []
        self.fail = fail

    def turn(self, payload: str) -> _WakeTurn:
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("ambiguous")
        return _WakeTurn()


class _WakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.resumes: list[str] = []
        self.thread_value = _WakeThread(fail=fail)

    def thread_resume(self, thread_id: str) -> _WakeThread:
        self.resumes.append(thread_id)
        return self.thread_value

    def close(self) -> None:
        return None


def test_shared_sdk_session_inherits_auth_and_resumes_same_visible_thread() -> None:
    with TemporaryDirectory() as directory:
        cwd = Path(directory)
        client = _Client()

        class _Sdk(SimpleNamespace):
            def CodexConfig(self, *, config_overrides: tuple[str, ...] = (), cwd: str | None = None) -> object:
                self.config = {"config_overrides": config_overrides, "cwd": cwd}
                return self.config

            def Codex(self, *, config: object) -> _Client:
                self.codex_config = config
                return client

        sdk = _Sdk(
            Sandbox=SimpleNamespace(read_only="read-only", workspace_write="workspace-write"),
            ApprovalMode=SimpleNamespace(deny_all="deny-all"),
            ReasoningEffort=SimpleNamespace(medium="medium"),
            SkillInput=None,
            version="0.147.0-test",
        )
        config = CodexSdkConfig(
            model="gpt-test",
            reasoning_effort=ReasoningEffort.MEDIUM,
            sandbox=Sandbox.READ_ONLY,
            cwd=cwd,
            leaf_worker=True,
        )
        adapter = CodexSdkAdapter(config, sdk=sdk)
        identity = adapter.start_thread()
        assert identity == ThreadIdentity("shared-thread")
        assert sdk.config == {"config_overrides": LEAF_WORKER_CONFIG_OVERRIDES, "cwd": str(cwd)}
        assert "env" not in sdk.config
        assert client.starts[0]["config"] == {"agents": {"enabled": False}, "features": {"multi_agent": False}}

        # Closing the optional UI/process boundary must not change the
        # persisted SDK identity.  A fresh SDK adapter resumes the same
        # standard-runtime thread by exact id.
        adapter.close()
        resumed_adapter = CodexSdkAdapter(config, sdk=sdk)
        resumed = resumed_adapter.resume_thread(identity)
        assert resumed == identity
        assert client.resumes[0][0] == identity.id
        assert client.resumes[0][1]["config"] == {"agents": {"enabled": False}, "features": {"multi_agent": False}}
        resumed_adapter.close()


def test_notifier_resumes_exact_source_without_model_effort_cwd_or_config_overrides() -> None:
    client = _WakeClient()

    class WakeSdk(SimpleNamespace):
        def Codex(self) -> _WakeClient:
            self.calls = getattr(self, "calls", 0) + 1
            return client

    sdk = WakeSdk(version="0.147.0-test")
    notifier = CodexSdkNotifier(sdk=sdk)
    assert notifier.deliver("source-thread", '{"kind":"TERMINAL"}') == "wake-turn"
    assert client.resumes == ["source-thread"]
    assert client.thread_value.payloads == ['{"kind":"TERMINAL"}']
    assert sdk.calls == 1


def test_notifier_classifies_resume_and_turn_failures_without_retry() -> None:
    resume_client = _WakeClient()

    class ResumeSdk(SimpleNamespace):
        def Codex(self) -> _WakeClient:
            return resume_client

    def unavailable() -> _WakeClient:
        raise RuntimeError("closed")

    with pytest.raises(WakeDeliveryUnavailable):
        CodexSdkNotifier(sdk=ResumeSdk(version="test"), client_factory=unavailable).deliver("source", "wake")

    ambiguous_client = _WakeClient(fail=True)
    with pytest.raises(WakeDeliveryAmbiguous):
        CodexSdkNotifier(sdk=ResumeSdk(version="test"), client_factory=lambda: ambiguous_client).deliver(
            "source", "wake"
        )


def test_plugin_capability_discovery_binds_bundle_and_rejects_disabled_or_changed_bytes() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "demo-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            '{"id":"demo-plugin","version":"1.2.3","source":"bundled",'
            '"enabled":true,"skills":["demo.skill"],"connectors":["demo-mcp"]}\n',
            encoding="utf-8",
        )
        snapshots = discover_plugin_capabilities(home)
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.readiness is PluginReadiness.READY
        requirement = PluginRequirement(
            "demo-plugin", "1.2.3", "bundled", bundle_digest(plugin), ("demo.skill",), ("demo-mcp",)
        )
        assert resolve_plugin_requirements((requirement,), home) == (snapshot,)
        (home / "config.toml").write_text("[plugins]\n\n[plugins.demo-plugin]\nenabled = false\n", encoding="utf-8")
        with pytest.raises(PluginCapabilityUnavailable, match="disabled"):
            resolve_plugin_requirements((requirement,), home)
        (home / "config.toml").write_text("[plugins]\n\n[plugins.demo-plugin]\nenabled = true\n", encoding="utf-8")
        (plugin / "skill.md").write_text("changed\n", encoding="utf-8")
        with pytest.raises(PluginCapabilityUnavailable, match="bytes"):
            resolve_plugin_requirements((requirement,), home)


def test_plugin_discovery_rejects_symlink_and_secret_bearing_manifest() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        (home / "plugins").mkdir(parents=True)
        target = Path(directory) / "target"
        target.mkdir()
        (home / "plugins" / "demo-plugin").symlink_to(target, target_is_directory=True)
        with pytest.raises(PluginCapabilityError, match="symlink"):
            discover_plugin_capabilities(home)
        (home / "plugins" / "demo-plugin").unlink()
        plugin = home / "plugins" / "demo-plugin"
        plugin.mkdir()
        (plugin / "plugin.json").write_text(
            '{"id":"demo-plugin","version":"1","source":"bundled","api_key":"should-never-be-retained"}\n',
            encoding="utf-8",
        )
        with pytest.raises(PluginCapabilityError, match="secret"):
            discover_plugin_capabilities(home)


def test_real_codex_cache_layout_uses_name_at_source_and_resolves_skill_path() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "cache" / "marketplace" / "demo" / "1.0"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / "skills" / "demo.skill").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            '{"name":"demo","version":"1.0","skills":"./skills/","enabled":true}\n', encoding="utf-8"
        )
        snapshot = discover_plugin_capabilities(home)[0]
        assert snapshot.canonical_id == "demo@marketplace"
        assert snapshot.source == "marketplace"
        requirement = PluginRequirement(
            snapshot.canonical_id, snapshot.version, snapshot.source, snapshot.bundle_digest, ("demo.skill",), ()
        )
        with _verified_skill_input((requirement,), (snapshot,), home) as skill_input:
            assert skill_input is not None
            assert skill_input.name == "demo.skill"
            assert Path(skill_input.path).is_dir()


def test_legacy_prompt_marker_cannot_authorize_plugin_requirements() -> None:
    assert not hasattr(plugin_capabilities, "plugin_requirements_from_prompt")
    assert not hasattr(plugin_capabilities, "bundled_skill_input_marker")


def test_secret_bearing_manifest_values_are_rejected() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "demo-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            '{"id":"demo-plugin","version":"1","source":"https://u:p@example.test"}\n', encoding="utf-8"
        )
        with pytest.raises(PluginCapabilityError, match="secret-bearing value"):
            discover_plugin_capabilities(home)


def test_local_marketplace_preserves_name_and_source_identity() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "local-marketplaces" / "local-market" / "plugins" / "demo"
        (plugin / "skills" / "demo.skill").mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            '{"name":"demo","version":"1","skills":"skills","enabled":true}\n', encoding="utf-8"
        )
        snapshot = discover_plugin_capabilities(home)[0]
        assert snapshot.canonical_id == "demo@local-market"
        assert snapshot.source == "local-market"


@pytest.mark.parametrize(
    ("constant", "expected"),
    (
        ("_MAX_DISCOVERY_ENTRIES", "total-entry"),
        ("_MAX_CANDIDATE_BUNDLES", "bundle-count"),
        ("_MAX_BUNDLE_DEPTH", "depth"),
        ("_MAX_BUNDLE_FILES", "file-count"),
        ("_MAX_SINGLE_FILE_BYTES", "byte bound"),
        ("_MAX_BUNDLE_BYTES", "total-byte"),
        ("_MAX_RESOLUTION_BYTES", "total-byte"),
    ),
)
def test_plugin_discovery_bounds_fail_closed(monkeypatch: pytest.MonkeyPatch, constant: str, expected: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "demo"
        (plugin / "skills" / "demo.skill").mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            '{"name":"demo","version":"1","skills":"skills","enabled":true}\n', encoding="utf-8"
        )
        monkeypatch.setattr(plugin_capabilities, constant, 0)
        with pytest.raises(PluginCapabilityError, match=expected):
            discover_plugin_capabilities(home)


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "fifo"))
def test_plugin_bundle_rejects_unsafe_file_types(unsafe_kind: str) -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "demo"
        plugin.mkdir(parents=True)
        manifest = plugin / "plugin.json"
        manifest.write_text('{"name":"demo","version":"1","enabled":true}\n', encoding="utf-8")
        unsafe = plugin / "unsafe"
        if unsafe_kind == "symlink":
            unsafe.symlink_to(manifest)
        elif unsafe_kind == "hardlink":
            os.link(manifest, unsafe)
        else:
            os.mkfifo(unsafe)
        with pytest.raises(PluginCapabilityError, match=r"symlink|hardlinked|non-regular"):
            discover_plugin_capabilities(home)


def test_held_skill_descriptor_survives_rename_but_fresh_swap_is_rejected() -> None:
    with TemporaryDirectory() as directory:
        home = Path(directory) / "codex"
        plugin = home / "plugins" / "demo"
        (plugin / "skills" / "demo.skill").mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            '{"name":"demo","version":"1","skills":"skills","enabled":true}\n', encoding="utf-8"
        )
        snapshot = discover_plugin_capabilities(home)[0]
        requirement = PluginRequirement("demo", "1", "bundled", snapshot.bundle_digest, ("demo.skill",), ())
        with _verified_skill_input((requirement,), (snapshot,), home) as skill_input:
            assert skill_input is not None
            renamed = plugin.with_name("demo-renamed")
            plugin.rename(renamed)
            assert Path(skill_input.path).is_dir()
        renamed.rename(plugin)
        (plugin / "plugin.json").write_text(
            '{"name":"demo","version":"1","skills":"skills","enabled":true,"setup_required":true}\n',
            encoding="utf-8",
        )
        with pytest.raises(PluginCapabilityUnavailable):
            with _verified_skill_input((requirement,), (snapshot,), home):
                pass
