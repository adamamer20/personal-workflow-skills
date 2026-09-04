from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import codex_flow.service as service_module
from codex_flow.ledger import HarnessRefreshBlocked, Ledger
from codex_flow.service import (
    CredentialUnavailable,
    ServiceError,
    ServiceRefreshDeferred,
    ServiceRefreshFailed,
    ServiceStartFailed,
    generate_unit,
    install_unit,
    refresh_with_credential,
    start_with_credential,
    unit_name,
    unit_path,
)


def test_temporary_service_template_has_exact_executable_and_no_install_side_effect() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        config_home = root / "config"
        unit = generate_unit(repository, executable=executable)

        assert unit.unit_name == unit_name(repository)
        assert unit.version == "0.2.0"
        assert unit.executable == executable
        assert f"ExecStart={executable} harness run --foreground --state-root {repository}" in unit.text
        executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        assert executable_digest == hashlib.sha256(b"#!/bin/sh\n").hexdigest()
        installed = install_unit(unit, config_home=config_home)
        assert installed == unit_path(config_home=config_home, repository_root=repository)
        assert installed.read_text(encoding="utf-8") == unit.text
        assert os.stat(installed).st_mode & 0o777 == 0o600
        assert not (config_home / "systemd" / "user" / "default.target.wants").exists()


def test_credential_handoff_passes_only_key_name_and_cleans_up_on_start_failure() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        secret = "do-not-leak-this-value"
        unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
        config_home = root / "config"
        install_unit(unit, config_home=config_home)
        calls: list[tuple[str, ...]] = []

        class Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode

        def runner(argv: tuple[str, ...], **_: object) -> Result:
            calls.append(argv)
            return Result(1 if argv[2] == "start" else 0)

        with pytest.raises(ServiceStartFailed):
            start_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": secret},
                runner=runner,
            )
        assert calls == [
            ("systemctl", "--user", "import-environment", "OPENAI_API_KEY"),
            ("systemctl", "--user", "start", unit.unit_name),
            ("systemctl", "--user", "unset-environment", "OPENAI_API_KEY"),
        ]
        assert all(secret not in argument for call in calls for argument in call)
        assert "PassEnvironment=OPENAI_API_KEY" in unit.text
        assert secret not in unit.text


def test_credential_handoff_fails_closed_before_manager_mutation_when_missing() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
        config_home = root / "config"
        install_unit(unit, config_home=config_home)
        calls: list[tuple[str, ...]] = []

        with pytest.raises(CredentialUnavailable):
            start_with_credential(unit, environment={}, runner=lambda argv, **_: calls.append(argv))
        assert calls == []


def test_credential_handoff_rejects_key_drift_even_when_value_exists() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")

        with pytest.raises(ServiceError, match="conflicts with the exact service unit"):
            start_with_credential(
                unit,
                provider_env_key="OTHER_API_KEY",
                environment={"OTHER_API_KEY": "value"},
                runner=lambda *_args, **_kwargs: None,
            )


@pytest.mark.parametrize("failure", (subprocess.SubprocessError("runner failed"), RuntimeError("runner defect")))
def test_credential_handoff_cleans_up_after_unexpected_runner_failure(failure: BaseException) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
        config_home = root / "config"
        install_unit(unit, config_home=config_home)
        calls: list[tuple[str, ...]] = []

        class Result:
            returncode = 0

        def runner(argv: tuple[str, ...], **_: object) -> Result:
            calls.append(argv)
            if argv[2] == "start":
                raise failure
            return Result()

        with pytest.raises(type(failure)):
            start_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
            )
        assert [call[2] for call in calls] == ["import-environment", "start", "unset-environment"]
        assert all("secret" not in argument for call in calls for argument in call)


def test_credential_handoff_preserves_original_failure_when_cleanup_raises_baseexception() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
        config_home = root / "config"
        install_unit(unit, config_home=config_home)
        calls: list[tuple[str, ...]] = []

        class Result:
            returncode = 0

        def runner(argv: tuple[str, ...], **_: object) -> Result:
            calls.append(argv)
            if argv[2] == "start":
                raise RuntimeError("start failed")
            if argv[2] == "unset-environment":
                raise KeyboardInterrupt()
            return Result()

        with pytest.raises(RuntimeError, match="start failed"):
            start_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
            )
        assert [call[2] for call in calls] == ["import-environment", "start", "unset-environment"]


@pytest.mark.parametrize(
    "mutation",
    (
        lambda text: text.replace("ExecStart=", "ExecStart=/tmp/stale ", 1),
        lambda text: text.replace("PassEnvironment=OPENAI_API_KEY", "PassEnvironment=STALE_API_KEY", 1),
        lambda text: text.replace(
            "# Codex-Flow-Profile-SHA256=" + "a" * 64, "# Codex-Flow-Profile-SHA256=" + "b" * 64, 1
        ),
    ),
)
def test_credential_handoff_refuses_drifted_installed_unit_before_import(mutation) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(
            repository,
            executable=executable,
            provider_env_key="OPENAI_API_KEY",
            profile_sha256="a" * 64,
        )
        config_home = root / "config"
        installed = install_unit(unit, config_home=config_home)
        installed.write_text(mutation(unit.text), encoding="utf-8")
        calls: list[tuple[str, ...]] = []

        with pytest.raises(ServiceError, match="installed service unit"):
            start_with_credential(
                unit,
                config_home=config_home,
                profile_sha256="a" * 64,
                environment={"OPENAI_API_KEY": "secret"},
                runner=lambda argv, **_: calls.append(argv),
            )
        assert calls == []


def test_credential_handoff_refuses_profile_identity_drift_before_import() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        executable = root / "codex-flow"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        unit = generate_unit(
            repository,
            executable=executable,
            provider_env_key="OPENAI_API_KEY",
            profile_sha256="a" * 64,
        )
        config_home = root / "config"
        install_unit(unit, config_home=config_home)
        with pytest.raises(ServiceError, match="provider profile identity"):
            start_with_credential(
                unit,
                config_home=config_home,
                profile_sha256="b" * 64,
                environment={"OPENAI_API_KEY": "secret"},
                runner=lambda *_args, **_kwargs: None,
            )


class _SystemctlResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _refresh_fixture(tmp_path: Path) -> tuple[Path, object, Ledger, dict[tuple[int, str], bool], dict[str, bool]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    state_dir = repository / ".codex-flow"
    state_dir.mkdir()
    ledger = Ledger(state_dir / "workflow.db")
    ledger.acquire_harness(
        repository_root=repository,
        state_root=repository,
        pid=500,
        process_birth_identity="old-birth",
        executable_digest="a" * 64,
        version="0.2.0",
        owner_nonce_sha256="b" * 64,
    )
    unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
    install_unit(unit, config_home=tmp_path / "config")
    processes = {(500, "old-birth"): True}
    service_state = {"active": True}
    return repository, unit, ledger, processes, service_state


def test_refresh_handoff_fences_shutdowns_by_identity_and_verifies_replacement(tmp_path: Path) -> None:
    _repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    shutdowns: list[tuple[Path, float]] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(500, "new-birth")] = True
            service_state["active"] = True
            ledger.acquire_harness(
                repository_root=unit.repository_root,
                state_root=unit.state_root,
                pid=500,
                process_birth_identity="new-birth",
                executable_digest="c" * 64,
                version=unit.version,
                owner_nonce_sha256="d" * 64,
            )
        return _SystemctlResult(0)

    def shutdown(socket_path: Path, timeout: float) -> dict[str, object]:
        shutdowns.append((socket_path, timeout))
        authority = ledger.harness_authority()
        assert authority is not None
        assert authority["requested_shutdown"] == 1
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    try:
        result = refresh_with_credential(
            unit,
            config_home=tmp_path / "config",
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            ledger=ledger,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            clock=lambda: 0.0,
            sleeper=lambda _: pytest.fail("handoff should not sleep when shutdown is observed"),
            shutdown_sender=shutdown,
        )
        assert result["refreshed"] is True
        assert result["old_epoch"] == 1
        assert result["new_epoch"] == 2
        assert len(shutdowns) == 1
        assert shutdowns[0][0] == unit.state_root / ".codex-flow" / "runtime" / "harness.sock"
        assert [call[2] for call in calls] == [
            "is-active",
            "daemon-reload",
            "import-environment",
            "start",
            "is-active",
        ]
        assert all("restart" not in call for call in calls)
        authority = ledger.harness_authority()
        assert authority is not None
        assert authority["requested_shutdown"] == 0
        assert authority["process_birth_identity"] == "new-birth"
    finally:
        ledger.close()


def test_refresh_defers_before_shutdown_or_manager_mutation_for_active_child(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
    config_home = tmp_path / "config"
    install_unit(unit, config_home=config_home)
    calls: list[tuple[str, ...]] = []
    shutdowns: list[Path] = []

    class ActiveChildLedger:
        def arm_harness_refresh_fence(self) -> None:
            raise HarnessRefreshBlocked("worker child is active")

    with pytest.raises(ServiceRefreshDeferred, match="worker child is active"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=lambda argv, **_: calls.append(argv),
            ledger=ActiveChildLedger(),  # type: ignore[arg-type]
            shutdown_sender=lambda path, _timeout: shutdowns.append(path) or {},
        )
    assert calls == []
    assert shutdowns == []


def test_refresh_preflight_allows_only_a_well_formed_profile_identity_update(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    config_home = tmp_path / "config"
    old_unit = generate_unit(
        repository,
        executable=executable,
        provider_env_key="OPENAI_API_KEY",
        profile_sha256="a" * 64,
    )
    new_unit = generate_unit(
        repository,
        executable=executable,
        provider_env_key="OPENAI_API_KEY",
        profile_sha256="b" * 64,
    )
    path = install_unit(old_unit, config_home=config_home)

    service_module._validate_installed_unit(
        new_unit,
        config_home=config_home,
        expected_profile_sha256="b" * 64,
        credential_value="secret",
        allow_profile_identity_update=True,
    )

    path.write_text(
        path.read_text(encoding="utf-8").replace("NoNewPrivileges=yes", "NoNewPrivileges=no"), encoding="utf-8"
    )
    with pytest.raises(ServiceError, match="content drifted"):
        service_module._validate_installed_unit(
            new_unit,
            config_home=config_home,
            expected_profile_sha256="b" * 64,
            credential_value="secret",
            allow_profile_identity_update=True,
        )

    moved = path.read_text(encoding="utf-8").splitlines()
    profile_index = next(index for index, line in enumerate(moved) if line.startswith("# Codex-Flow-Profile-SHA256="))
    moved.insert(0, moved.pop(profile_index))
    path.write_text("\n".join(moved) + "\n", encoding="utf-8")
    with pytest.raises(ServiceError, match="profile identity drifted"):
        service_module._validate_installed_unit(
            new_unit,
            config_home=config_home,
            expected_profile_sha256="b" * 64,
            credential_value="secret",
            allow_profile_identity_update=True,
        )


def test_refresh_timeout_is_explicit_and_leaves_fence_armed(tmp_path: Path) -> None:
    _repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    now = [0.0]

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        if argv[2] == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if argv[2] == "start":
            service_state["active"] = True
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    try:
        with pytest.raises(ServiceRefreshFailed, match="timed out"):
            refresh_with_credential(
                unit,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                ledger=ledger,
                process_is_live=lambda pid, birth: processes.get((pid, birth), False),
                clock=lambda: now[0],
                sleeper=lambda delay: now.__setitem__(0, now[0] + max(delay, 0.1)),
                shutdown_sender=shutdown,
                deadline_seconds=0.2,
            )
        authority = ledger.harness_authority()
        assert authority is not None
        assert authority["requested_shutdown"] == 1
        assert [call[2] for call in calls][-1] == "unset-environment"
    finally:
        ledger.close()


def test_refresh_migrates_one_installed_v18_supervisor_handoff_to_harness(tmp_path: Path) -> None:
    repository = tmp_path / "supervisor-repository"
    repository.mkdir()
    executable = tmp_path / "supervisor-bin" / "codex-flow-supervisor"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    state_root = repository / ".codex-flow"
    state_root.mkdir()
    ledger_path = state_root / "workflow.db"
    ledger = Ledger(ledger_path)
    ledger.acquire_harness(
        repository_root=repository,
        state_root=repository,
        pid=500,
        process_birth_identity="old-birth",
        executable_digest="a" * 64,
        version="0.2.0",
        owner_nonce_sha256="b" * 64,
    )
    ledger.close()
    connection = sqlite3.connect(ledger_path)
    connection.execute("ALTER TABLE harness_authority RENAME TO supervisor_authority")
    connection.execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
    connection.execute(
        "UPDATE schema_meta SET value = 'codex_flow_event_driven_program_controller_v18' WHERE key = 'schema_identity'"
    )
    connection.commit()
    connection.close()

    unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
    legacy = service_module._legacy_supervisor_unit(unit)
    config_home = tmp_path / "config"
    install_unit(legacy, config_home=config_home)
    processes = {(500, "old-birth"): True}
    service_state = {"active": True}
    calls: list[tuple[str, ...]] = []
    now = [0.0]

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
        if operation == "is-active":
            return Result(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(501, "new-birth")] = True
            service_state["active"] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=repository,
                state_root=repository,
                pid=501,
                process_birth_identity="new-birth",
                executable_digest="c" * 64,
                version=unit.version,
                owner_nonce_sha256="d" * 64,
            )
            replacement.close()
        return Result(0)

    def clock() -> float:
        now[0] += 0.001
        return now[0]

    def shutdown(socket_path: Path, _timeout: float) -> dict[str, object]:
        assert socket_path.name == "supervisor.sock"
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        environment={"OPENAI_API_KEY": "secret"},
        runner=runner,
        process_is_live=lambda pid, birth: processes.get((pid, birth), False),
        clock=clock,
        sleeper=lambda _delay: None,
        shutdown_sender=shutdown,
    )
    assert result["refreshed"] is True
    installed = (config_home / "systemd" / "user" / unit.unit_name).read_text(encoding="utf-8")
    exec_lines = [line for line in installed.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines == [f"ExecStart={executable} harness run --foreground --state-root {repository}"]
    assert [call[2] for call in calls] == ["is-active", "daemon-reload", "import-environment", "start", "is-active"]
    migrated = Ledger(ledger_path)
    try:
        assert migrated.schema_version.value == 19
        assert (
            migrated._db().execute("SELECT 1 FROM sqlite_master WHERE name = 'supervisor_authority'").fetchone() is None
        )
    finally:
        migrated.close()
